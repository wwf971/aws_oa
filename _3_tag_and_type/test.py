# test of the tag/type service:
#
#   backend  (default) needs no deployed architecture: creates a TEMPORARY
#            stack from zero (two temp name indices on the local es service,
#            all eight dynamodb tables, lambda role, lambda) under the prefix
#            {prefix}-temp-{timestamp}, runs the tag tree / history / obj
#            attach flow, the char-level name search flow, an
#            index-confirmation failure check, plus a type independence check
#            through the temp lambda, then removes every temp resource. this
#            reproduces ensurement, operation and removal of the architecture.
#   api      checks that the DEPLOYED http api rejects requests without a
#            cognito jwt; requires ensure_architect.py to have been run
#            (reads the api endpoint from config_gen.yaml).
#
# external dependencies of both items: _0_auth_cognito must be deployed
# (cognito user pool with an admin user, user table with its user_id mapping).
# the backend item additionally needs _2_local_es deployed and its worker on
# the home server running (the temp indices live on the local elasticsearch).
# obj ids in this service are plain references (not validated against the
# sub-project owning the obj), so the test uses generated fake obj ids.
#
# run the default item:  python test.py
# run selected items:    python test.py api backend
# clean test residue:     python test.py --clean
# clean old-prefix residue:
#                         python test.py --clean --assume-prefix old-prefix

import argparse
import json
import re
import secrets
import string
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aws_utils import (
    TestFail,
    check,
    check_count,
    lambda_delete,
    lambda_role_delete,
    step,
    table_delete,
    timestamp_resource_make,
)
from config_gen import (
    TABLE_KEY_LIST,
    aws_client_make,
    cognito_gen_load,
    config_gen_load,
    config_load,
    index_names_build,
    local_es_client_make,
    local_es_gen_load,
    names_build,
)
from ensure_architect import (
    api_lambda_ensure,
    api_role_ensure,
    indices_delete,
    indices_ensure,
    lambda_env_build,
    tag_type_tables_ensure,
)

ITEM_LIST = ["api", "backend"]
ITEM_LIST_DEFAULT = ["backend"]

TIMEZONE_TEST = 540  # +09:00 in minutes

# table name endings of this service, used by --clean to recognize residue
TABLE_SUFFIX_LIST = (
    "-tag", "-tag-history", "-obj-tag", "-obj-tag-history",
    "-type", "-type-history", "-obj-type", "-obj-type-history",
)

# temp timestamp inside an aws residue name, e.g. '...-temp-20260913_23015944p09--tag'.
# --clean recovers the timestamps of failed runs from it, to also delete the
# temp name indices of those runs (the local es service has no index list
# api, so index residue is only findable through the aws residue names)
TIMESTAMP_TEMP_RE = re.compile(r"-temp-(\d{8}_\d{8}[pm]\d{2})")


def test_run(args):
    config = config_load()
    if args.assume_prefix is not None:
        config["name_prefix"] = args.assume_prefix
    if args.clean:
        test_resources_clean(config)
        return

    item_list = args.items or ITEM_LIST_DEFAULT
    if "api" in item_list:
        test_api()
    if "backend" in item_list:
        test_backend(config)
    print(f"\nall {check_count()} checks passed")


# ------------------------------------------------------------------ item: api


def test_api():
    step("api: deployed http api rejects a request without a jwt")
    config_gen = config_gen_load().get("tag_type")
    if config_gen is None:
        raise SystemExit(
            "tag_type not found in config_gen.yaml: the api item tests "
            "the deployed http api, run ensure_architect.py first"
        )
    try:
        urlopen(f"{config_gen['api_endpoint']}/api/tag", timeout=30)
        status = 200
    except HTTPError as error:
        status = error.code
    check(status in (401, 403), "/api/tag rejects a request without a jwt")


# -------------------------------------------------------------- item: backend


def test_backend(config):
    """create temp architecture from zero -> run the api flow -> remove it."""
    timestamp = timestamp_resource_make()
    prefix_temp = f"{config['name_prefix']}-temp-{timestamp}"
    names = names_build({"name_prefix": prefix_temp})
    names["table_user"] = cognito_gen_load()["user_table"]["table_name"]
    # throwaway name indices of this run, e.g. 3_tag_temp_20260913_23015944p09
    index_names = index_names_build(f"_temp_{timestamp}")

    es_gen = local_es_gen_load()
    es = local_es_client_make(config, es_gen)
    dynamodb = aws_client_make(config, "dynamodb")
    iam = aws_client_make(config, "iam")
    lambda_client = aws_client_make(config, "lambda")
    sts = aws_client_make(config, "sts")
    cognito = aws_client_make(config, "cognito-idp")

    temp_resources_absent_check(dynamodb, iam, lambda_client, es, names, index_names)
    claims = admin_claims_get(config, cognito)

    try:
        step(f"backend: create temp architecture, prefix {prefix_temp}")
        region = config["aws"]["region_name"]
        account_id = sts.get_caller_identity()["Account"]
        indices_ensure(es, index_names)
        tag_type_tables_ensure(dynamodb, names)
        role_arn = api_role_ensure(iam, names, region, account_id, es_gen)
        api_lambda_ensure(
            lambda_client, names, config, role_arn, es_gen, index_names
        )
        lambda_client.get_waiter("function_active_v2").wait(
            FunctionName=names["lambda_function"]
        )

        test_api_flow(lambda_client, names["lambda_function"], claims)
        test_index_fail_flow(
            lambda_client, names, config, es_gen, index_names, claims
        )
    finally:
        step("backend: remove temp architecture")
        error_list = temp_resource_set_delete(
            dynamodb, iam, lambda_client, es, names, index_names
        )
        if error_list and sys.exc_info()[0] is None:
            raise error_list[0]


def temp_resource_set_delete(dynamodb, iam, lambda_client, es, names, index_names):
    """Try every delete even if one delete fails, so one failure does not
    prevent cleanup of the remaining resource objects."""
    error_list = []

    # the temp indices first: while their deletion fails (typically the es
    # worker being down), keeping the aws residue discoverable by --clean also
    # keeps THEM discoverable (--clean derives index names from aws residue)
    for index_name in (index_names["index_tag"], index_names["index_type"]):
        try:
            result = es.index_delete(index_name)
            if result["code"] != 0:
                raise TestFail(result.get("message", "index delete failed"))
            print(f"index deleted: {index_name}")
        except Exception as error:
            error_list.append(error)
            print(f"cleanup failed for index {index_name}: {error}")

    operation_list = [
        (lambda_delete, lambda_client, names["lambda_function"]),
        (lambda_role_delete, iam, names["lambda_role"]),
    ]
    for table_key in TABLE_KEY_LIST:
        operation_list.append((table_delete, dynamodb, names[table_key]))
    for operation, client, resource_name in operation_list:
        try:
            operation(client, resource_name)
        except Exception as error:
            error_list.append(error)
            print(f"cleanup failed for {resource_name}: {error}")
    return error_list


def test_resources_clean(config):
    """Locate test resources by the configured prefix + '-temp-' marker and
    attempt to remove all of them. Only resource types created by the backend
    test are considered. Temp name indices are located indirectly: their
    timestamps are recovered from the aws residue names (the local es service
    has no index list api), so an index whose aws residue is already fully
    removed cannot be found here; delete such an index via
    _2_local_es/es_client.py index-delete."""
    prefix = config["name_prefix"]
    name_marker = f"{prefix}-temp-"
    dynamodb = aws_client_make(config, "dynamodb")
    iam = aws_client_make(config, "iam")
    lambda_client = aws_client_make(config, "lambda")

    function_name_list = [
        function["FunctionName"]
        for page in lambda_client.get_paginator("list_functions").paginate()
        for function in page["Functions"]
        if function["FunctionName"].startswith(name_marker)
        and function["FunctionName"].endswith("-api")
    ]
    role_name_list = [
        role["RoleName"]
        for page in iam.get_paginator("list_roles").paginate()
        for role in page["Roles"]
        if role["RoleName"].startswith(name_marker)
        and role["RoleName"].endswith("-api-role")
    ]
    table_name_list = [
        table_name
        for page in dynamodb.get_paginator("list_tables").paginate()
        for table_name in page["TableNames"]
        if table_name.startswith(name_marker)
        and table_name.endswith(TABLE_SUFFIX_LIST)
    ]

    resource_count = (
        len(function_name_list) + len(role_name_list) + len(table_name_list)
    )
    step(f"clean test resources matching {name_marker}*")
    if resource_count == 0:
        print("  no matching test resources found")
        return

    print(f"  found {resource_count} resource(s)")
    error_list = []

    # temp indices of the found runs first, refer to temp_resource_set_delete
    es = local_es_client_make(config, local_es_gen_load())
    timestamp_set = set()
    for name in function_name_list + role_name_list + table_name_list:
        match = TIMESTAMP_TEMP_RE.search(name)
        if match is not None:
            timestamp_set.add(match.group(1))
    for timestamp in sorted(timestamp_set):
        index_names = index_names_build(f"_temp_{timestamp}")
        for index_name in (index_names["index_tag"], index_names["index_type"]):
            try:
                result = es.index_delete(index_name)
                if result["code"] != 0:
                    raise TestFail(result.get("message", "index delete failed"))
                if result["data"]["is_deleted"]:
                    print(f"index deleted: {index_name}")
            except Exception as error:
                error_list.append(error)
                print(f"cleanup failed for index {index_name}: {error}")

    for operation, client, resource_name_list in [
        (lambda_delete, lambda_client, function_name_list),
        (lambda_role_delete, iam, role_name_list),
        (table_delete, dynamodb, table_name_list),
    ]:
        for resource_name in resource_name_list:
            try:
                operation(client, resource_name)
            except Exception as error:
                error_list.append(error)
                print(f"cleanup failed for {resource_name}: {error}")
    if error_list:
        raise error_list[0]
    print("  all matching test resources removed or scheduled for deletion")


def temp_resources_absent_check(dynamodb, iam, lambda_client, es, names, index_names):
    """suspend the test if a resource instance with the name and type of a
    temp resource object to be created already exists. the index check also
    proves the es worker is reachable before any temp resource is created."""
    for index_name in (index_names["index_tag"], index_names["index_type"]):
        result = es.index_check(index_name)
        if result["code"] != 0:
            raise SystemExit(
                f"local es service not reachable ({result.get('message')}), "
                "is the worker on the home server running?"
            )
        if result["data"]["is_existing"]:
            raise SystemExit(f"test suspended: index already exists: {index_name}")
    for table_key in TABLE_KEY_LIST:
        table_name = names[table_key]
        try:
            dynamodb.describe_table(TableName=table_name)
            raise SystemExit(f"test suspended: table already exists: {table_name}")
        except dynamodb.exceptions.ResourceNotFoundException:
            pass
    try:
        iam.get_role(RoleName=names["lambda_role"])
        raise SystemExit(
            f"test suspended: role already exists: {names['lambda_role']}"
        )
    except iam.exceptions.NoSuchEntityException:
        pass
    try:
        lambda_client.get_function(FunctionName=names["lambda_function"])
        raise SystemExit(
            f"test suspended: lambda already exists: {names['lambda_function']}"
        )
    except lambda_client.exceptions.ResourceNotFoundException:
        pass


def test_api_flow(lambda_client, function_name, claims):
    def call(method, path, body=None, query=None):
        return lambda_api_call(
            lambda_client, function_name, claims, method, path, body, query
        )

    step("backend: caller identity")
    response = call("GET", "/api/me")
    check(response["code"] == 0, f"me request succeeds: {response}")
    check(
        response["data"]["role"] == "admin",
        "selected test user has the admin role",
    )

    # ----------------------------------------------------------- tag tree CRUD
    step("backend: tag tree crud, name search, circular parent prohibition")
    response = call("GET", "/api/tag")
    check(response["code"] == 0, f"initial tag list succeeds: {response}")
    check(response["data"]["tags"] == [], "freshly created table has no tag")

    # tree: alpha -> beta -> gamma; only alpha logs history
    response = call("POST", "/api/tag", {
        "name": "alpha tag", "is_history_enabled": True, "time_zone": TIMEZONE_TEST,
    })
    check(response["code"] == 0, f"tag creation succeeds: {response}")
    tag_a = response["data"]["tag"]["tag_id"]
    check(
        response["data"]["tag"]["create_at_timezone"] == TIMEZONE_TEST,
        "created tag stores the given time zone",
    )

    response = call("POST", "/api/tag", {"name": "beta tag", "parent_id": tag_a})
    check(response["code"] == 0, f"child tag creation succeeds: {response}")
    tag_b = response["data"]["tag"]["tag_id"]
    response = call("POST", "/api/tag", {"name": "gamma tag", "parent_id": tag_b})
    check(response["code"] == 0, f"grandchild tag creation succeeds: {response}")
    tag_c = response["data"]["tag"]["tag_id"]

    response = call("POST", "/api/tag", {"name": "orphan", "parent_id": "0" * 16})
    check(response["code"] < 0, f"creation under a missing parent fails: {response}")

    response = call("GET", "/api/tag")
    check(len(response["data"]["tags"]) == 3, "tag list returns the 3 created tags")
    response = call("GET", "/api/tag", query={"name": "ALPHA"})
    name_list = [item["name"] for item in response["data"]["tags"]]
    check(
        name_list == ["alpha tag"],
        f"case-insensitive name search finds the tag: {name_list}",
    )

    response = call("GET", f"/api/tag/{tag_a}/children")
    child_ids = [item["tag_id"] for item in response["data"]["children"]]
    check(child_ids == [tag_b], f"children of alpha is [beta]: {child_ids}")

    response = call("GET", f"/api/tag/{tag_c}/ancestor")
    ancestor_ids = [item["tag_id"] for item in response["data"]["ancestors"]]
    check(
        ancestor_ids == [tag_b, tag_a],
        f"ancestors of gamma are [beta, alpha], nearest first: {ancestor_ids}",
    )

    response = call("PATCH", f"/api/tag/{tag_a}", {"parent_id": tag_c})
    check(
        response["code"] < 0,
        f"moving alpha under its descendant gamma fails (circular): {response}",
    )
    response = call("PATCH", f"/api/tag/{tag_a}", {"parent_id": tag_a})
    check(response["code"] < 0, f"a tag as its own parent fails: {response}")

    # -------------------------------------------------- char-level name search
    step("backend: char-level name search through the local es index")
    response = call("GET", "/api/tag/search", query={"query": "alpha"})
    check(response["code"] == 0, f"search request succeeds: {response}")
    results = response["data"]["results"]
    check(
        [item["tag_id"] for item in results] == [tag_a],
        f"search 'alpha' finds exactly the alpha tag: {results}",
    )
    match_list = results[0]["match_list"]
    check(
        match_list == [{"field": "name", "index_start": 0, "index_end": 5}],
        f"match positions cover 'alpha' inside 'alpha tag': {match_list}",
    )

    response = call("GET", "/api/tag/search", query={"query": "TAG"})
    check(
        len(response["data"]["results"]) == 3,
        "case-insensitive substring 'TAG' matches all 3 names",
    )
    response = call("GET", "/api/tag/search", query={"query": "no such name"})
    check(
        response["data"]["results"] == [],
        "search without a matching substring returns nothing",
    )
    response = call("GET", "/api/tag/search")
    check(response["code"] < 0, f"search without 'query' fails: {response}")

    # -------------------------------------------------------------- tag history
    step("backend: tag edit history and is_history_enabled toggling")
    response = call("GET", f"/api/tag/{tag_a}/history")
    check(
        len(response["data"]["history"]) == 1
        and response["data"]["history"][0]["operation"] == "create",
        f"history of alpha holds the create record: {response['data']}",
    )

    response = call("PATCH", f"/api/tag/{tag_a}", {"name": "alpha tag renamed"})
    check(response["code"] == 0, f"tag rename succeeds: {response}")
    response = call("GET", "/api/tag/search", query={"query": "renamed"})
    check(
        [item["tag_id"] for item in response["data"]["results"]] == [tag_a],
        "rename is reflected in the name index",
    )
    response = call("GET", f"/api/tag/{tag_a}/history")
    history = response["data"]["history"]
    check(
        len(history) == 2 and history[0]["operation"] == "update"
        and history[0]["detail"]["name"] == "alpha tag renamed",
        f"rename is logged, newest first: {history}",
    )

    response = call("PATCH", f"/api/tag/{tag_a}", {"is_history_enabled": False})
    check(response["code"] == 0, f"history toggle off succeeds: {response}")
    response = call("GET", f"/api/tag/{tag_a}/history")
    check(
        response["data"]["history"] == [],
        "toggling history off removed all history of alpha",
    )

    response = call("PATCH", f"/api/tag/{tag_a}", {"is_history_enabled": True})
    check(response["code"] == 0, f"history toggle on succeeds: {response}")
    response = call("GET", f"/api/tag/{tag_a}/history")
    history = response["data"]["history"]
    check(
        len(history) == 1 and history[0]["detail"] == {"is_history_enabled": True},
        f"logging restarts from the toggle-on edit itself: {history}",
    )

    response = call("GET", f"/api/tag/{tag_b}/history")
    check(
        response["data"]["history"] == [],
        "tag with is_history_enabled off has no history",
    )

    # ----------------------------------------------------- obj attach + reorder
    step("backend: obj attach, lexorank order, reorder")
    obj_1 = id_make()

    for tag_id in (tag_a, tag_b, tag_c):
        response = call("POST", f"/api/obj/{obj_1}/tag", {"tag_id": tag_id})
        check(response["code"] == 0, f"attach succeeds: {response}")

    response = call("GET", f"/api/obj/{obj_1}/tag")
    id_list = [entry["tag_id"] for entry in response["data"]["entries"]]
    check(
        id_list == [tag_a, tag_b, tag_c],
        f"attach without lexorank appends, order [alpha, beta, gamma]: {id_list}",
    )

    response = call("POST", f"/api/obj/{obj_1}/tag", {"tag_id": tag_a})
    check(response["code"] < 0, f"attaching an already attached tag fails: {response}")

    response = call(
        "PATCH", f"/api/obj/{obj_1}/tag/{tag_b}", {"lexorank": "a0"}
    )
    check(response["code"] < 0, f"lexorank ending with '0' is rejected: {response}")

    # '5' sorts before every server-generated rank (generation starts at 'i')
    response = call("PATCH", f"/api/obj/{obj_1}/tag/{tag_c}", {"lexorank": "5"})
    check(response["code"] == 0, f"reorder succeeds: {response}")
    response = call("GET", f"/api/obj/{obj_1}/tag")
    id_list = [entry["tag_id"] for entry in response["data"]["entries"]]
    check(
        id_list == [tag_c, tag_a, tag_b],
        f"gamma moved to the front: {id_list}",
    )

    response = call("PATCH", f"/api/obj/{obj_1}/tag/{id_make()}", {"lexorank": "5"})
    check(response["code"] < 0, f"reorder of a not attached tag fails: {response}")

    # 'which objs have this tag' and tag deletion go through gsi_tag_id,
    # which is eventually consistent; give the fresh entries a moment
    time.sleep(2)

    response = call("GET", f"/api/tag/{tag_a}/obj")
    check(
        response["data"]["obj_ids"] == [obj_1],
        f"alpha reports the obj it is attached to: {response['data']}",
    )

    # ------------------------------------------------------------- obj history
    step("backend: obj edit history, delete history before a time point")
    response = call("GET", f"/api/obj/{obj_1}/tag-history")
    history = response["data"]["history"]
    operation_list = [record["operation"] for record in history]
    check(
        operation_list == ["reorder", "attach", "attach", "attach"],
        f"obj history holds 3 attaches and the reorder, newest first: {operation_list}",
    )

    # cut at the reorder record: everything strictly earlier gets deleted
    time_cut = history[0]["time_stamp"]
    response = call(
        "DELETE", f"/api/obj/{obj_1}/tag-history", query={"before": time_cut}
    )
    check(response["code"] == 0, f"history delete-before succeeds: {response}")
    check(
        response["data"]["history_count_deleted"] == 3,
        f"the 3 attach records earlier than the cut are deleted: {response['data']}",
    )
    response = call("GET", f"/api/obj/{obj_1}/tag-history")
    history = response["data"]["history"]
    check(
        len(history) == 1 and history[0]["operation"] == "reorder",
        f"records at/after the cut survive: {history}",
    )

    # ---------------------------------------------------------- detach + delete
    step("backend: detach, tag deletion rules")
    response = call("DELETE", f"/api/obj/{obj_1}/tag/{tag_b}")
    check(response["code"] == 0, f"detach succeeds: {response}")
    response = call("GET", f"/api/obj/{obj_1}/tag")
    id_list = [entry["tag_id"] for entry in response["data"]["entries"]]
    check(id_list == [tag_c, tag_a], f"beta is detached: {id_list}")
    response = call("DELETE", f"/api/obj/{obj_1}/tag/{tag_b}")
    check(response["code"] < 0, f"detaching a not attached tag fails: {response}")

    response = call("DELETE", f"/api/tag/{tag_a}")
    check(
        response["code"] < 0,
        f"deleting alpha fails while beta is still its child: {response}",
    )

    response = call("DELETE", f"/api/tag/{tag_c}")
    check(response["code"] == 0, f"deleting leaf tag gamma succeeds: {response}")
    check(
        response["data"]["obj_detach_count"] == 1,
        "gamma deletion detached it from the obj",
    )
    response = call("GET", f"/api/obj/{obj_1}/tag")
    id_list = [entry["tag_id"] for entry in response["data"]["entries"]]
    check(id_list == [tag_a], f"only alpha stays attached: {id_list}")
    response = call("GET", f"/api/obj/{obj_1}/tag-history")
    check(
        response["data"]["history"][0]["operation"] == "detach",
        "tag deletion logged a detach to the obj history",
    )
    response = call("GET", f"/api/tag/{tag_c}")
    check(response["code"] < 0, "deleted tag is not found any more")
    response = call("GET", "/api/tag/search", query={"query": "gamma"})
    check(
        response["data"]["results"] == [],
        "deleted tag is gone from the name index",
    )
    response = call("GET", f"/api/tag/{tag_b}/children")
    check(response["data"]["children"] == [], "beta has no children any more")

    # ------------------------------------------------------- type independence
    step("backend: type side works and stays independent from the tag side")
    response = call("POST", "/api/type", {
        "name": "file type", "is_history_enabled": True,
    })
    check(response["code"] == 0, f"type creation succeeds: {response}")
    type_x = response["data"]["type"]["type_id"]

    response = call("GET", "/api/type")
    check(
        len(response["data"]["types"]) == 1,
        "type list holds only the created type (no tag rows leak in)",
    )
    response = call("GET", "/api/tag")
    check(
        len(response["data"]["tags"]) == 2,
        "tag list still holds alpha and beta (no type rows leak in)",
    )

    response = call("GET", "/api/type/search", query={"query": "file"})
    check(
        [item["type_id"] for item in response["data"]["results"]] == [type_x],
        "type name search finds the type through the type index",
    )
    response = call("GET", "/api/tag/search", query={"query": "file"})
    check(
        response["data"]["results"] == [],
        "tag search does not see type names (separate indices)",
    )

    response = call("POST", f"/api/obj/{obj_1}/type", {"type_id": type_x})
    check(response["code"] == 0, f"type attach succeeds: {response}")
    response = call("GET", f"/api/obj/{obj_1}/type")
    id_list = [entry["type_id"] for entry in response["data"]["entries"]]
    check(id_list == [type_x], f"obj has the attached type: {id_list}")

    response = call("GET", f"/api/obj/{obj_1}/type-history")
    check(
        [record["operation"] for record in response["data"]["history"]] == ["attach"],
        "obj type history is recorded in its own table",
    )
    response = call("GET", f"/api/type/{type_x}/history")
    check(
        [record["operation"] for record in response["data"]["history"]] == ["create"],
        "type edit history is recorded in its own table",
    )


def test_index_fail_flow(lambda_client, names, config, es_gen, index_names, claims):
    """an edit whose index operation is not confirmed must fail without
    changing dynamodb. simulated by setting the lambda's result timeout to 0:
    the index task is still sent, but the lambda gives up waiting for the
    worker's confirmation immediately, reproducing 'sqs message not consumed /
    no confirmation of index complete' while the real worker keeps running."""
    function_name = names["lambda_function"]

    def call(method, path, body=None, query=None):
        return lambda_api_call(
            lambda_client, function_name, claims, method, path, body, query
        )

    def lambda_env_set(env):
        lambda_client.update_function_configuration(
            FunctionName=function_name, Environment={"Variables": env}
        )
        lambda_client.get_waiter("function_updated_v2").wait(
            FunctionName=function_name
        )

    env_normal = lambda_env_build(names, config, es_gen, index_names)

    step("backend: edits fail while the index confirmation does not arrive")
    response = call("GET", "/api/tag")
    tag_list_before = response["data"]["tags"]
    # the target must be a leaf (beta, the one having a parent), so the delete
    # reaches the index operation instead of being refused for having children
    tag_leaf = next(tag for tag in tag_list_before if tag.get("parent_id"))

    lambda_env_set({**env_normal, "ES_RESULT_TIMEOUT_SEC": "0"})
    try:
        response = call("POST", "/api/tag", {"name": "unconfirmed tag"})
        check(
            response["code"] < 0,
            f"creation fails without index confirmation: {response}",
        )
        response = call(
            "PATCH", f"/api/tag/{tag_leaf['tag_id']}",
            {"name": "unconfirmed rename"},
        )
        check(
            response["code"] < 0,
            f"rename fails without index confirmation: {response}",
        )
        response = call("DELETE", f"/api/tag/{tag_leaf['tag_id']}")
        check(
            response["code"] < 0,
            f"delete fails without index confirmation: {response}",
        )
    finally:
        lambda_env_set(env_normal)

    response = call("GET", "/api/tag")
    check(
        response["data"]["tags"] == tag_list_before,
        "the failed edits left dynamodb unchanged",
    )


# --------------------------------------------------------------------- helpers


def admin_claims_get(config, cognito):
    cognito_gen = cognito_gen_load()
    pool_id = cognito_gen["cognito"]["user_pool_id"]
    group_access = config["tag_type"]["cognito"]["group_access"]
    group_admin = config["tag_type"]["cognito"]["group_admin"]

    paginator = cognito.get_paginator("list_users")
    for page in paginator.paginate(UserPoolId=pool_id):
        for user in page["Users"]:
            username = user["Username"]
            groups = set()
            paginator_group = cognito.get_paginator("admin_list_groups_for_user")
            for page_group in paginator_group.paginate(
                UserPoolId=pool_id, Username=username
            ):
                groups.update(group["GroupName"] for group in page_group["Groups"])
            if group_access not in groups or group_admin not in groups:
                continue

            attrs = {item["Name"]: item["Value"] for item in user["Attributes"]}
            return {
                "sub": attrs["sub"],
                "cognito:username": username,
                "email": attrs.get("email"),
                "cognito:groups": sorted(groups),
            }
    raise SystemExit(
        f"no cognito user belongs to both {group_access!r} and {group_admin!r}"
    )


def lambda_api_call(lambda_client, function_name, claims, method, path, body=None, query=None):
    event = {
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": claims}},
        },
        "rawPath": path,
    }
    if body is not None:
        event["body"] = json.dumps(body)
    if query is not None:
        event["queryStringParameters"] = {key: str(value) for key, value in query.items()}

    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode(),
    )
    payload = json.loads(response["Payload"].read())
    if response.get("FunctionError"):
        raise TestFail(f"lambda invocation failed: {payload}")
    return json.loads(payload["body"])


def id_make():
    chars = string.digits + string.ascii_lowercase
    return "".join(secrets.choice(chars) for _ in range(16))


def args_parse():
    parser = argparse.ArgumentParser(description="test the tag/type service")
    parser.add_argument(
        "items",
        nargs="*",
        help=f"test items, defaults to: {', '.join(ITEM_LIST_DEFAULT)}."
        " backend creates temp resources from zero; api needs the deployed"
        " architecture (ensure_architect.py)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove resources left by previous backend test runs",
    )
    parser.add_argument(
        "--assume-prefix",
        help="with --clean, find residue under this prefix instead of local config",
    )
    args = parser.parse_args()
    if args.clean and args.items:
        parser.error("--clean cannot be combined with test items")
    if args.assume_prefix is not None and not args.clean:
        parser.error("--assume-prefix can only be used with --clean")
    item_invalid_list = [item for item in args.items if item not in ITEM_LIST]
    if item_invalid_list:
        parser.error(
            f"invalid test item: {item_invalid_list[0]!r}; "
            f"choose from {', '.join(ITEM_LIST)}"
        )
    return args


if __name__ == "__main__":
    try:
        test_run(args_parse())
    except TestFail:
        raise SystemExit("\ntest FAILED")
