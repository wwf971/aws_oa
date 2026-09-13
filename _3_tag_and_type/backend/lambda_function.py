# tag / type api, runs in lambda behind api gateway http api.
# api gateway jwt authorizer already verified the cognito token; this code
# resolves user_id from the user table, checks the role (admin/guest) from
# cognito groups, and serves the tag/type apis.
#
# tag and type are almost the same concept for now, but they are kept fully
# independent at data and api level (own tables, own paths). the code is
# shared through a 'kind' descriptor (KIND_TAG / KIND_TYPE) holding the table
# set of one side; if tag and type diverge in the future, the shared function
# is split instead of growing branches.
#
# api list (all responses use {code, data, message}; code 0 = success; every
# 'tag' path below also exists with 'type' instead of 'tag'):
#   GET    /api/me                            role of the caller
#   GET    /api/tag?name=                     list/search own tags by name
#   GET    /api/tag/search?query=&limit=      char-level name search through
#                                              the local es index, results
#                                              carry match positions
#   POST   /api/tag                           create {name, parent_id?,
#                                              is_history_enabled?, time_zone?}
#   GET    /api/tag/{tag_id}                  one tag
#   PATCH  /api/tag/{tag_id}                  update {name?, parent_id? (null =
#                                              move to root), is_history_enabled?,
#                                              time_zone?}
#   DELETE /api/tag/{tag_id}                  delete (refused while it has
#                                              children; detaches it from objs)
#   GET    /api/tag/{tag_id}/children         direct children
#   GET    /api/tag/{tag_id}/ancestor         ancestor chain, nearest first
#   GET    /api/tag/{tag_id}/history?limit=   edit history, newest first
#   GET    /api/tag/{tag_id}/obj              ids of objs the tag is attached to
#   GET    /api/obj/{obj_id}/tag              attached tags in lexorank order
#   POST   /api/obj/{obj_id}/tag              attach {tag_id, lexorank?,
#                                              time_zone?} (no lexorank = append)
#   PATCH  /api/obj/{obj_id}/tag/{tag_id}     reorder {lexorank, time_zone?}
#   DELETE /api/obj/{obj_id}/tag/{tag_id}     detach (?time_zone=)
#   GET    /api/obj/{obj_id}/tag-history?limit=       obj tag edit history
#   DELETE /api/obj/{obj_id}/tag-history?before=      delete history earlier
#                                                      than the unix ms point
#
# history design: a tag/type logs its own edits only while its
# is_history_enabled is on; the history record is written in the SAME dynamodb
# transaction as the edit, so a failed history write fails the edit itself.
# toggling is_history_enabled off removes all history of that tag/type.
# obj attach/reorder/detach history is always recorded (same-transaction too).
#
# time storage (see time-format.md): time stamps are unix milliseconds stored
# as numbers, each accompanied by a timezone attribute in signed minutes
# (e.g. +09:00 -> 540). obj ids are plain references, not validated against
# the sub-project owning the obj.
#
# name index: tag and type names are char-level searchable through the local
# es service of _2_local_es (one separate index per side). every write api
# runs its index operation first and only touches dynamodb after the index
# operation is confirmed, refer to the 'name index' comment block below.
# es_client.py is the aws-side library of _2_local_es, packaged into this
# lambda's zip by ensure_architect.py.

import base64
import json
import os
import secrets
import string
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from es_client import EsClient

TABLE_USER = os.environ["TABLE_USER"]
GROUP_ACCESS = os.environ["GROUP_ACCESS"]
GROUP_ADMIN = os.environ["GROUP_ADMIN"]

# pointer to the local es service (task queue + result table of _2_local_es)
# and the name indices of this service, all provided by ensure_architect.py
ES_QUEUE_URL = os.environ["ES_QUEUE_URL"]
ES_RESULT_TABLE = os.environ["ES_RESULT_TABLE"]
ES_REGION = os.environ["ES_REGION"]
ES_RESULT_TIMEOUT_SEC = float(os.environ["ES_RESULT_TIMEOUT_SEC"])
ES_RESULT_POLL_SEC = 0.25
ES_INDEX_CONFIG_NAME = "char"

ID_LENGTH = 16
ID_CHARS = string.digits + string.ascii_lowercase

# lexorank: order of the tags/types attached to one obj. plain string compare
# of ranks gives the display order; ranks never end with '0', so a gap always
# exists below every rank.
RANK_CHARS = string.digits + string.ascii_lowercase

# sort key of the history tables: zero-padded time stamp + '#' + random chars
# (keeps the key unique when two records land on the same millisecond). plain
# string order of the key equals time order.
TIME_KEY_DIGITS = 16
TIME_KEY_RANDOM_LENGTH = 4

TIMEZONE_MINUTES_MAX = 14 * 60
HISTORY_LIMIT_DEFAULT = 100
SEARCH_LIMIT_DEFAULT = 100
ANCESTOR_DEPTH_MAX = 100

dynamodb = boto3.resource("dynamodb")
dynamodb_client = boto3.client("dynamodb")
serializer = TypeSerializer()

table_user = dynamodb.Table(TABLE_USER)

# requester of the local es service: sends one task into the task queue of
# _2_local_es, then polls its result table for the worker's confirmation.
# the boto3 clients carry no explicit keys, they use this lambda's role.
es_client = EsClient(
    sqs=boto3.client("sqs", region_name=ES_REGION),
    db=boto3.client("dynamodb", region_name=ES_REGION),
    queue_url=ES_QUEUE_URL,
    table_name=ES_RESULT_TABLE,
    result_timeout_sec=ES_RESULT_TIMEOUT_SEC,
    result_poll_sec=ES_RESULT_POLL_SEC,
)


def kind_build(kind_name):
    """table set + name index of one side (tag or type). all shared code
    receives one of these descriptors and never touches the other side's
    tables or index."""
    key = kind_name.upper()
    names = {
        "entity": os.environ[f"TABLE_{key}"],
        "history": os.environ[f"TABLE_{key}_HISTORY"],
        "obj": os.environ[f"TABLE_OBJ_{key}"],
        "obj_history": os.environ[f"TABLE_OBJ_{key}_HISTORY"],
    }
    return {
        "kind": kind_name,
        "id_attr": f"{kind_name}_id",
        "gsi_id": f"gsi_{kind_name}_id",
        "index_name": os.environ[f"INDEX_{key}"],
        "table_entity_name": names["entity"],
        "table_history_name": names["history"],
        "table_obj_name": names["obj"],
        "table_obj_history_name": names["obj_history"],
        "table_entity": dynamodb.Table(names["entity"]),
        "table_history": dynamodb.Table(names["history"]),
        "table_obj": dynamodb.Table(names["obj"]),
        "table_obj_history": dynamodb.Table(names["obj_history"]),
    }


KIND_TAG = kind_build("tag")
KIND_TYPE = kind_build("type")
KIND_BY_NAME = {"tag": KIND_TAG, "type": KIND_TYPE}

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

    if method == "GET" and parts[1:] == ["me"]:
        return api_me(user_id, claims, role)

    # every non-GET api writes, and writes are admin only
    if method != "GET" and role != "admin":
        return resp(403, -3, message="this operation needs admin role")

    # /api/tag/... and /api/type/...
    if parts[1] in KIND_BY_NAME:
        return route_kind(KIND_BY_NAME[parts[1]], method, parts[2:], query, body, user_id)
    # /api/obj/{obj_id}/...
    if parts[1] == "obj" and len(parts) >= 4:
        return route_obj(parts[2], method, parts[3:], query, body, user_id)
    raise ApiError(-2, f"unknown api: {method} {path}", http_status=404)


def route_kind(kind, method, rest, query, body, user_id):
    if not rest:
        if method == "GET":
            return api_entity_list(kind, user_id, query)
        if method == "POST":
            return api_entity_create(kind, user_id, body)
    # 'search' is a reserved path word, it can never collide with an entity
    # id (ids are 16 random chars of 0-9 a-z)
    elif rest == ["search"] and method == "GET":
        return api_entity_search(kind, user_id, query)
    elif len(rest) == 1:
        entity_id = rest[0]
        if method == "GET":
            return api_entity_get(kind, user_id, entity_id)
        if method == "PATCH":
            return api_entity_update(kind, user_id, entity_id, body)
        if method == "DELETE":
            return api_entity_delete(kind, user_id, entity_id)
    elif len(rest) == 2 and method == "GET":
        entity_id, section = rest
        if section == "children":
            return api_entity_children(kind, user_id, entity_id)
        if section == "ancestor":
            return api_entity_ancestors(kind, user_id, entity_id)
        if section == "history":
            return api_entity_history(kind, user_id, entity_id, query)
        if section == "obj":
            return api_entity_objs(kind, user_id, entity_id)
    raise ApiError(
        -2, f"unknown api: {method} /api/{kind['kind']}/{'/'.join(rest)}",
        http_status=404,
    )


def route_obj(obj_id, method, rest, query, body, user_id):
    for kind in (KIND_TAG, KIND_TYPE):
        if rest[0] == kind["kind"]:
            if len(rest) == 1:
                if method == "GET":
                    return api_obj_entries(kind, user_id, obj_id)
                if method == "POST":
                    return api_obj_attach(kind, user_id, obj_id, body)
            if len(rest) == 2:
                if method == "PATCH":
                    return api_obj_rank_change(kind, user_id, obj_id, rest[1], body)
                if method == "DELETE":
                    return api_obj_detach(kind, user_id, obj_id, rest[1], query)
        if rest[0] == f"{kind['kind']}-history" and len(rest) == 1:
            if method == "GET":
                return api_obj_history(kind, user_id, obj_id, query)
            if method == "DELETE":
                return api_obj_history_delete(kind, user_id, obj_id, query)
    raise ApiError(
        -2, f"unknown api: {method} /api/obj/{obj_id}/{'/'.join(rest)}",
        http_status=404,
    )


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


def now_ms():
    return int(time.time() * 1000)


def time_key_build(time_stamp):
    suffix = "".join(secrets.choice(ID_CHARS) for _ in range(TIME_KEY_RANDOM_LENGTH))
    return f"{time_stamp:0{TIME_KEY_DIGITS}d}#{suffix}"


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


def rank_parse(value):
    if not isinstance(value, str) or not value:
        raise ApiError(-1, "lexorank must be a non-empty string of chars 0-9 a-z")
    if any(char not in RANK_CHARS for char in value):
        raise ApiError(-1, f"lexorank contains an invalid character: {value}")
    if value.endswith("0"):
        raise ApiError(-1, "lexorank must not end with '0' (keeps a gap below every rank)")
    return value


def rank_between(rank_low, rank_high):
    """a rank strictly between the two given ranks in plain string order,
    never ending with '0'. empty rank_low means 'before everything', empty
    rank_high means 'after everything'."""
    if rank_high and rank_low >= rank_high:
        raise ApiError(-1, f"lexorank order broken: {rank_low!r} >= {rank_high!r}")
    result = ""
    position = 0
    while True:
        digit_low = RANK_CHARS.index(rank_low[position]) if position < len(rank_low) else 0
        digit_high = RANK_CHARS.index(rank_high[position]) if position < len(rank_high) else len(RANK_CHARS)
        if digit_high - digit_low > 1:
            return result + RANK_CHARS[(digit_low + digit_high) // 2]
        # digits equal or adjacent: keep the low digit and go one char deeper
        result += RANK_CHARS[digit_low]
        position += 1


def item_typed(item):
    """resource-layer item -> typed attribute values of the low-level client
    (transact_write_items only exists on the low-level client)."""
    return {key: serializer.serialize(value) for key, value in item.items()}


def transact_put(table_name, item):
    return {"Put": {"TableName": table_name, "Item": item_typed(item)}}


def transact_delete(table_name, key):
    return {"Delete": {"TableName": table_name, "Key": item_typed(key)}}


# ------------------------------------------------------- name index (local es)
#
# each side owns one char-level index on the local es service, holding one doc
# per tag/type: {name, user_id}, keyed by the entity id. only the name is
# searched; user_id is an exact filter keeping every search inside the
# caller's own data.
#
# the dynamodb tables are the source of truth. a doc in the index whose entity
# is missing or stale in dynamodb is harmless, because search results are
# resolved through dynamodb before being returned. but an entity in dynamodb
# that is NOT indexed would silently never appear in char search. so every
# write api runs its index operation FIRST, and only touches dynamodb after
# the worker confirmed the index operation: create/rename/delete fail without
# changing dynamodb when the index is not updated (worker down, sqs message
# not consumed, no confirmation before the timeout, ...).


def index_doc_put(kind, user_id, entity_id, name):
    result = es_client.doc_put(
        kind["index_name"], entity_id, {"name": name, "user_id": user_id}
    )
    index_result_check(kind, result)


def index_doc_delete(kind, entity_id):
    result = es_client.doc_delete(kind["index_name"], entity_id)
    index_result_check(kind, result)


def index_result_check(kind, result):
    if result["code"] != 0:
        raise ApiError(
            -6,
            f"name index of {kind['kind']} not updated: {result.get('message', '')}",
            http_status=502,
        )


# ------------------------------------------------- entity storage (tag / type)


def entity_get(kind, user_id, entity_id):
    item = kind["table_entity"].get_item(
        Key={"user_id": user_id, kind["id_attr"]: entity_id}
    ).get("Item")
    if item is None:
        raise ApiError(-2, f"{kind['kind']} not found: {entity_id}", http_status=404)
    return item


def entity_list_all(kind, user_id):
    """all tags/types of the user: one partition query. a user's tag/type
    count is small, so children lookups and name search filter in code."""
    entities = []
    params = {"KeyConditionExpression": Key("user_id").eq(user_id)}
    while True:
        page = kind["table_entity"].query(**params)
        entities.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            break
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return entities


def children_list(kind, user_id, entity_id):
    return [
        entity for entity in entity_list_all(kind, user_id)
        if entity.get("parent_id") == entity_id
    ]


def ancestor_walk(kind, user_id, entity):
    """ancestors of the entity, nearest first, walking up parent_id."""
    found = []
    current = entity
    while current.get("parent_id") is not None:
        if len(found) >= ANCESTOR_DEPTH_MAX:
            raise ApiError(-5, "parent chain too deep or broken", http_status=500)
        parent = kind["table_entity"].get_item(
            Key={"user_id": user_id, kind["id_attr"]: current["parent_id"]}
        ).get("Item")
        if parent is None:
            break
        found.append(parent)
        current = parent
    return found


def parent_check(kind, user_id, entity_id, parent_id):
    """the new parent must exist, and the entity itself must not appear on the
    new parent's ancestor chain: this is what prohibits circular parent-child
    relationships."""
    if parent_id == entity_id:
        raise ApiError(-1, f"a {kind['kind']} can not be its own parent")
    parent = entity_get(kind, user_id, parent_id)
    ancestor_id_list = [item[kind["id_attr"]] for item in ancestor_walk(kind, user_id, parent)]
    if entity_id in ancestor_id_list:
        raise ApiError(
            -1,
            f"circular parent-child relationship is prohibited: {parent_id} is "
            f"a descendant of {entity_id}",
        )


def entity_save(kind, entity, user_id, operation, detail, time_zone):
    """put the entity item; while its history logging is on, the history
    record goes into the SAME transaction, so a failed history write also
    fails the edit itself."""
    action_list = [transact_put(kind["table_entity_name"], entity)]
    if entity.get("is_history_enabled"):
        action_list.append(transact_put(kind["table_history_name"], {
            kind["id_attr"]: entity[kind["id_attr"]],
            "time_key": time_key_build(entity["modify_at"]),
            "user_id": user_id,
            "operation": operation,
            "detail": detail,
            "time_stamp": entity["modify_at"],
            "time_stamp_timezone": time_zone,
        }))
    dynamodb_client.transact_write_items(TransactItems=action_list)


def history_wipe(kind, entity_id):
    """remove ALL history records of the entity (used when is_history_enabled
    is toggled off, and when the entity is deleted)."""
    key_list = []
    params = {"KeyConditionExpression": Key(kind["id_attr"]).eq(entity_id)}
    while True:
        page = kind["table_history"].query(**params)
        key_list.extend(
            {kind["id_attr"]: item[kind["id_attr"]], "time_key": item["time_key"]}
            for item in page["Items"]
        )
        if "LastEvaluatedKey" not in page:
            break
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    with kind["table_history"].batch_writer() as batch:
        for key in key_list:
            batch.delete_item(Key=key)


# ------------------------------------------- obj attach storage (shared shape)


def obj_entry_list(kind, user_id, obj_id):
    """attach entries of the obj (the caller's only), sorted by lexorank."""
    entries = []
    params = {"KeyConditionExpression": Key("obj_id").eq(obj_id)}
    while True:
        page = kind["table_obj"].query(**params)
        entries.extend(item for item in page["Items"] if item["user_id"] == user_id)
        if "LastEvaluatedKey" not in page:
            break
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    entries.sort(key=lambda entry: entry["lexorank"])
    return entries


def entity_obj_entries(kind, entity_id):
    """all (obj, entity) attach entries of one tag/type, via the gsi. the
    INCLUDE projection carries user_id, enough for detach and for listing
    obj ids."""
    entries = []
    params = {
        "IndexName": kind["gsi_id"],
        "KeyConditionExpression": Key(kind["id_attr"]).eq(entity_id),
    }
    while True:
        page = kind["table_obj"].query(**params)
        entries.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            break
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return entries


def obj_history_put_build(kind, obj_id, user_id, operation, entity_id, time_stamp, time_zone, lexorank=None):
    record = {
        "obj_id": obj_id,
        "time_key": time_key_build(time_stamp),
        "user_id": user_id,
        "operation": operation,
        kind["id_attr"]: entity_id,
        "time_stamp": time_stamp,
        "time_stamp_timezone": time_zone,
    }
    if lexorank is not None:
        record["lexorank"] = lexorank
    return transact_put(kind["table_obj_history_name"], record)


def obj_detach_write(kind, user_id, obj_id, entity_id, time_zone):
    """delete the attach entry; the detach history record goes into the same
    transaction."""
    time_stamp = now_ms()
    dynamodb_client.transact_write_items(TransactItems=[
        transact_delete(kind["table_obj_name"], {"obj_id": obj_id, kind["id_attr"]: entity_id}),
        obj_history_put_build(kind, obj_id, user_id, "detach", entity_id, time_stamp, time_zone),
    ])


def entry_public(kind, entry):
    return {
        kind["id_attr"]: entry[kind["id_attr"]],
        "lexorank": entry["lexorank"],
        "create_at": entry["create_at"],
        "create_at_timezone": entry["create_at_timezone"],
    }


# -------------------------------------------------------- apis: entity (kind)


def api_me(user_id, claims, role):
    return resp_ok({
        "user_id": user_id,
        "username": claims.get("cognito:username") or claims.get("username"),
        "email": claims.get("email"),
        "role": role,
    })


def api_entity_list(kind, user_id, query):
    """all tags/types of the user; ?name= narrows by case-insensitive
    substring."""
    entities = entity_list_all(kind, user_id)
    name_query = (query.get("name") or "").strip().lower()
    if name_query:
        entities = [
            entity for entity in entities
            if name_query in entity["name"].lower()
        ]
    return resp_ok({f"{kind['kind']}s": entities})


def api_entity_search(kind, user_id, query):
    """char-level name search through the local es index: the query text
    matches any substring of a name, case-insensitive. each result is the
    entity plus match_list ([{field, index_start, index_end}] over the name)
    for highlighting."""
    text = (query.get("query") or "").strip()
    if not text:
        raise ApiError(-1, "query parameter 'query' is required")
    limit = int_query_parse(query, "limit", default=SEARCH_LIMIT_DEFAULT)
    result = es_client.search(
        kind["index_name"], ES_INDEX_CONFIG_NAME, text, ["name"],
        filter_exact={"user_id": user_id}, limit=limit,
    )
    if result["code"] != 0:
        raise ApiError(
            -6,
            f"name index search failed: {result.get('message', '')}",
            http_status=502,
        )
    found = []
    for hit in result["data"]:
        item = kind["table_entity"].get_item(
            Key={"user_id": user_id, kind["id_attr"]: hit["doc_id"]}
        ).get("Item")
        # the index can be momentarily ahead of dynamodb (e.g. a create whose
        # dynamodb write failed after the doc was indexed); results are
        # resolved through dynamodb and such docs are dropped
        if item is None:
            continue
        found.append({**item, "match_list": hit["match_list"]})
    return resp_ok({"results": found})


def api_entity_get(kind, user_id, entity_id):
    return resp_ok({kind["kind"]: entity_get(kind, user_id, entity_id)})


def api_entity_create(kind, user_id, body):
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError(-1, "name is required")
    is_history_enabled = body.get("is_history_enabled", False)
    if not isinstance(is_history_enabled, bool):
        raise ApiError(-1, "is_history_enabled must be a boolean")
    time_zone = timezone_parse(body.get("time_zone", 0), "time_zone")
    parent_id = body.get("parent_id")
    if parent_id is not None:
        # parent must exist; a freshly generated id can not create a cycle
        entity_get(kind, user_id, parent_id)

    time_stamp = now_ms()
    entity_id = id_generate()
    entity = {
        "user_id": user_id,
        kind["id_attr"]: entity_id,
        "name": name,
        "is_history_enabled": is_history_enabled,
        "create_at": time_stamp,
        "create_at_timezone": time_zone,
        "modify_at": time_stamp,
        "modify_at_timezone": time_zone,
    }
    detail = {"name": name}
    if parent_id is not None:
        entity["parent_id"] = parent_id
        detail["parent_id"] = parent_id
    # index first: when the name is not confirmed indexed, the creation
    # itself fails and dynamodb is never written (see the name index block)
    index_doc_put(kind, user_id, entity_id, name)
    entity_save(kind, entity, user_id, "create", detail, time_zone)
    return resp_ok({kind["kind"]: entity})


def api_entity_update(kind, user_id, entity_id, body):
    entity = entity_get(kind, user_id, entity_id)
    was_history_enabled = bool(entity.get("is_history_enabled"))

    detail = {}
    if "name" in body:
        name = (body["name"] or "").strip()
        if not name:
            raise ApiError(-1, "name must not be empty")
        entity["name"] = name
        detail["name"] = name
    if "parent_id" in body:
        parent_id = body["parent_id"]
        if parent_id is None:
            entity.pop("parent_id", None)
        else:
            parent_check(kind, user_id, entity_id, parent_id)
            entity["parent_id"] = parent_id
        detail["parent_id"] = parent_id
    if "is_history_enabled" in body:
        if not isinstance(body["is_history_enabled"], bool):
            raise ApiError(-1, "is_history_enabled must be a boolean")
        entity["is_history_enabled"] = body["is_history_enabled"]
        detail["is_history_enabled"] = body["is_history_enabled"]
    if not detail:
        raise ApiError(-1, "body must contain 'name', 'parent_id' and/or 'is_history_enabled'")

    time_zone = timezone_parse(body.get("time_zone", 0), "time_zone")
    entity["modify_at"] = now_ms()
    entity["modify_at_timezone"] = time_zone
    # a name change re-indexes first: when the new name is not confirmed
    # indexed, the update fails and dynamodb keeps the old name
    if "name" in detail:
        index_doc_put(kind, user_id, entity_id, entity["name"])
    # while logging is on the edit and its history record share one
    # transaction; the toggle-on edit itself is the first logged record
    entity_save(kind, entity, user_id, "update", detail, time_zone)
    if was_history_enabled and not entity["is_history_enabled"]:
        history_wipe(kind, entity_id)
    return resp_ok({kind["kind"]: entity})


def api_entity_delete(kind, user_id, entity_id):
    entity_get(kind, user_id, entity_id)
    child_list = children_list(kind, user_id, entity_id)
    if child_list:
        raise ApiError(
            -1,
            f"{kind['kind']} still has {len(child_list)} child(ren), "
            "move or delete them first",
        )

    # index first: a failed doc delete fails the whole delete while both the
    # doc and the entity are still in place, and a retry simply runs the doc
    # delete again (a no-op when the doc is already gone)
    index_doc_delete(kind, entity_id)

    # detach from every obj first; each detach logs to that obj's history
    entry_list = entity_obj_entries(kind, entity_id)
    for entry in entry_list:
        obj_detach_write(kind, entry["user_id"], entry["obj_id"], entity_id, 0)

    # history of a deleted tag/type is dropped together with the entity
    history_wipe(kind, entity_id)
    kind["table_entity"].delete_item(Key={"user_id": user_id, kind["id_attr"]: entity_id})
    return resp_ok({"obj_detach_count": len(entry_list)})


def api_entity_children(kind, user_id, entity_id):
    entity_get(kind, user_id, entity_id)
    return resp_ok({"children": children_list(kind, user_id, entity_id)})


def api_entity_ancestors(kind, user_id, entity_id):
    entity = entity_get(kind, user_id, entity_id)
    return resp_ok({"ancestors": ancestor_walk(kind, user_id, entity)})


def api_entity_history(kind, user_id, entity_id, query):
    entity_get(kind, user_id, entity_id)
    limit = int_query_parse(query, "limit", default=HISTORY_LIMIT_DEFAULT)
    records = []
    params = {
        "KeyConditionExpression": Key(kind["id_attr"]).eq(entity_id),
        "ScanIndexForward": False,
    }
    while len(records) < limit:
        params["Limit"] = limit - len(records)
        page = kind["table_history"].query(**params)
        records.extend(
            {key: value for key, value in item.items() if key != "time_key"}
            for item in page["Items"]
        )
        if "LastEvaluatedKey" not in page:
            break
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return resp_ok({"history": records})


def api_entity_objs(kind, user_id, entity_id):
    entity_get(kind, user_id, entity_id)
    obj_ids = [
        entry["obj_id"] for entry in entity_obj_entries(kind, entity_id)
        if entry["user_id"] == user_id
    ]
    return resp_ok({"obj_ids": obj_ids})


# --------------------------------------------------- apis: obj attach entries


def api_obj_entries(kind, user_id, obj_id):
    entries = obj_entry_list(kind, user_id, obj_id)
    return resp_ok({"entries": [entry_public(kind, entry) for entry in entries]})


def api_obj_attach(kind, user_id, obj_id, body):
    entity_id = (body.get(kind["id_attr"]) or "").strip()
    if not entity_id:
        raise ApiError(-1, f"{kind['id_attr']} is required")
    entity_get(kind, user_id, entity_id)

    existing = kind["table_obj"].get_item(
        Key={"obj_id": obj_id, kind["id_attr"]: entity_id}
    ).get("Item")
    if existing is not None:
        raise ApiError(-1, f"{kind['kind']} already attached to this obj: {entity_id}")

    if "lexorank" in body:
        lexorank = rank_parse(body["lexorank"])
    else:
        # no rank given: append after the current last entry
        entries = obj_entry_list(kind, user_id, obj_id)
        rank_last = entries[-1]["lexorank"] if entries else ""
        lexorank = rank_between(rank_last, "")

    time_zone = timezone_parse(body.get("time_zone", 0), "time_zone")
    time_stamp = now_ms()
    entry = {
        "obj_id": obj_id,
        kind["id_attr"]: entity_id,
        "user_id": user_id,
        "lexorank": lexorank,
        "create_at": time_stamp,
        "create_at_timezone": time_zone,
    }
    dynamodb_client.transact_write_items(TransactItems=[
        transact_put(kind["table_obj_name"], entry),
        obj_history_put_build(
            kind, obj_id, user_id, "attach", entity_id, time_stamp, time_zone,
            lexorank=lexorank,
        ),
    ])
    return resp_ok({"entry": entry_public(kind, entry)})


def api_obj_rank_change(kind, user_id, obj_id, entity_id, body):
    entry = kind["table_obj"].get_item(
        Key={"obj_id": obj_id, kind["id_attr"]: entity_id}
    ).get("Item")
    if entry is None or entry["user_id"] != user_id:
        raise ApiError(
            -2, f"{kind['kind']} not attached to this obj: {entity_id}",
            http_status=404,
        )
    if "lexorank" not in body:
        raise ApiError(-1, "body must contain 'lexorank'")
    entry["lexorank"] = rank_parse(body["lexorank"])

    time_zone = timezone_parse(body.get("time_zone", 0), "time_zone")
    time_stamp = now_ms()
    dynamodb_client.transact_write_items(TransactItems=[
        transact_put(kind["table_obj_name"], entry),
        obj_history_put_build(
            kind, obj_id, user_id, "reorder", entity_id, time_stamp, time_zone,
            lexorank=entry["lexorank"],
        ),
    ])
    return resp_ok({"entry": entry_public(kind, entry)})


def api_obj_detach(kind, user_id, obj_id, entity_id, query):
    entry = kind["table_obj"].get_item(
        Key={"obj_id": obj_id, kind["id_attr"]: entity_id}
    ).get("Item")
    if entry is None or entry["user_id"] != user_id:
        raise ApiError(
            -2, f"{kind['kind']} not attached to this obj: {entity_id}",
            http_status=404,
        )
    time_zone = timezone_parse(query.get("time_zone", 0), "time_zone")
    obj_detach_write(kind, user_id, obj_id, entity_id, time_zone)
    return resp_ok()


# ----------------------------------------------------- apis: obj edit history


def api_obj_history(kind, user_id, obj_id, query):
    limit = int_query_parse(query, "limit", default=HISTORY_LIMIT_DEFAULT)
    records = []
    params = {
        "KeyConditionExpression": Key("obj_id").eq(obj_id),
        "ScanIndexForward": False,
    }
    while len(records) < limit:
        page = kind["table_obj_history"].query(**params)
        records.extend(
            {key: value for key, value in item.items() if key not in ("obj_id", "time_key")}
            for item in page["Items"]
            if item["user_id"] == user_id
        )
        if "LastEvaluatedKey" not in page:
            break
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return resp_ok({"history": records[:limit]})


def api_obj_history_delete(kind, user_id, obj_id, query):
    """delete obj edit history records EARLIER than the given time point.
    deleting a time range or specific records is not supported."""
    before = time_stamp_parse(
        int_query_parse(query, "before", required=True), "before"
    )
    key_list = []
    params = {
        "KeyConditionExpression": (
            Key("obj_id").eq(obj_id)
            & Key("time_key").lt(f"{before:0{TIME_KEY_DIGITS}d}")
        ),
    }
    while True:
        page = kind["table_obj_history"].query(**params)
        key_list.extend(
            {"obj_id": item["obj_id"], "time_key": item["time_key"]}
            for item in page["Items"]
            if item["user_id"] == user_id
        )
        if "LastEvaluatedKey" not in page:
            break
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    with kind["table_obj_history"].batch_writer() as batch:
        for key in key_list:
            batch.delete_item(Key=key)
    return resp_ok({"history_count_deleted": len(key_list)})
