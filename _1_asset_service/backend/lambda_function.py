# asset service api, runs in lambda behind api gateway http api.
# api gateway jwt authorizer already verified the cognito token; this code
# resolves user_id from the user table, checks the role (admin/guest) from
# cognito groups, and serves the tree/asset apis.
# see asset_service_impl.md#api for the api list and flows.

import base64
import json
import os
import secrets
import string
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import PurePosixPath
from urllib.parse import quote

import boto3
from boto3.dynamodb.conditions import Key

BUCKET_ASSET = os.environ["BUCKET_ASSET"]
TABLE_ASSET_NODE = os.environ["TABLE_ASSET_NODE"]
TABLE_USER = os.environ["TABLE_USER"]
GROUP_ACCESS = os.environ["GROUP_ACCESS"]
GROUP_ADMIN = os.environ["GROUP_ADMIN"]

FOLDER_MARKER = "__@@FOLDER@@__"
PRESIGN_EXPIRE_SEC = 3600
ID_LENGTH = 16
ID_CHARS = string.digits + string.ascii_lowercase
RANK_CHARS = string.digits + string.ascii_lowercase  # '0' < ... < 'z'

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
table_node = dynamodb.Table(TABLE_ASSET_NODE)
table_user = dynamodb.Table(TABLE_USER)

# sub -> user_id, cached for the lifetime of the lambda container
user_id_cache = {}


# ------------------------------------------------------------------- handler


def lambda_handler(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event["rawPath"]
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]

    role = role_get(claims)
    if role is None:
        return resp(403, -3, message=f"user is not in group {GROUP_ACCESS}")

    user_id = user_id_resolve(claims["sub"])
    if user_id is None:
        return resp(
            403, -4,
            message="no user_id mapping for this cognito user, run ensure_user_table.py",
        )

    # path shape: /api/<section>[/<node_id>]
    parts = path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "api":
        return resp(404, -2, message=f"unknown path: {path}")
    section = parts[1]
    node_id = parts[2] if len(parts) > 2 else None

    try:
        body = body_parse(event)

        if method == "GET" and section == "me":
            return api_me(user_id, claims, role)
        if method == "GET" and section == "tree":
            return api_tree(user_id)
        if method == "GET" and section == "download" and node_id:
            return api_download(user_id, node_id)

        if role != "admin":
            return resp(403, -3, message="this operation needs admin role")

        if method == "POST" and section == "folder":
            return api_folder_create(user_id, body)
        if method == "POST" and section == "asset":
            return api_asset_create(user_id, body)
        if method == "POST" and section == "asset-complete":
            return api_asset_complete(user_id, body)
        if method == "PATCH" and section == "node" and node_id:
            return api_node_update(user_id, node_id, body)
        if method == "DELETE" and section == "node" and node_id:
            return api_node_delete(user_id, node_id)

        return resp(404, -2, message=f"unknown api: {method} {path}")
    except ApiError as error:
        return resp(error.http_status, error.code, message=error.message)


class ApiError(Exception):
    def __init__(self, code, message, http_status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def resp(http_status, code, data=None, message=""):
    body = {"code": code, "data": data, "message": message}
    return {
        "statusCode": http_status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=json_default),
    }


def resp_ok(data=None, message=""):
    return resp(200, 0, data=data, message=message)


def json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    raise TypeError(f"not json serializable: {type(value)}")


def body_parse(event):
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    return json.loads(raw)


# ---------------------------------------------------------------- auth / user


def role_get(claims):
    """role from cognito groups in the jwt: 'admin' / 'guest' / None (no access)."""
    groups = claims.get("cognito:groups", [])
    if isinstance(groups, str):
        # http api authorizer stringifies the list, e.g. "[admin asset-service]"
        groups = groups.strip("[]").split()
    if GROUP_ACCESS not in groups:
        return None
    return "admin" if GROUP_ADMIN in groups else "guest"


def user_id_resolve(sub):
    if sub in user_id_cache:
        return user_id_cache[sub]
    found = table_user.query(
        IndexName="gsi_auth_id",
        KeyConditionExpression=Key("auth_id").eq(f"cognito#{sub}"),
    )["Items"]
    if not found:
        return None
    user_id_cache[sub] = found[0]["user_id"]
    return user_id_cache[sub]


# ------------------------------------------------------------------ tree data


def id_generate():
    return "".join(secrets.choice(ID_CHARS) for _ in range(ID_LENGTH))


def nodes_all(user_id):
    nodes = []
    params = {"KeyConditionExpression": Key("user_id").eq(user_id)}
    while True:
        page = table_node.query(**params)
        nodes.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return nodes
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def node_get(user_id, node_id):
    item = table_node.get_item(Key={"user_id": user_id, "node_id": node_id}).get("Item")
    if item is None:
        raise ApiError(-2, f"node not found: {node_id}", http_status=404)
    return item


def parent_check(user_id, parent_id):
    """parent must exist and be a tree folder (a node without asset_id)."""
    if parent_id is None:
        return
    parent = node_get(user_id, parent_id)
    if "asset_id" in parent:
        raise ApiError(-1, "parent node is an asset, not a tree folder")


def rank_between(rank_prev, rank_next):
    """string strictly between the two ranks, chars 0-9a-z. either side can be
    None (open end). generated ranks never end with '0', so between any two
    generated ranks a midpoint always exists."""
    prev = rank_prev or ""
    next_ = rank_next or ""
    result = []
    i = 0
    while True:
        digit_prev = RANK_CHARS.index(prev[i]) if i < len(prev) else 0
        digit_next = RANK_CHARS.index(next_[i]) if i < len(next_) else len(RANK_CHARS)
        if digit_next - digit_prev > 1:
            result.append(RANK_CHARS[(digit_prev + digit_next) // 2])
            return "".join(result)
        result.append(RANK_CHARS[digit_prev])
        i += 1


def rank_for_append(user_id, parent_id):
    """rank placing a new node after the current last child of the parent."""
    siblings = [
        node for node in nodes_all(user_id)
        if node.get("parent_id") == parent_id or (parent_id is None and "parent_id" not in node)
    ]
    rank_last = max((node["lexorank"] for node in siblings), default=None)
    return rank_between(rank_last, None)


def rank_validate(lexorank):
    if not lexorank or any(char not in RANK_CHARS for char in lexorank):
        raise ApiError(-1, "lexorank must be a non-empty string of chars 0-9a-z")


def file_path_validate(path):
    pure = PurePosixPath(path)
    if path.startswith("/") or ".." in pure.parts or path.strip() == "":
        raise ApiError(-1, f"invalid file path: {path}")


# ------------------------------------------------------------------ apis: read


def api_me(user_id, claims, role):
    return resp_ok({
        "user_id": user_id,
        "username": claims.get("cognito:username") or claims.get("username"),
        "email": claims.get("email"),
        "role": role,
    })


def api_tree(user_id):
    return resp_ok({"nodes": nodes_all(user_id)})


def api_download(user_id, node_id):
    node = node_get(user_id, node_id)
    if "asset_id" not in node:
        raise ApiError(-1, "node is a tree folder, nothing to download")
    if node.get("upload_state") != "ready":
        raise ApiError(-1, "asset upload is not completed yet")
    if node["asset_type"] == "file":
        url = download_url_file(node)
    else:
        url = download_url_folder_zip(node)
    return resp_ok({"url": url})


def download_url_file(node):
    file_name = node["file_name"]
    return s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET_ASSET,
            "Key": f"{node['asset_id']}/{file_name}",
            "ResponseContentDisposition": f"attachment; filename*=UTF-8''{quote(file_name)}",
        },
        ExpiresIn=PRESIGN_EXPIRE_SEC,
    )


def download_url_folder_zip(node):
    """zip the folder asset in /tmp, upload to zip-tmp/ (expired by bucket
    lifecycle rule after 1 day), return a presigned url of the zip."""
    asset_id = node["asset_id"]
    prefix = f"{asset_id}/"
    zip_path = f"/tmp/{asset_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET_ASSET, Prefix=prefix):
            for obj in page.get("Contents", []):
                rel_path = obj["Key"][len(prefix):]
                if rel_path.endswith(FOLDER_MARKER):
                    dir_path = rel_path[: -len(FOLDER_MARKER)]
                    zip_file.writestr(zipfile.ZipInfo(dir_path), b"")
                    continue
                tmp_path = f"/tmp/{asset_id}.part"
                s3.download_file(BUCKET_ASSET, obj["Key"], tmp_path)
                zip_file.write(tmp_path, rel_path)
                os.remove(tmp_path)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    zip_key = f"zip-tmp/{asset_id}-{timestamp}.zip"
    s3.upload_file(zip_path, BUCKET_ASSET, zip_key)
    os.remove(zip_path)

    zip_name = f"{node['name']}.zip"
    return s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET_ASSET,
            "Key": zip_key,
            "ResponseContentDisposition": f"attachment; filename*=UTF-8''{quote(zip_name)}",
        },
        ExpiresIn=PRESIGN_EXPIRE_SEC,
    )


# ----------------------------------------------------------------- apis: write


def api_folder_create(user_id, body):
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError(-1, "folder name is required")
    parent_id = body.get("parent_id")
    parent_check(user_id, parent_id)

    node = {
        "user_id": user_id,
        "node_id": id_generate(),
        "name": name,
        "lexorank": rank_for_append(user_id, parent_id),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if parent_id is not None:
        node["parent_id"] = parent_id
    table_node.put_item(Item=node)
    return resp_ok({"node": node})


def api_asset_create(user_id, body):
    name = (body.get("name") or "").strip()
    asset_type = body.get("asset_type")
    files = body.get("files") or []
    if not name:
        raise ApiError(-1, "asset name is required")
    if asset_type not in ("file", "folder"):
        raise ApiError(-1, "asset_type must be 'file' or 'folder'")
    if not files:
        raise ApiError(-1, "files list is empty")
    if asset_type == "file" and len(files) != 1:
        raise ApiError(-1, "file asset must have exactly one file")
    parent_id = body.get("parent_id")
    parent_check(user_id, parent_id)

    asset_id = id_generate()
    upload_urls = []
    for file_entry in files:
        path = file_entry["path"]
        file_path_validate(path)
        if file_entry.get("is_folder"):
            # empty folder inside a folder asset: marker object with fixed content
            key = f"{asset_id}/{path.rstrip('/')}/{FOLDER_MARKER}"
            content_type = "text/plain"
        else:
            key = f"{asset_id}/{path}"
            content_type = file_entry.get("content_type") or "application/octet-stream"
        url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": BUCKET_ASSET,
                "Key": key,
                "ContentType": content_type,
                "StorageClass": "STANDARD_IA",
            },
            ExpiresIn=PRESIGN_EXPIRE_SEC,
        )
        upload_urls.append({
            "path": path,
            "url": url,
            "headers": {"Content-Type": content_type, "x-amz-storage-class": "STANDARD_IA"},
        })

    node = {
        "user_id": user_id,
        "node_id": id_generate(),
        "name": name,
        "lexorank": rank_for_append(user_id, parent_id),
        "asset_id": asset_id,
        "asset_type": asset_type,
        "upload_state": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if parent_id is not None:
        node["parent_id"] = parent_id
    if asset_type == "file":
        node["file_name"] = files[0]["path"]
        node["content_type"] = files[0].get("content_type") or "application/octet-stream"
    table_node.put_item(Item=node)
    return resp_ok({"node": node, "upload_urls": upload_urls})


def api_asset_complete(user_id, body):
    node_id = body.get("node_id")
    if not node_id:
        raise ApiError(-1, "node_id is required")
    node = node_get(user_id, node_id)
    if "asset_id" not in node:
        raise ApiError(-1, "node is a tree folder, not an asset")

    size_total = 0
    object_count = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET_ASSET, Prefix=f"{node['asset_id']}/"):
        for obj in page.get("Contents", []):
            size_total += obj["Size"]
            object_count += 1
    if object_count == 0:
        raise ApiError(-1, "no uploaded object found for this asset")

    table_node.update_item(
        Key={"user_id": user_id, "node_id": node_id},
        # 'size' is a dynamodb reserved word, needs the #size alias
        UpdateExpression="SET upload_state = :state, #size = :size",
        ExpressionAttributeNames={"#size": "size"},
        ExpressionAttributeValues={":state": "ready", ":size": size_total},
    )
    node["upload_state"] = "ready"
    node["size"] = size_total
    return resp_ok({"node": node})


def api_node_update(user_id, node_id, body):
    node_get(user_id, node_id)
    if "name" in body:
        return node_rename(user_id, node_id, body["name"])
    if "lexorank" in body:
        return node_move(user_id, node_id, body.get("parent_id"), body["lexorank"])
    raise ApiError(-1, "body must contain 'name' (rename) or 'lexorank' (move)")


def node_rename(user_id, node_id, name):
    name = (name or "").strip()
    if not name:
        raise ApiError(-1, "name must not be empty")
    table_node.update_item(
        Key={"user_id": user_id, "node_id": node_id},
        UpdateExpression="SET #name = :name",
        ExpressionAttributeNames={"#name": "name"},
        ExpressionAttributeValues={":name": name},
    )
    return resp_ok()


def node_move(user_id, node_id, parent_id, lexorank):
    rank_validate(lexorank)
    parent_check(user_id, parent_id)

    # reject moving a folder into its own subtree
    nodes_by_id = {node["node_id"]: node for node in nodes_all(user_id)}
    ancestor_id = parent_id
    while ancestor_id is not None:
        if ancestor_id == node_id:
            raise ApiError(-1, "cannot move a node into its own subtree")
        ancestor_id = nodes_by_id.get(ancestor_id, {}).get("parent_id")

    if parent_id is None:
        table_node.update_item(
            Key={"user_id": user_id, "node_id": node_id},
            UpdateExpression="SET lexorank = :rank REMOVE parent_id",
            ExpressionAttributeValues={":rank": lexorank},
        )
    else:
        table_node.update_item(
            Key={"user_id": user_id, "node_id": node_id},
            UpdateExpression="SET lexorank = :rank, parent_id = :parent",
            ExpressionAttributeValues={":rank": lexorank, ":parent": parent_id},
        )
    return resp_ok()


def api_node_delete(user_id, node_id):
    node_get(user_id, node_id)
    nodes = nodes_all(user_id)

    # collect the subtree rooted at node_id
    child_ids_by_parent = {}
    for node in nodes:
        if "parent_id" in node:
            child_ids_by_parent.setdefault(node["parent_id"], []).append(node["node_id"])
    nodes_by_id = {node["node_id"]: node for node in nodes}
    subtree_ids = []
    pending_ids = [node_id]
    while pending_ids:
        current_id = pending_ids.pop()
        subtree_ids.append(current_id)
        pending_ids.extend(child_ids_by_parent.get(current_id, []))

    for subtree_id in subtree_ids:
        asset_id = nodes_by_id[subtree_id].get("asset_id")
        if asset_id:
            s3_prefix_delete(f"{asset_id}/")

    with table_node.batch_writer() as batch:
        for subtree_id in subtree_ids:
            batch.delete_item(Key={"user_id": user_id, "node_id": subtree_id})
    return resp_ok({"node_ids_deleted": subtree_ids})


def s3_prefix_delete(prefix):
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET_ASSET, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=BUCKET_ASSET, Delete={"Objects": keys})
