# ensure all aws resource instances of the asset timeline service, in
# dependency order:
#   timeline table + timeline-asset table -> lambda role -> lambda function
#   -> http api (jwt authorizer, route)
#
# 'ensure' means: create if missing, update if config differs, never recreate.
# generated ids are saved to config_gen.yaml. no frontend resources yet
# (no web bucket / cloudfront); clients call the http api endpoint directly.
#
# generic ensure/delete logic lives in /aws_utils/ (see
# /doc/aws_oa_impl.md#shared-utilities-aws_utils); this script only holds
# what is specific to this service: table schemas, role policy, lambda env.
#
# cognito ids come from ../_0_auth_cognito/config_gen.yaml (run
# ensure_cognito.py + ensure_user_table.py there first).
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
    aws_client_make,
    cognito_gen_load,
    config_gen_load,
    config_gen_save,
    config_load,
    names_build,
)

DIR_BACKEND = DIR_SELF / "backend"

ROUTE_KEY_API = "ANY /api/{proxy+}"


def args_parse():
    parser = argparse.ArgumentParser(
        description="ensure or delete the asset timeline architecture"
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


# --------------------------------------------------------------------- tables


def timeline_table_ensure(dynamodb, table_name):
    """basic info of each timeline, one row per timeline, keyed by owner."""
    table_ensure(
        dynamodb,
        table_name,
        attribute_definitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "timeline_id", "AttributeType": "S"},
        ],
        key_schema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "timeline_id", "KeyType": "RANGE"},
        ],
    )


def timeline_asset_table_ensure(dynamodb, table_name):
    """collect relationships, one row per (timeline, asset). the sort key
    time_key is '{time_stamp zero-padded to 16}#{asset_id}', so range and
    neighbor queries are plain sort key conditions. gsi_asset_id answers
    'which timelines collect this asset' and locates an entry by
    (timeline_id, asset_id); INCLUDE keeps the projection small while
    letting reads skip a second lookup of the time point."""
    table_ensure(
        dynamodb,
        table_name,
        attribute_definitions=[
            {"AttributeName": "timeline_id", "AttributeType": "S"},
            {"AttributeName": "time_key", "AttributeType": "S"},
            {"AttributeName": "asset_id", "AttributeType": "S"},
        ],
        key_schema=[
            {"AttributeName": "timeline_id", "KeyType": "HASH"},
            {"AttributeName": "time_key", "KeyType": "RANGE"},
        ],
        gsi_list=[
            {
                "IndexName": "gsi_asset_id",
                "KeySchema": [{"AttributeName": "asset_id", "KeyType": "HASH"}],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": ["user_id", "time_stamp", "time_stamp_timezone"],
                },
            }
        ],
    )


# ---------------------------------------------------------------- role/lambda


def api_role_ensure(iam, names, region, account_id):
    """execution role of the api lambda: cloudwatch logs, read of the user
    table (owned by _0_auth_cognito), full item access on both timeline
    tables. returns the role arn."""
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
                "Sid": "TimelineTables",
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
                    f"arn:aws:dynamodb:{region}:{account_id}:table/{names['table_timeline']}",
                    f"arn:aws:dynamodb:{region}:{account_id}:table/{names['table_timeline']}/index/*",
                    f"arn:aws:dynamodb:{region}:{account_id}:table/{names['table_timeline_asset']}",
                    f"arn:aws:dynamodb:{region}:{account_id}:table/{names['table_timeline_asset']}/index/*",
                ],
            },
        ],
    }
    return lambda_role_ensure(iam, names["lambda_role"], policy)


def lambda_env_build(names, config):
    service_config = config["asset_timeline"]
    return {
        "TABLE_TIMELINE": names["table_timeline"],
        "TABLE_TIMELINE_ASSET": names["table_timeline_asset"],
        "TABLE_USER": names["table_user"],
        "GROUP_ACCESS": service_config["cognito"]["group_access"],
        "GROUP_ADMIN": service_config["cognito"]["group_admin"],
    }


def api_lambda_ensure(lambda_client, names, config, role_arn):
    return lambda_function_ensure(
        lambda_client,
        names["lambda_function"],
        config["asset_timeline"]["lambda"],
        role_arn,
        lambda_env_build(names, config),
        lambda_zip_build(DIR_BACKEND),
    )


# --------------------------------------------------------------------- delete


def architecture_delete(config, names):
    delete_confirm(
        f"All asset timeline AWS resources with prefix {config['name_prefix']!r} "
        "will be deleted."
    )

    dynamodb = aws_client_make(config, "dynamodb")
    iam = aws_client_make(config, "iam")
    lambda_client = aws_client_make(config, "lambda")
    apigw = aws_client_make(config, "apigatewayv2")

    http_api_delete(apigw, names["http_api"])
    lambda_delete(lambda_client, names["lambda_function"])
    lambda_role_delete(iam, names["lambda_role"])
    table_delete(dynamodb, names["table_timeline_asset"])
    table_delete(dynamodb, names["table_timeline"])


# ----------------------------------------------------------------------- main


def architecture_ensure(config, names):
    region = config["aws"]["region_name"]

    cognito_gen = cognito_gen_load()
    pool_id = cognito_gen["cognito"]["user_pool_id"]
    app_client = config["asset_timeline"]["cognito"]["app_client"]
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

    timeline_table_ensure(dynamodb, names["table_timeline"])
    timeline_asset_table_ensure(dynamodb, names["table_timeline_asset"])

    role_arn = api_role_ensure(iam, names, region, account_id)
    lambda_arn = api_lambda_ensure(lambda_client, names, config, role_arn)

    api_id, api_endpoint = http_api_ensure(apigw, names["http_api"])
    authorizer_id = jwt_authorizer_ensure(apigw, api_id, issuer, client_id)
    integration_id = lambda_integration_ensure(apigw, api_id, lambda_arn)
    api_route_ensure(apigw, api_id, ROUTE_KEY_API, integration_id, authorizer_id)
    api_stage_ensure(apigw, api_id)
    lambda_invoke_permission_ensure(
        lambda_client, names["lambda_function"], region, account_id, api_id
    )

    config_gen = config_gen_load()
    config_gen["asset_timeline"] = {
        "table_timeline": names["table_timeline"],
        "table_timeline_asset": names["table_timeline_asset"],
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
        architecture_delete(config, names)
        return
    architecture_ensure(config, names)


if __name__ == "__main__":
    main()
