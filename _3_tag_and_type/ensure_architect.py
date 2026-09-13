# ensure all aws resource instances of the tag/type service, in dependency
# order:
#   2 name indices on the local es service -> 4 tag tables + 4 type tables
#   -> lambda role -> lambda function -> http api (jwt authorizer, route)
#
# 'ensure' means: create if missing, update if config differs, never recreate.
# generated ids are saved to config_gen.yaml. no frontend resources yet
# (no web bucket / cloudfront); clients call the http api endpoint directly.
#
# generic ensure/delete logic lives in /aws_utils/ (see
# /doc/aws_oa_impl.md#shared-utilities-aws_utils); this script only holds
# what is specific to this service: table schemas, role policy, lambda env,
# and the name indices on the local es service.
#
# cognito ids come from ../_0_auth_cognito/config_gen.yaml (run
# ensure_cognito.py + ensure_user_table.py there first). the local es task
# queue/result table come from ../_2_local_es/config_gen.yaml (run
# ensure_architect.py there first); ensuring/deleting the indices needs the
# es worker on the home server running.
#
# delete everything:            python ensure_architect.py --delete all
# delete under an old prefix:   python ensure_architect.py --delete all --assume-prefix xxx

import argparse
import sys
from pathlib import Path

DIR_SELF = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR_SELF.parent))

from aws_utils import (
    api_route_ensure,
    api_stage_ensure,
    delete_confirm,
    http_api_delete,
    http_api_ensure,
    jwt_authorizer_ensure,
    lambda_delete,
    lambda_function_ensure,
    lambda_integration_ensure,
    lambda_invoke_permission_ensure,
    lambda_role_delete,
    lambda_role_ensure,
    lambda_zip_build,
    table_delete,
    table_ensure,
)
from config_gen import (
    ES_FIELD_CONFIG,
    ES_INDEX_CONFIG_NAME,
    ES_RESULT_TIMEOUT_SEC,
    PATH_LOCAL_ES_CLIENT,
    TABLE_KEY_LIST,
    aws_client_make,
    cognito_gen_load,
    config_gen_load,
    config_gen_save,
    config_load,
    index_names_build,
    local_es_client_make,
    local_es_gen_load,
    names_build,
)

DIR_BACKEND = DIR_SELF / "backend"

ROUTE_KEY_API = "ANY /api/{proxy+}"


def args_parse():
    parser = argparse.ArgumentParser(
        description="ensure or delete the tag/type architecture"
    )
    parser.add_argument(
        "--delete", choices=["all"], help="delete all maintained AWS resources"
    )
    parser.add_argument(
        "--assume-prefix",
        help="use this resource name prefix instead of the prefix in local config",
    )
    args = parser.parse_args()
    if args.assume_prefix is not None and args.delete != "all":
        parser.error("--assume-prefix can only be used with --delete all")
    return args


# --------------------------------------------------------- name indices (es)


def indices_ensure(es, index_names):
    """ensure the tag name index and the type name index on the local es
    service ('char' config: case-insensitive substring search over the name,
    user_id as exact filter). the worker on the home server must be running;
    without a confirmed index the ensure fails, matching the api behavior
    (creation fails when the name is not indexed)."""
    for index_name in (index_names["index_tag"], index_names["index_type"]):
        result = es.index_ensure(index_name, ES_INDEX_CONFIG_NAME, ES_FIELD_CONFIG)
        if result["code"] != 0:
            raise SystemExit(
                f"index ensure failed for {index_name}: {result.get('message')}"
            )
        state = "created" if result["data"]["is_created"] else "already exists, ok"
        print(f"index {index_name}: {state}")


def indices_delete(es, index_names):
    """delete the two name indices. failures (typically the worker being
    down) are reported but do not stop the aws-side deletion."""
    for index_name in (index_names["index_tag"], index_names["index_type"]):
        result = es.index_delete(index_name)
        if result["code"] != 0:
            print(
                f"index delete FAILED for {index_name}: {result.get('message')}"
                " -- delete it later via _2_local_es/es_client.py index-delete"
            )
        elif result["data"]["is_deleted"]:
            print(f"index deleted: {index_name}")
        else:
            print(f"index does not exist: {index_name}")


# --------------------------------------------------------------------- tables


def entity_table_ensure(dynamodb, table_name, id_attr):
    """basic info of each tag/type, one row per tag/type, keyed by owner. the
    parent of a tag/type is a plain parent_id attribute (absent = root), so
    the whole tree of a user is one partition query; children lookups and
    name search filter in lambda."""
    table_ensure(
        dynamodb,
        table_name,
        attribute_definitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": id_attr, "AttributeType": "S"},
        ],
        key_schema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": id_attr, "KeyType": "RANGE"},
        ],
    )


def entity_history_table_ensure(dynamodb, table_name, id_attr):
    """edit history of one tag/type, one partition per tag/type. the sort key
    time_key is '{time_stamp zero-padded to 16}#{random}', so the history of
    a tag/type is one query in time order."""
    table_ensure(
        dynamodb,
        table_name,
        attribute_definitions=[
            {"AttributeName": id_attr, "AttributeType": "S"},
            {"AttributeName": "time_key", "AttributeType": "S"},
        ],
        key_schema=[
            {"AttributeName": id_attr, "KeyType": "HASH"},
            {"AttributeName": "time_key", "KeyType": "RANGE"},
        ],
    )


def obj_table_ensure(dynamodb, table_name, id_attr):
    """attach entries: one row per (obj, tag/type), with a lexorank giving
    the order of the tags/types of one obj. gsi_{id_attr} answers 'which
    objs have this tag/type' (and locates entries when a tag/type is
    deleted); the INCLUDE projection carries user_id only."""
    table_ensure(
        dynamodb,
        table_name,
        attribute_definitions=[
            {"AttributeName": "obj_id", "AttributeType": "S"},
            {"AttributeName": id_attr, "AttributeType": "S"},
        ],
        key_schema=[
            {"AttributeName": "obj_id", "KeyType": "HASH"},
            {"AttributeName": id_attr, "KeyType": "RANGE"},
        ],
        gsi_list=[
            {
                "IndexName": f"gsi_{id_attr}",
                "KeySchema": [{"AttributeName": id_attr, "KeyType": "HASH"}],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": ["user_id"],
                },
            }
        ],
    )


def obj_history_table_ensure(dynamodb, table_name):
    """edit history of the tags/types of one obj (attach/reorder/detach), one
    partition per obj, sort key time_key as in the entity history table.
    deleting history earlier than a time point is one key-condition query
    plus a batch delete."""
    table_ensure(
        dynamodb,
        table_name,
        attribute_definitions=[
            {"AttributeName": "obj_id", "AttributeType": "S"},
            {"AttributeName": "time_key", "AttributeType": "S"},
        ],
        key_schema=[
            {"AttributeName": "obj_id", "KeyType": "HASH"},
            {"AttributeName": "time_key", "KeyType": "RANGE"},
        ],
    )


def tag_type_tables_ensure(dynamodb, names):
    """the four tables of the tag side and the four tables of the type side.
    tag and type are kept fully independent at data level, so each side owns
    a whole table set of identical shape."""
    for kind in ("tag", "type"):
        entity_table_ensure(dynamodb, names[f"table_{kind}"], f"{kind}_id")
        entity_history_table_ensure(dynamodb, names[f"table_{kind}_history"], f"{kind}_id")
        obj_table_ensure(dynamodb, names[f"table_obj_{kind}"], f"{kind}_id")
        obj_history_table_ensure(dynamodb, names[f"table_obj_{kind}_history"])


# ---------------------------------------------------------------- role/lambda


def api_role_ensure(iam, names, region, account_id, es_gen):
    """execution role of the api lambda: cloudwatch logs, read of the user
    table (owned by _0_auth_cognito), full item access on all eight tag/type
    tables, plus the requester side of the local es service (send index
    tasks, read results). returns the role arn."""
    resource_list = []
    for table_key in TABLE_KEY_LIST:
        table_arn = f"arn:aws:dynamodb:{region}:{account_id}:table/{names[table_key]}"
        resource_list.append(table_arn)
        resource_list.append(f"{table_arn}/index/*")
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
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
                "Sid": "TagTypeTables",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:Query",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:BatchWriteItem",
                ],
                "Resource": resource_list,
            },
            {
                "Sid": "EsTaskSend",
                "Effect": "Allow",
                "Action": ["sqs:SendMessage"],
                "Resource": es_gen["queue_task"]["queue_arn"],
            },
            {
                "Sid": "EsResultRead",
                "Effect": "Allow",
                "Action": ["dynamodb:GetItem"],
                "Resource": es_gen["table_result"]["table_arn"],
            },
        ],
    }
    return lambda_role_ensure(iam, names["lambda_role"], policy)


def lambda_env_build(names, config, es_gen, index_names):
    service_config = config["tag_type"]
    return {
        "TABLE_TAG": names["table_tag"],
        "TABLE_TAG_HISTORY": names["table_tag_history"],
        "TABLE_OBJ_TAG": names["table_obj_tag"],
        "TABLE_OBJ_TAG_HISTORY": names["table_obj_tag_history"],
        "TABLE_TYPE": names["table_type"],
        "TABLE_TYPE_HISTORY": names["table_type_history"],
        "TABLE_OBJ_TYPE": names["table_obj_type"],
        "TABLE_OBJ_TYPE_HISTORY": names["table_obj_type_history"],
        "TABLE_USER": names["table_user"],
        "GROUP_ACCESS": service_config["cognito"]["group_access"],
        "GROUP_ADMIN": service_config["cognito"]["group_admin"],
        "INDEX_TAG": index_names["index_tag"],
        "INDEX_TYPE": index_names["index_type"],
        "ES_QUEUE_URL": es_gen["queue_task"]["queue_url"],
        "ES_RESULT_TABLE": es_gen["table_result"]["table_name"],
        "ES_REGION": es_gen["region_name"],
        "ES_RESULT_TIMEOUT_SEC": str(ES_RESULT_TIMEOUT_SEC),
    }


def api_lambda_ensure(lambda_client, names, config, role_arn, es_gen, index_names):
    # es_client.py of _2_local_es is packaged into the zip: the lambda uses
    # it as the requester library of the local es service
    return lambda_function_ensure(
        lambda_client,
        names["lambda_function"],
        config["tag_type"]["lambda"],
        role_arn,
        lambda_env_build(names, config, es_gen, index_names),
        lambda_zip_build(DIR_BACKEND, [PATH_LOCAL_ES_CLIENT]),
    )


# --------------------------------------------------------------------- delete


def architecture_delete(config, names, is_prefix_assumed):
    delete_confirm(
        f"All tag/type AWS resources with prefix {config['name_prefix']!r} "
        "will be deleted."
    )

    dynamodb = aws_client_make(config, "dynamodb")
    iam = aws_client_make(config, "iam")
    lambda_client = aws_client_make(config, "lambda")
    apigw = aws_client_make(config, "apigatewayv2")

    http_api_delete(apigw, names["http_api"])
    lambda_delete(lambda_client, names["lambda_function"])
    lambda_role_delete(iam, names["lambda_role"])
    for table_key in TABLE_KEY_LIST:
        table_delete(dynamodb, names[table_key])

    # the name indices do not carry the aws name prefix, so when deleting an
    # OLD architecture under --assume-prefix they may still be in use by the
    # currently deployed one: only the deletion of the current architecture
    # (no assumed prefix) deletes them
    if is_prefix_assumed:
        print("indices kept: --assume-prefix deletes only the aws resources")
        return
    es = local_es_client_make(config, local_es_gen_load())
    indices_delete(es, index_names_build())


# ----------------------------------------------------------------------- main


def architecture_ensure(config, names):
    region = config["aws"]["region_name"]

    cognito_gen = cognito_gen_load()
    pool_id = cognito_gen["cognito"]["user_pool_id"]
    app_client = config["tag_type"]["cognito"]["app_client"]
    client_id = cognito_gen["cognito"]["app_client_ids"][app_client]
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"

    # the user table belongs to _0_auth_cognito (own name prefix), so its
    # actual name is read from that sub-project's generated config
    if "user_table" not in cognito_gen:
        raise SystemExit(
            "user_table not found in _0_auth_cognito/config_gen.yaml, "
            "run _0_auth_cognito/ensure_user_table.py first"
        )
    names["table_user"] = cognito_gen["user_table"]["table_name"]

    dynamodb = aws_client_make(config, "dynamodb")
    iam = aws_client_make(config, "iam")
    lambda_client = aws_client_make(config, "lambda")
    apigw = aws_client_make(config, "apigatewayv2")
    sts = aws_client_make(config, "sts")
    account_id = sts.get_caller_identity()["Account"]

    # name indices first: the write apis refuse to work while the index is
    # not reachable, so a deploy with the es worker down fails here, before
    # any aws resource is touched
    es_gen = local_es_gen_load()
    index_names = index_names_build()
    es = local_es_client_make(config, es_gen)
    indices_ensure(es, index_names)

    tag_type_tables_ensure(dynamodb, names)

    role_arn = api_role_ensure(iam, names, region, account_id, es_gen)
    lambda_arn = api_lambda_ensure(
        lambda_client, names, config, role_arn, es_gen, index_names
    )

    api_id, api_endpoint = http_api_ensure(apigw, names["http_api"])
    authorizer_id = jwt_authorizer_ensure(apigw, api_id, issuer, client_id)
    integration_id = lambda_integration_ensure(apigw, api_id, lambda_arn)
    api_route_ensure(apigw, api_id, ROUTE_KEY_API, integration_id, authorizer_id)
    api_stage_ensure(apigw, api_id)
    lambda_invoke_permission_ensure(
        lambda_client, names["lambda_function"], region, account_id, api_id
    )

    config_gen = config_gen_load()
    config_gen["tag_type"] = {
        **{table_key: names[table_key] for table_key in TABLE_KEY_LIST},
        "index_tag": index_names["index_tag"],
        "index_type": index_names["index_type"],
        "lambda_arn": lambda_arn,
        "api_id": api_id,
        "api_endpoint": api_endpoint,
        "cognito_client_id": client_id,
        "cognito_issuer": issuer,
    }
    config_gen_save(config_gen)
    print(f"saved to config_gen.yaml: api endpoint {api_endpoint}")


def main():
    args = args_parse()
    config = config_load()
    if args.assume_prefix is not None:
        config["name_prefix"] = args.assume_prefix
    names = names_build(config)

    if args.delete == "all":
        architecture_delete(config, names, args.assume_prefix is not None)
        return
    architecture_ensure(config, names)


if __name__ == "__main__":
    main()
