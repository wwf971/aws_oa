# test of the asset timeline service:
#
#   backend  (default) needs no deployed timeline architecture: creates a
#            TEMPORARY stack from zero (timeline tables + lambda, plus a
#            temp asset-node table + asset lambda wired to those tables)
#            under the prefix {prefix}-temp-{timestamp}, runs timeline CRUD,
#            collect, range and neighbor queries, then the asset-delete
#            flow that must clear every timeline collecting the asset
#            (and roll back when one collect-entry delete fails). then
#            removes every temp resource. this reproduces ensurement,
#            operation and removal of the architecture.
#            exception: the lambda execution roles (including the denied
#            role of the rollback check) use STABLE names (no timestamp)
#            and are kept across runs, because in this aws account a
#            freshly created role can stay un-assumable by lambda for many
#            minutes (measured > 4 min). only their inline policies are
#            rewritten each run for the current timestamped tables. the
#            very first run (roles not created yet) may still fail on role
#            assumability: simply rerun a few minutes later.
#            the temp asset lambda needs the deployed asset bucket name
#            (s3 prefix delete after the dynamodb transaction); that name
#            is read from _1_asset_service/config_gen.yaml.
#   api      checks that the DEPLOYED http api rejects requests without a
#            cognito jwt; requires ensure_architect.py to have been run
#            (reads the api endpoint from config_gen.yaml).
#
# external dependencies of both items: _0_auth_cognito must be deployed
# (cognito user pool with an admin user, user table with its user_id mapping).
# asset ids in this service are plain references (not validated against the
# asset service), so the timeline CRUD flow uses generated fake asset ids.
# the asset-delete flow creates real asset-node rows through a temp copy
# of the asset-service lambda, wired to the temp timeline-asset table.
#
# run the default item:  python test.py
# run selected items:    python test.py api backend
# clean test residue:     python test.py --clean
# clean old-prefix residue:
#                         python test.py --clean --assume-prefix old-prefix

import argparse
import json
import secrets
import string
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aws_utils import (
    TestFail,
    check,
    check_count,
    lambda_delete,
    lambda_function_ensure,
    lambda_role_delete,
    lambda_role_ensure,
    lambda_zip_build,
    step,
    table_delete,
    table_ensure,
    timestamp_resource_make,
)
from config_gen import (
    aws_client_make,
    cognito_gen_load,
    config_gen_load,
    config_load,
    names_build,
)
from ensure_architect import (
    api_lambda_ensure,
    api_role_ensure,
    timeline_asset_table_ensure,
    timeline_table_ensure,
)

ITEM_LIST = ["api", "backend"]
ITEM_LIST_DEFAULT = ["backend"]

DIR_SELF = Path(__file__).resolve().parent
DIR_ASSET_SERVICE = DIR_SELF.parent / "_1_asset_service"
DIR_ASSET_BACKEND = DIR_ASSET_SERVICE / "backend"
PATH_ASSET_CONFIG_GEN = DIR_ASSET_SERVICE / "config_gen.yaml"

MINUTE_MS = 60 * 1000
TIMEZONE_TEST = 540  # +09:00 in minutes
# iam policy changes are not always visible on the next request
IAM_PROPAGATE_SEC = 10


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
    config_gen = config_gen_load().get("asset_timeline")
    if config_gen is None:
        raise SystemExit(
            "asset_timeline not found in config_gen.yaml: the api item tests "
            "the deployed http api, run ensure_architect.py first"
        )
    try:
        urlopen(f"{config_gen['api_endpoint']}/api/timeline", timeout=30)
        status = 200
    except HTTPError as error:
        status = error.code
    check(status in (401, 403), "/api/timeline rejects a request without a jwt")


# -------------------------------------------------------------- item: backend


def test_backend(config):
    """create temp architecture from zero -> run the api flow -> remove it."""
    prefix_temp = f"{config['name_prefix']}-temp-{timestamp_resource_make()}"
    names = names_build({"name_prefix": prefix_temp})
    names["table_user"] = cognito_gen_load()["user_table"]["table_name"]
    names["table_asset_node"] = f"{prefix_temp}-asset-node"
    names["lambda_asset_function"] = f"{prefix_temp}-asset-api"
    names["bucket_asset"] = asset_bucket_load()
    # the execution roles keep stable names (no timestamp) and are reused
    # across runs: a freshly created role stays un-assumable by lambda for
    # many minutes in this aws account, so the roles are kept by the
    # cleanup and only their inline policies are rewritten each run to
    # point at the current timestamped tables. the denied role is for the
    # rollback test: its policy never allows DeleteItem on the
    # timeline-asset table (see test_asset_delete_from_timelines)
    names["lambda_role"] = f"{config['name_prefix']}-temp-api-role"
    names["lambda_asset_role"] = f"{config['name_prefix']}-temp-asset-api-role"
    names["lambda_asset_role_denied"] = (
        f"{config['name_prefix']}-temp-asset-api-denied-role"
    )

    dynamodb = aws_client_make(config, "dynamodb")
    iam = aws_client_make(config, "iam")
    lambda_client = aws_client_make(config, "lambda")
    sts = aws_client_make(config, "sts")
    cognito = aws_client_make(config, "cognito-idp")

    temp_resources_absent_check(dynamodb, lambda_client, names)
    claims = admin_claims_get(config, cognito)

    try:
        step(f"backend: create temp architecture, prefix {prefix_temp}")
        region = config["aws"]["region_name"]
        account_id = sts.get_caller_identity()["Account"]
        timeline_table_ensure(dynamodb, names["table_timeline"])
        timeline_asset_table_ensure(dynamodb, names["table_timeline_asset"])
        asset_node_table_ensure(dynamodb, names["table_asset_node"])
        role_arn = api_role_ensure(iam, names, region, account_id)
        api_lambda_ensure(lambda_client, names, config, role_arn)
        asset_temp_lambda_ensure(lambda_client, iam, names, config, region, account_id)
        lambda_client.get_waiter("function_active_v2").wait(
            FunctionName=names["lambda_function"]
        )
        lambda_client.get_waiter("function_active_v2").wait(
            FunctionName=names["lambda_asset_function"]
        )
        # the reused roles had their inline policies rewritten above for
        # the new table names; give iam a moment to propagate the policies
        time.sleep(IAM_PROPAGATE_SEC)

        test_api_flow(lambda_client, names["lambda_function"], claims)
        test_asset_delete_from_timelines(lambda_client, iam, names, claims)
    finally:
        step("backend: remove temp architecture")
        error_list = temp_resource_set_delete(dynamodb, lambda_client, names)
        if error_list and sys.exc_info()[0] is None:
            raise error_list[0]


def temp_resource_set_delete(dynamodb, lambda_client, names):
    """Try every delete even if one delete fails, so one failure does not
    prevent cleanup of the remaining resource objects. the two execution
    roles are intentionally NOT deleted: they are reused across runs (see
    test_backend); --clean removes them together with everything else."""
    operation_list = [
        (lambda_delete, lambda_client, names["lambda_function"]),
        (lambda_delete, lambda_client, names["lambda_asset_function"]),
        (table_delete, dynamodb, names["table_timeline_asset"]),
        (table_delete, dynamodb, names["table_timeline"]),
        (table_delete, dynamodb, names["table_asset_node"]),
    ]
    error_list = []
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
    test are considered (timeline + temp asset-api)."""
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
        and role["RoleName"].endswith("-role")
    ]
    table_name_list = [
        table_name
        for page in dynamodb.get_paginator("list_tables").paginate()
        for table_name in page["TableNames"]
        if table_name.startswith(name_marker)
        and table_name.endswith(("-info", "-asset", "-asset-node"))
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


def temp_resources_absent_check(dynamodb, lambda_client, names):
    """suspend the test if a resource instance with the name and type of a
    temp resource object to be created already exists. the two execution
    roles are not checked: they are expected to persist across runs (see
    test_backend)."""
    for table_name in (
        names["table_timeline"],
        names["table_timeline_asset"],
        names["table_asset_node"],
    ):
        try:
            dynamodb.describe_table(TableName=table_name)
            raise SystemExit(f"test suspended: table already exists: {table_name}")
        except dynamodb.exceptions.ResourceNotFoundException:
            pass
    for function_name in (names["lambda_function"], names["lambda_asset_function"]):
        try:
            lambda_client.get_function(FunctionName=function_name)
            raise SystemExit(
                f"test suspended: lambda already exists: {function_name}"
            )
        except lambda_client.exceptions.ResourceNotFoundException:
            pass


def test_api_flow(lambda_client, function_name, claims):
    step("backend: timeline CRUD, collect, range and neighbor queries")
    name_test = "test timeline"
    timeline_id = None

    # five fake assets, one minute apart: t[i] = time_base + i minutes
    asset_ids = [asset_id_make() for _ in range(5)]
    time_base = int(time.time() * 1000)
    t = [time_base + i * MINUTE_MS for i in range(5)]

    def call(method, path, body=None, query=None):
        return lambda_api_call(
            lambda_client, function_name, claims, method, path, body, query
        )

    response = call("GET", "/api/me")
    check(response["code"] == 0, f"me request succeeds: {response}")
    check(
        response["data"]["role"] == "admin",
        "selected test user has the admin role",
    )

    response = call("GET", "/api/timeline")
    check(response["code"] == 0, f"initial timeline list succeeds: {response}")
    check(
        response["data"]["timelines"] == [],
        "freshly created table has no timeline",
    )

    # ------------------------------------------------------------ timeline CRUD
    response = call(
        "POST", "/api/timeline", {"name": name_test, "time_zone": TIMEZONE_TEST}
    )
    check(response["code"] == 0, f"timeline creation succeeds: {response}")
    timeline_id = response["data"]["timeline"]["timeline_id"]

    response = call("GET", "/api/timeline", query={"name": "TEST TIME"})
    check(response["code"] == 0, f"timeline name search succeeds: {response}")
    name_list = [item["name"] for item in response["data"]["timelines"]]
    check(
        name_list == [name_test],
        f"case-insensitive name search finds the test timeline: {name_list}",
    )

    response = call("GET", f"/api/timeline/{timeline_id}")
    check(response["code"] == 0, f"timeline get succeeds: {response}")
    check(
        response["data"]["timeline"]["time_zone"] == TIMEZONE_TEST,
        "timeline time_zone matches the created value",
    )

    response = call(
        "PATCH", f"/api/timeline/{timeline_id}", {"name": f"{name_test} renamed"}
    )
    check(response["code"] == 0, f"timeline rename succeeds: {response}")
    response = call("GET", f"/api/timeline/{timeline_id}")
    check(
        response["data"]["timeline"]["name"] == f"{name_test} renamed",
        "renamed timeline name is stored",
    )

    # ---------------------------------------------------------------- collecting
    for i in range(5):
        response = call(
            "POST",
            f"/api/timeline/{timeline_id}/asset",
            {"asset_id": asset_ids[i], "time_stamp": t[i]},
        )
        check(response["code"] == 0, f"asset {i} collect succeeds: {response}")
        check(
            response["data"]["entry"]["time_stamp_timezone"] == TIMEZONE_TEST,
            f"asset {i} inherits the timeline time zone",
        )

    # collect/change/remove locate entries through gsi_asset_id, which is
    # eventually consistent; give the freshly written entries a moment
    time.sleep(2)

    response = call(
        "POST",
        f"/api/timeline/{timeline_id}/asset",
        {"asset_id": asset_ids[0], "time_stamp": t[3]},
    )
    check(
        response["code"] < 0,
        f"collecting an already collected asset fails: {response}",
    )

    # ------------------------------------------------------------- range queries
    response = call(
        "GET",
        f"/api/timeline/{timeline_id}/asset",
        query={"time_start": t[1], "time_end": t[3]},
    )
    check(response["code"] == 0, f"range query succeeds: {response}")
    id_list = [entry["asset_id"] for entry in response["data"]["assets"]]
    check(
        id_list == asset_ids[1:4],
        f"range [t1, t3] returns assets 1..3 in time order: {id_list}",
    )

    # ---------------------------------------------------------- neighbor queries
    response = call(
        "GET",
        f"/api/timeline/{timeline_id}/asset-neighbor",
        query={"time_point": t[2], "count": 2, "direction": "both"},
    )
    check(response["code"] == 0, f"neighbor query succeeds: {response}")
    before_ids = [entry["asset_id"] for entry in response["data"]["before"]]
    after_ids = [entry["asset_id"] for entry in response["data"]["after"]]
    check(
        before_ids == [asset_ids[1], asset_ids[0]],
        f"neighbors before t2, nearest first: {before_ids}",
    )
    check(
        after_ids == [asset_ids[3], asset_ids[4]],
        f"neighbors after t2, nearest first: {after_ids}",
    )
    check(
        asset_ids[2] not in before_ids + after_ids,
        "asset exactly at the time point is on neither side",
    )

    response = call(
        "GET",
        f"/api/timeline/{timeline_id}/asset-neighbor",
        query={"time_point": t[2], "count": 3, "direction": "after"},
    )
    check(
        [entry["asset_id"] for entry in response["data"]["after"]]
        == [asset_ids[3], asset_ids[4]],
        "one-direction neighbor query returns only the later assets",
    )

    # ---------------------------------------------------------- time point change
    t_moved = time_base + 5 * MINUTE_MS
    response = call(
        "PATCH",
        f"/api/timeline/{timeline_id}/asset/{asset_ids[0]}",
        {"time_stamp": t_moved},
    )
    check(response["code"] == 0, f"asset time point change succeeds: {response}")
    response = call(
        "GET",
        f"/api/timeline/{timeline_id}/asset",
        query={"time_start": t[0], "time_end": t[0]},
    )
    check(
        response["data"]["assets"] == [],
        "old time point of the moved asset is empty",
    )
    response = call(
        "GET",
        f"/api/timeline/{timeline_id}/asset",
        query={"time_start": t_moved, "time_end": t_moved},
    )
    check(
        [entry["asset_id"] for entry in response["data"]["assets"]]
        == [asset_ids[0]],
        "moved asset is found at its new time point",
    )

    # ------------------------------------------------------- asset -> timelines
    response = call("GET", f"/api/asset/{asset_ids[1]}/timeline")
    check(response["code"] == 0, f"asset timelines query succeeds: {response}")
    found = response["data"]["timelines"]
    check(
        [entry["timeline_id"] for entry in found] == [timeline_id]
        and found[0]["time_stamp"] == t[1],
        f"asset 1 is reported as collected by the test timeline at t1: {found}",
    )

    # -------------------------------------------------------------------- remove
    response = call("DELETE", f"/api/timeline/{timeline_id}/asset/{asset_ids[1]}")
    check(response["code"] == 0, f"asset removal succeeds: {response}")
    response = call(
        "GET",
        f"/api/timeline/{timeline_id}/asset",
        query={"time_start": t[1], "time_end": t[1]},
    )
    check(
        response["data"]["assets"] == [],
        "removed asset is gone from the timeline",
    )

    # -------------------------------------------------------------------- delete
    response = call("DELETE", f"/api/timeline/{timeline_id}")
    check(response["code"] == 0, f"timeline deletion succeeds: {response}")
    check(
        response["data"]["entry_count_deleted"] == 4,
        "deletion removed the 4 remaining collect entries",
    )
    response = call("GET", f"/api/timeline/{timeline_id}")
    check(response["code"] < 0, "deleted timeline is not found any more")


def test_asset_delete_from_timelines(lambda_client, iam, names, claims):
    """asset delete (the asset-service lambda) must remove the asset from
    every timeline that collects it, in one dynamodb transaction: if a
    collect-entry delete fails, the asset node stays too."""
    step("backend: asset delete clears every collecting timeline")
    time_stamp = int(time.time() * 1000)

    def timeline_call(method, path, body=None, query=None):
        return lambda_api_call(
            lambda_client, names["lambda_function"], claims, method, path, body, query
        )

    def asset_call(method, path, body=None):
        return lambda_api_call(
            lambda_client, names["lambda_asset_function"], claims, method, path, body
        )

    response = timeline_call(
        "POST", "/api/timeline", {"name": "scanner", "time_zone": TIMEZONE_TEST}
    )
    check(response["code"] == 0, f"scanner timeline creation succeeds: {response}")
    timeline_a = response["data"]["timeline"]["timeline_id"]

    response = timeline_call(
        "POST", "/api/timeline", {"name": "transactions", "time_zone": TIMEZONE_TEST}
    )
    check(response["code"] == 0, f"transactions timeline creation succeeds: {response}")
    timeline_b = response["data"]["timeline"]["timeline_id"]

    # two assets: the one being deleted, and a sibling that must stay collected
    response = asset_call(
        "POST",
        "/api/asset",
        {
            "name": "receipt.txt",
            "asset_type": "file",
            "files": [{"path": "receipt.txt", "content_type": "text/plain"}],
        },
    )
    check(response["code"] == 0, f"deleted-target asset creation succeeds: {response}")
    node_removed = response["data"]["node"]
    asset_removed = node_removed["asset_id"]

    response = asset_call(
        "POST",
        "/api/asset",
        {
            "name": "keep.txt",
            "asset_type": "file",
            "files": [{"path": "keep.txt", "content_type": "text/plain"}],
        },
    )
    check(response["code"] == 0, f"kept asset creation succeeds: {response}")
    node_kept = response["data"]["node"]
    asset_kept = node_kept["asset_id"]

    response = timeline_call(
        "POST",
        f"/api/timeline/{timeline_a}/asset",
        {"asset_id": asset_removed, "time_stamp": time_stamp},
    )
    check(response["code"] == 0, f"collect target into scanner succeeds: {response}")
    response = timeline_call(
        "POST",
        f"/api/timeline/{timeline_b}/asset",
        {"asset_id": asset_removed, "time_stamp": time_stamp},
    )
    check(
        response["code"] == 0,
        f"collect target into transactions succeeds: {response}",
    )
    response = timeline_call(
        "POST",
        f"/api/timeline/{timeline_a}/asset",
        {"asset_id": asset_kept, "time_stamp": time_stamp + MINUTE_MS},
    )
    check(response["code"] == 0, f"collect kept asset into scanner succeeds: {response}")

    # collect/remove locate entries through gsi_asset_id, which is
    # eventually consistent; give the freshly written entries a moment
    time.sleep(2)

    response = asset_call("DELETE", f"/api/node/{node_removed['node_id']}")
    check(response["code"] == 0, f"asset delete succeeds: {response}")
    check(
        response["data"]["timeline_entry_count_deleted"] == 2,
        f"delete removed collect entries from both timelines: {response['data']}",
    )

    response = timeline_call("GET", f"/api/asset/{asset_removed}/timeline")
    check(response["code"] == 0, f"cleared asset timelines query succeeds: {response}")
    check(
        response["data"]["timelines"] == [],
        f"deleted asset is collected by no timeline: {response['data']}",
    )
    response = timeline_call("GET", f"/api/asset/{asset_kept}/timeline")
    check(
        [entry["timeline_id"] for entry in response["data"]["timelines"]]
        == [timeline_a],
        f"kept asset is still collected by scanner only: {response['data']}",
    )

    response = asset_call("GET", "/api/tree")
    node_ids = {node["node_id"] for node in response["data"]["nodes"]}
    check(node_removed["node_id"] not in node_ids, "deleted asset node is gone")
    check(node_kept["node_id"] in node_ids, "kept asset node is still in the tree")

    # ---------------------------------------------------------- folder subtree
    response = asset_call("POST", "/api/folder", {"name": "bundle"})
    check(response["code"] == 0, f"bundle folder creation succeeds: {response}")
    folder_id = response["data"]["node"]["node_id"]
    response = asset_call(
        "POST",
        "/api/asset",
        {
            "name": "inside.txt",
            "parent_id": folder_id,
            "asset_type": "file",
            "files": [{"path": "inside.txt", "content_type": "text/plain"}],
        },
    )
    check(response["code"] == 0, f"folder-child asset creation succeeds: {response}")
    node_child = response["data"]["node"]
    response = timeline_call(
        "POST",
        f"/api/timeline/{timeline_b}/asset",
        {"asset_id": node_child["asset_id"], "time_stamp": time_stamp},
    )
    check(response["code"] == 0, f"collect folder-child into transactions succeeds: {response}")
    time.sleep(2)

    response = asset_call("DELETE", f"/api/node/{folder_id}")
    check(response["code"] == 0, f"folder subtree delete succeeds: {response}")
    check(
        response["data"]["timeline_entry_count_deleted"] == 1,
        f"subtree delete removed the child's collect entry: {response['data']}",
    )
    response = timeline_call("GET", f"/api/asset/{node_child['asset_id']}/timeline")
    check(
        response["data"]["timelines"] == [],
        "folder-child is gone from transactions after subtree delete",
    )

    # ---------------------------------------------------------- transaction rollback
    response = timeline_call(
        "POST",
        f"/api/timeline/{timeline_b}/asset",
        {"asset_id": asset_kept, "time_stamp": time_stamp + 2 * MINUTE_MS},
    )
    check(
        response["code"] == 0,
        f"collect kept asset into transactions succeeds: {response}",
    )
    time.sleep(2)

    # switch the asset lambda to the denied role, whose policy never allows
    # DeleteItem on the timeline-asset table: the transaction then fails on
    # a collect-entry delete and must roll the node delete back
    asset_lambda_role_switch(
        lambda_client, iam, names, names["lambda_asset_role_denied"]
    )

    response = asset_call("DELETE", f"/api/node/{node_kept['node_id']}")
    check(
        response["code"] < 0,
        f"delete fails when a timeline collect removal is denied: {response}",
    )

    response = asset_call("GET", "/api/tree")
    node_ids = {node["node_id"] for node in response["data"]["nodes"]}
    check(
        node_kept["node_id"] in node_ids,
        "rolled-back delete left the asset node in the tree",
    )
    response = timeline_call("GET", f"/api/asset/{asset_kept}/timeline")
    timeline_ids = sorted(entry["timeline_id"] for entry in response["data"]["timelines"])
    check(
        timeline_ids == sorted([timeline_a, timeline_b]),
        f"rolled-back delete left the asset on both timelines: {response['data']}",
    )

    asset_lambda_role_switch(lambda_client, iam, names, names["lambda_asset_role"])


# ----------------------------------------------------- temp asset-api helpers


def asset_bucket_load():
    """the deployed asset bucket, used by the temp asset lambda for the
    s3 prefix delete that runs after the dynamodb transaction. no object
    is uploaded; the list of `{asset_id}/` is empty."""
    if not PATH_ASSET_CONFIG_GEN.exists():
        raise SystemExit(
            f"{PATH_ASSET_CONFIG_GEN} not found: the asset-delete flow "
            "needs the asset service bucket, run "
            "_1_asset_service/ensure_architect.py first"
        )
    with open(PATH_ASSET_CONFIG_GEN) as f:
        asset_gen = yaml.safe_load(f)
    bucket_asset = (asset_gen.get("asset_service") or {}).get("bucket_asset")
    if not bucket_asset:
        raise SystemExit(
            "bucket_asset missing from _1_asset_service/config_gen.yaml"
        )
    return bucket_asset


def asset_node_table_ensure(dynamodb, table_name):
    """same schema as _1_asset_service/ensure_architect.py asset_node_table_ensure."""
    table_ensure(
        dynamodb,
        table_name,
        attribute_definitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "node_id", "AttributeType": "S"},
            {"AttributeName": "asset_id", "AttributeType": "S"},
        ],
        key_schema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "node_id", "KeyType": "RANGE"},
        ],
        gsi_list=[
            {
                "IndexName": "gsi_asset_id",
                "KeySchema": [{"AttributeName": "asset_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            }
        ],
    )


def asset_temp_role_policy(names, region, account_id, timeline_delete_allowed=True):
    """execution role of the temp asset lambda. timeline DeleteItem is
    optional so the rollback case can deny collect-entry removal."""
    timeline_actions = ["dynamodb:Query"]
    if timeline_delete_allowed:
        timeline_actions.append("dynamodb:DeleteItem")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": "*",
            },
            {
                "Sid": "UserTableRead",
                "Effect": "Allow",
                "Action": ["dynamodb:Query"],
                "Resource": [
                    f"arn:aws:dynamodb:{region}:{account_id}:table/{names['table_user']}",
                    f"arn:aws:dynamodb:{region}:{account_id}:table/{names['table_user']}/index/*",
                ],
            },
            {
                "Sid": "AssetNodeTable",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:Query",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:BatchWriteItem",
                ],
                "Resource": [
                    f"arn:aws:dynamodb:{region}:{account_id}:table/{names['table_asset_node']}",
                    f"arn:aws:dynamodb:{region}:{account_id}:table/{names['table_asset_node']}/index/*",
                ],
            },
            {
                "Sid": "TimelineAssetTable",
                "Effect": "Allow",
                "Action": timeline_actions,
                "Resource": [
                    f"arn:aws:dynamodb:{region}:{account_id}:table/{names['table_timeline_asset']}",
                    f"arn:aws:dynamodb:{region}:{account_id}:table/{names['table_timeline_asset']}/index/*",
                ],
            },
            {
                "Sid": "AssetBucket",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{names['bucket_asset']}",
                    f"arn:aws:s3:::{names['bucket_asset']}/*",
                ],
            },
        ],
    }


def asset_lambda_role_switch(lambda_client, iam, names, role_name):
    """point the temp asset lambda at another execution role. both roles are
    persistent and long-established, so the switch takes effect as soon as
    the configuration update completes: the new execution environments get
    the new role's credentials. this avoids rewriting the policy of the
    in-use role, which can take minutes to take effect in this aws account."""
    role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    lambda_client.update_function_configuration(
        FunctionName=names["lambda_asset_function"], Role=role_arn
    )
    lambda_client.get_waiter("function_updated_v2").wait(
        FunctionName=names["lambda_asset_function"]
    )


def asset_temp_lambda_ensure(lambda_client, iam, names, config, region, account_id):
    role_arn = lambda_role_ensure(
        iam,
        names["lambda_asset_role"],
        asset_temp_role_policy(names, region, account_id),
    )
    # second execution role for the rollback test: identical except that
    # DeleteItem on the timeline-asset table is never allowed. the test
    # switches the lambda between the two roles instead of rewriting one
    # policy, because policy changes on an in-use role can take minutes to
    # take effect in this aws account
    lambda_role_ensure(
        iam,
        names["lambda_asset_role_denied"],
        asset_temp_role_policy(names, region, account_id, timeline_delete_allowed=False),
    )
    env = {
        "BUCKET_ASSET": names["bucket_asset"],
        "TABLE_ASSET_NODE": names["table_asset_node"],
        "TABLE_USER": names["table_user"],
        "TABLE_TIMELINE_ASSET": names["table_timeline_asset"],
        "GROUP_ACCESS": config["asset_timeline"]["cognito"]["group_access"],
        "GROUP_ADMIN": config["asset_timeline"]["cognito"]["group_admin"],
    }
    return lambda_function_ensure(
        lambda_client,
        names["lambda_asset_function"],
        config["asset_timeline"]["lambda"],
        role_arn,
        env,
        lambda_zip_build(DIR_ASSET_BACKEND),
    )


# --------------------------------------------------------------------- helpers


def admin_claims_get(config, cognito):
    cognito_gen = cognito_gen_load()
    pool_id = cognito_gen["cognito"]["user_pool_id"]
    group_access = config["asset_timeline"]["cognito"]["group_access"]
    group_admin = config["asset_timeline"]["cognito"]["group_admin"]

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


def asset_id_make():
    chars = string.digits + string.ascii_lowercase
    return "".join(secrets.choice(chars) for _ in range(16))


def args_parse():
    parser = argparse.ArgumentParser(description="test the asset timeline service")
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
    except TestFail as error:
        raise SystemExit(f"\ntest FAILED: {error}")
