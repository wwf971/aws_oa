# asset timeline api, runs in lambda behind api gateway http api.
# api gateway jwt authorizer already verified the cognito token; this code
# resolves user_id from the user table, checks the role (admin/guest) from
# cognito groups, and serves the timeline apis.
#
# api list (all responses use {code, data, message}; code 0 = success):
#   GET    /api/me                                       role of the caller
#   GET    /api/timeline?name=                           list/search own timelines
#   POST   /api/timeline                                 create {name, time_zone}
#   GET    /api/timeline/{timeline_id}                   get one timeline
#   PATCH  /api/timeline/{timeline_id}                   update {name?, time_zone?}
#   DELETE /api/timeline/{timeline_id}                   delete timeline + its entries
#   GET    /api/timeline/{timeline_id}/asset             ?time_start=&time_end=&limit=
#   GET    /api/timeline/{timeline_id}/asset-neighbor    ?time_point=&count=&direction=
#   POST   /api/timeline/{timeline_id}/asset             collect {asset_id, time_stamp,
#                                                                  time_stamp_timezone?}
#   PATCH  /api/timeline/{timeline_id}/asset/{asset_id}  change time {time_stamp,
#                                                                  time_stamp_timezone?}
#   DELETE /api/timeline/{timeline_id}/asset/{asset_id}  remove asset from timeline
#   GET    /api/asset/{asset_id}/timeline                timelines collecting the asset
#
# time storage (see time-format.md): time stamps are unix milliseconds stored
# as numbers, each accompanied by a timezone attribute in signed minutes
# (e.g. +09:00 -> 540). asset ids are not validated against the asset service:
# a timeline entry is just (timeline_id, asset_id, time point).

import base64
import json
import os
import secrets
import string
import time

import boto3
from boto3.dynamodb.conditions import Key
from decimal import Decimal

TABLE_TIMELINE = os.environ["TABLE_TIMELINE"]
TABLE_TIMELINE_ASSET = os.environ["TABLE_TIMELINE_ASSET"]
TABLE_USER = os.environ["TABLE_USER"]
GROUP_ACCESS = os.environ["GROUP_ACCESS"]
GROUP_ADMIN = os.environ["GROUP_ADMIN"]

ID_LENGTH = 16
ID_CHARS = string.digits + string.ascii_lowercase

# sort key of the timeline-asset table: zero-padded time stamp + '#' + asset id.
# plain string order of the key equals time order, and the key stays unique
# when several assets share one time stamp. '#' (0x23) sorts before '0' and
# '~' (0x7e) sorts after 'z', which the range/neighbor key conditions rely on.
TIME_KEY_DIGITS = 16
TIMEZONE_MINUTES_MAX = 14 * 60

RANGE_LIMIT_DEFAULT = 1000
NEIGHBOR_COUNT_DEFAULT = 10
NEIGHBOR_COUNT_MAX = 100

dynamodb = boto3.resource("dynamodb")
table_timeline = dynamodb.Table(TABLE_TIMELINE)
table_timeline_asset = dynamodb.Table(TABLE_TIMELINE_ASSET)
table_user = dynamodb.Table(TABLE_USER)

# sub -> user_id, cached for the lifetime of the lambda container
user_id_cache = {}


# ------------------------------------------------------------------- handler


def lambda_handler(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event["rawPath"]
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    query = event.get("queryStringParameters") or {}

    role = role_get(claims)
    if role is None:
        return resp(403, -3, message=f"user is not in group {GROUP_ACCESS}")

    user_id = user_id_resolve(claims["sub"])
    if user_id is None:
        return resp(
            403, -4,
            message="no user_id mapping for this cognito user, run ensure_user_table.py",
        )

    try:
        body = body_parse(event)
        return route(method, path, query, body, user_id, claims, role)
    except ApiError as error:
        return resp(error.http_status, error.code, message=error.message)


def route(method, path, query, body, user_id, claims, role):
    parts = path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "api":
        raise ApiError(-2, f"unknown path: {path}", http_status=404)
    section = parts[1]

    # reads, allowed for both guest and admin
    if method == "GET":
        if parts[1:] == ["me"]:
            return api_me(user_id, claims, role)
        if parts[1:] == ["timeline"]:
            return api_timeline_list(user_id, query)
        if section == "timeline" and len(parts) == 3:
            return api_timeline_get(user_id, parts[2])
        if section == "timeline" and len(parts) == 4 and parts[3] == "asset":
            return api_timeline_assets(user_id, parts[2], query)
        if section == "timeline" and len(parts) == 4 and parts[3] == "asset-neighbor":
            return api_timeline_asset_neighbors(user_id, parts[2], query)
        if section == "asset" and len(parts) == 4 and parts[3] == "timeline":
            return api_asset_timelines(user_id, parts[2])
        raise ApiError(-2, f"unknown api: GET {path}", http_status=404)

    # writes, admin only
    if role != "admin":
        return resp(403, -3, message="this operation needs admin role")

    if method == "POST" and parts[1:] == ["timeline"]:
        return api_timeline_create(user_id, body)
    if section == "timeline" and len(parts) == 3:
        if method == "PATCH":
            return api_timeline_update(user_id, parts[2], body)
        if method == "DELETE":
            return api_timeline_delete(user_id, parts[2])
    if section == "timeline" and len(parts) == 4 and parts[3] == "asset":
        if method == "POST":
            return api_asset_collect(user_id, parts[2], body)
    if section == "timeline" and len(parts) == 5 and parts[3] == "asset":
        if method == "PATCH":
            return api_asset_time_change(user_id, parts[2], parts[4], body)
        if method == "DELETE":
            return api_asset_remove(user_id, parts[2], parts[4])
    raise ApiError(-2, f"unknown api: {method} {path}", http_status=404)


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


# ---------------------------------------------------------- values and helpers


def id_generate():
    return "".join(secrets.choice(ID_CHARS) for _ in range(ID_LENGTH))


def time_key_build(time_stamp, asset_id):
    return f"{time_stamp:0{TIME_KEY_DIGITS}d}#{asset_id}"


def time_stamp_parse(value, name):
    try:
        time_stamp = int(value)
    except (TypeError, ValueError):
        raise ApiError(-1, f"{name} must be an integer of unix milliseconds")
    if time_stamp < 0 or time_stamp >= 10 ** TIME_KEY_DIGITS:
        raise ApiError(-1, f"{name} out of supported range: {time_stamp}")
    return time_stamp


def timezone_parse(value, name):
    try:
        timezone_minutes = int(value)
    except (TypeError, ValueError):
        raise ApiError(-1, f"{name} must be an integer of minutes, e.g. 540 for +09:00")
    if abs(timezone_minutes) > TIMEZONE_MINUTES_MAX:
        raise ApiError(-1, f"{name} out of range: {timezone_minutes}")
    return timezone_minutes


def int_query_parse(query, name, default=None, required=False):
    if name not in query:
        if required:
            raise ApiError(-1, f"query parameter {name} is required")
        return default
    try:
        return int(query[name])
    except ValueError:
        raise ApiError(-1, f"query parameter {name} must be an integer")


def timeline_get(user_id, timeline_id):
    item = table_timeline.get_item(
        Key={"user_id": user_id, "timeline_id": timeline_id}
    ).get("Item")
    if item is None:
        raise ApiError(-2, f"timeline not found: {timeline_id}", http_status=404)
    return item


def entry_public(item):
    """timeline-asset item -> response shape (internal sort key stripped)."""
    return {
        "asset_id": item["asset_id"],
        "time_stamp": item["time_stamp"],
        "time_stamp_timezone": item["time_stamp_timezone"],
    }


def collect_entry_find(user_id, timeline_id, asset_id):
    """the (timeline_id, asset_id) collect entry, via gsi_asset_id, or None.
    the gsi is eventually consistent, which is acceptable here: collect and
    change/remove of the same asset are not expected within the same moment."""
    params = {
        "IndexName": "gsi_asset_id",
        "KeyConditionExpression": Key("asset_id").eq(asset_id),
    }
    while True:
        page = table_timeline_asset.query(**params)
        for item in page["Items"]:
            if item["timeline_id"] == timeline_id and item["user_id"] == user_id:
                return item
        if "LastEvaluatedKey" not in page:
            return None
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# ------------------------------------------------------------- apis: timeline


def api_me(user_id, claims, role):
    return resp_ok({
        "user_id": user_id,
        "username": claims.get("cognito:username") or claims.get("username"),
        "email": claims.get("email"),
        "role": role,
    })


def api_timeline_list(user_id, query):
    """all timelines of the user; ?name= narrows by case-insensitive substring
    (a user's timeline count is small, so filtering in code is fine)."""
    timelines = []
    params = {"KeyConditionExpression": Key("user_id").eq(user_id)}
    while True:
        page = table_timeline.query(**params)
        timelines.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            break
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    name_query = (query.get("name") or "").strip().lower()
    if name_query:
        timelines = [
            timeline for timeline in timelines
            if name_query in timeline["name"].lower()
        ]
    return resp_ok({"timelines": timelines})


def api_timeline_get(user_id, timeline_id):
    return resp_ok({"timeline": timeline_get(user_id, timeline_id)})


def api_timeline_create(user_id, body):
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError(-1, "timeline name is required")
    time_zone = timezone_parse(body.get("time_zone"), "time_zone")

    timeline = {
        "user_id": user_id,
        "timeline_id": id_generate(),
        "name": name,
        "time_zone": time_zone,
        "create_at": int(time.time() * 1000),
        "create_at_timezone": time_zone,
    }
    table_timeline.put_item(Item=timeline)
    return resp_ok({"timeline": timeline})


def api_timeline_update(user_id, timeline_id, body):
    timeline_get(user_id, timeline_id)
    updates = {}
    if "name" in body:
        name = (body["name"] or "").strip()
        if not name:
            raise ApiError(-1, "name must not be empty")
        updates["name"] = name
    if "time_zone" in body:
        updates["time_zone"] = timezone_parse(body["time_zone"], "time_zone")
    if not updates:
        raise ApiError(-1, "body must contain 'name' and/or 'time_zone'")

    table_timeline.update_item(
        Key={"user_id": user_id, "timeline_id": timeline_id},
        # 'name' is a dynamodb reserved word, alias every updated attribute
        UpdateExpression="SET " + ", ".join(f"#{key} = :{key}" for key in updates),
        ExpressionAttributeNames={f"#{key}": key for key in updates},
        ExpressionAttributeValues={f":{key}": value for key, value in updates.items()},
    )
    return resp_ok()


def api_timeline_delete(user_id, timeline_id):
    timeline_get(user_id, timeline_id)

    entry_keys = []
    params = {"KeyConditionExpression": Key("timeline_id").eq(timeline_id)}
    while True:
        page = table_timeline_asset.query(**params)
        entry_keys.extend(
            {"timeline_id": item["timeline_id"], "time_key": item["time_key"]}
            for item in page["Items"]
        )
        if "LastEvaluatedKey" not in page:
            break
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    with table_timeline_asset.batch_writer() as batch:
        for key in entry_keys:
            batch.delete_item(Key=key)
    table_timeline.delete_item(Key={"user_id": user_id, "timeline_id": timeline_id})
    return resp_ok({"entry_count_deleted": len(entry_keys)})


# ------------------------------------------------------- apis: assets in time


def api_timeline_assets(user_id, timeline_id, query):
    """assets whose time point is within [time_start, time_end], ascending.
    a query of a given year/month/date is this api with the day's boundaries."""
    timeline_get(user_id, timeline_id)
    time_start = time_stamp_parse(
        int_query_parse(query, "time_start", required=True), "time_start"
    )
    time_end = time_stamp_parse(
        int_query_parse(query, "time_end", required=True), "time_end"
    )
    limit = int_query_parse(query, "limit", default=RANGE_LIMIT_DEFAULT)

    # inclusive range on the padded sort key: '#...' > plain digits at the low
    # end, '~' > any '#...' at the high end
    key_low = f"{time_start:0{TIME_KEY_DIGITS}d}"
    key_high = f"{time_end:0{TIME_KEY_DIGITS}d}~"
    entries = []
    params = {
        "KeyConditionExpression": (
            Key("timeline_id").eq(timeline_id)
            & Key("time_key").between(key_low, key_high)
        ),
    }
    while len(entries) < limit:
        params["Limit"] = limit - len(entries)
        page = table_timeline_asset.query(**params)
        entries.extend(entry_public(item) for item in page["Items"])
        if "LastEvaluatedKey" not in page:
            break
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return resp_ok({"assets": entries})


def api_timeline_asset_neighbors(user_id, timeline_id, query):
    """up to `count` assets on each requested side of time_point.
    'before' = strictly earlier, nearest first (descending);
    'after' = strictly later, nearest first (ascending);
    entries lying exactly at time_point belong to neither side."""
    timeline_get(user_id, timeline_id)
    time_point = time_stamp_parse(
        int_query_parse(query, "time_point", required=True), "time_point"
    )
    count = int_query_parse(query, "count", default=NEIGHBOR_COUNT_DEFAULT)
    if count < 1 or count > NEIGHBOR_COUNT_MAX:
        raise ApiError(-1, f"count must be between 1 and {NEIGHBOR_COUNT_MAX}")
    direction = query.get("direction", "both")
    if direction not in ("before", "after", "both"):
        raise ApiError(-1, "direction must be 'before', 'after' or 'both'")

    data = {}
    if direction in ("before", "both"):
        page = table_timeline_asset.query(
            KeyConditionExpression=(
                Key("timeline_id").eq(timeline_id)
                & Key("time_key").lt(f"{time_point:0{TIME_KEY_DIGITS}d}")
            ),
            ScanIndexForward=False,
            Limit=count,
        )
        data["before"] = [entry_public(item) for item in page["Items"]]
    if direction in ("after", "both"):
        page = table_timeline_asset.query(
            KeyConditionExpression=(
                Key("timeline_id").eq(timeline_id)
                & Key("time_key").gt(f"{time_point:0{TIME_KEY_DIGITS}d}~")
            ),
            ScanIndexForward=True,
            Limit=count,
        )
        data["after"] = [entry_public(item) for item in page["Items"]]
    return resp_ok(data)


def api_asset_timelines(user_id, asset_id):
    """all timelines of the caller that collect the asset, with the asset's
    time point in each. gsi query gives keys + time point; timeline info is
    fetched from the timeline table in a second step (KEYS_ONLY/INCLUDE
    projection pattern)."""
    entries = []
    params = {
        "IndexName": "gsi_asset_id",
        "KeyConditionExpression": Key("asset_id").eq(asset_id),
    }
    while True:
        page = table_timeline_asset.query(**params)
        entries.extend(item for item in page["Items"] if item["user_id"] == user_id)
        if "LastEvaluatedKey" not in page:
            break
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    found = []
    for entry in entries:
        timeline = table_timeline.get_item(
            Key={"user_id": user_id, "timeline_id": entry["timeline_id"]}
        ).get("Item")
        if timeline is None:
            continue
        found.append({
            "timeline_id": entry["timeline_id"],
            "name": timeline["name"],
            "time_zone": timeline["time_zone"],
            "time_stamp": entry["time_stamp"],
            "time_stamp_timezone": entry["time_stamp_timezone"],
        })
    return resp_ok({"timelines": found})


# ------------------------------------------------------- apis: collect entries


def api_asset_collect(user_id, timeline_id, body):
    timeline = timeline_get(user_id, timeline_id)
    asset_id = (body.get("asset_id") or "").strip()
    if not asset_id:
        raise ApiError(-1, "asset_id is required")
    time_stamp = time_stamp_parse(body.get("time_stamp"), "time_stamp")
    if "time_stamp_timezone" in body:
        time_stamp_timezone = timezone_parse(
            body["time_stamp_timezone"], "time_stamp_timezone"
        )
    else:
        time_stamp_timezone = timeline["time_zone"]

    if collect_entry_find(user_id, timeline_id, asset_id) is not None:
        raise ApiError(
            -1,
            f"asset already collected by this timeline: {asset_id}"
            " (change its time point instead)",
        )

    entry = {
        "timeline_id": timeline_id,
        "time_key": time_key_build(time_stamp, asset_id),
        "asset_id": asset_id,
        "user_id": user_id,
        "time_stamp": time_stamp,
        "time_stamp_timezone": time_stamp_timezone,
    }
    table_timeline_asset.put_item(Item=entry)
    return resp_ok({"entry": entry_public(entry)})


def api_asset_time_change(user_id, timeline_id, asset_id, body):
    timeline_get(user_id, timeline_id)
    time_stamp = time_stamp_parse(body.get("time_stamp"), "time_stamp")

    entry_old = collect_entry_find(user_id, timeline_id, asset_id)
    if entry_old is None:
        raise ApiError(
            -2, f"asset not collected by this timeline: {asset_id}", http_status=404
        )
    if "time_stamp_timezone" in body:
        time_stamp_timezone = timezone_parse(
            body["time_stamp_timezone"], "time_stamp_timezone"
        )
    else:
        time_stamp_timezone = entry_old["time_stamp_timezone"]

    # the time stamp is part of the sort key, so a time change is a new item
    # plus a delete of the old one
    entry_new = {
        "timeline_id": timeline_id,
        "time_key": time_key_build(time_stamp, asset_id),
        "asset_id": asset_id,
        "user_id": user_id,
        "time_stamp": time_stamp,
        "time_stamp_timezone": time_stamp_timezone,
    }
    table_timeline_asset.put_item(Item=entry_new)
    if entry_old["time_key"] != entry_new["time_key"]:
        table_timeline_asset.delete_item(
            Key={"timeline_id": timeline_id, "time_key": entry_old["time_key"]}
        )
    return resp_ok({"entry": entry_public(entry_new)})


def api_asset_remove(user_id, timeline_id, asset_id):
    timeline_get(user_id, timeline_id)
    entry = collect_entry_find(user_id, timeline_id, asset_id)
    if entry is None:
        raise ApiError(
            -2, f"asset not collected by this timeline: {asset_id}", http_status=404
        )
    table_timeline_asset.delete_item(
        Key={"timeline_id": timeline_id, "time_key": entry["time_key"]}
    )
    return resp_ok()
