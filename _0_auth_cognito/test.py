# lifecycle test of the cognito sub-project:
#
#   cognito    creates, re-ensures, then deletes a temporary user pool
#   user-table creates, operates on, then deletes a temporary user table
#
# run all items:       python test.py
# run selected items:  python test.py cognito user-table

import argparse
import copy
import sys
from pathlib import Path

from boto3.dynamodb.conditions import Key

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aws_utils import TestFail, check, check_count, step, timestamp_make
from ensure_cognito import (
    app_clients_ensure,
    cognito_client_make,
    config_load,
    domain_ensure,
    groups_ensure,
    public_signup_ensure,
    resource_servers_ensure,
    user_pool_ensure,
    user_pool_find,
)
from ensure_user_table import (
    AUTH_SYSTEM_COGNITO,
    AUTH_SYSTEM_SCRIPT,
    GSI_AUTH_ID,
    dynamodb_resource_make,
    user_id_generate,
    user_table_ensure,
)

ITEM_LIST = ["cognito", "user-table"]


def test_run(item_list):
    config = config_load()
    prefix_temp = prefix_temp_make(config["name_prefix"])

    if "cognito" in item_list:
        test_cognito(config, prefix_temp)
    if "user-table" in item_list:
        test_user_table(config, prefix_temp)

    print(f"\nall {check_count()} checks passed")


def test_cognito(config, prefix_temp):
    step("cognito: temporary user pool create -> re-ensure -> delete")
    cognito = cognito_client_make(config)
    pool_config = copy.deepcopy(config["cognito"]["user_pool"])
    pool_name_suffix = f"_{prefix_temp}"
    pool_name_base = pool_config["name"][: 128 - len(pool_name_suffix)]
    pool_config["name"] = f"{pool_name_base}{pool_name_suffix}"
    pool_config["users"] = []
    if "domain" in pool_config:
        domain_base = pool_config["domain"]["prefix"]
        timestamp = prefix_temp.rsplit("_temp_", 1)[1]
        timestamp_domain = timestamp.replace("_", "-").replace("+", "p")
        domain_suffix = f"-temp-{timestamp_domain}"
        domain_base = domain_base[: 63 - len(domain_suffix)].rstrip("-")
        pool_config["domain"]["prefix"] = f"{domain_base}{domain_suffix}"

    pool_name = pool_config["name"]
    domain = pool_config.get("domain", {}).get("prefix")
    if user_pool_find(cognito, pool_name) is not None:
        raise SystemExit(f"test suspended: user pool already exists: {pool_name}")
    if domain is not None:
        domain_desc = cognito.describe_user_pool_domain(Domain=domain).get(
            "DomainDescription", {}
        )
        if domain_desc.get("UserPoolId"):
            raise SystemExit(f"test suspended: user pool domain already exists: {domain}")

    pool_id = None
    try:
        pool_id = user_pool_ensure(cognito, pool_config)
        resource_servers_ensure(
            cognito, pool_id, pool_config.get("resource_servers", [])
        )
        client_ids = app_clients_ensure(
            cognito, pool_id, pool_config.get("app_clients", {})
        )
        public_signup_ensure(cognito, pool_id, pool_config)
        groups_ensure(cognito, pool_id, pool_config.get("groups", []))
        if domain is not None:
            domain_ensure(cognito, pool_id, pool_config["domain"])

        check(
            user_pool_find(cognito, pool_name) == pool_id,
            "temporary user pool exists under the expected name",
        )

        pool_id_again = user_pool_ensure(cognito, pool_config)
        client_ids_again = app_clients_ensure(
            cognito, pool_id, pool_config.get("app_clients", {})
        )
        check(pool_id_again == pool_id, "re-ensure keeps the same user pool")
        check(client_ids_again == client_ids, "re-ensure keeps the same app clients")
    finally:
        if pool_id is not None:
            pool = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
            if pool.get("Domain"):
                cognito.delete_user_pool_domain(
                    UserPoolId=pool_id, Domain=pool["Domain"]
                )
            cognito.delete_user_pool(UserPoolId=pool_id)
            print(f"\ntemporary user pool deleted: {pool_name}")


def test_user_table(config, prefix_temp):
    step("user-table: temporary table create -> put/query -> delete")
    table_name = f"{prefix_temp.replace('+', 'p')}-user"
    dynamodb = dynamodb_resource_make(config)
    db = dynamodb.meta.client
    try:
        db.describe_table(TableName=table_name)
        raise SystemExit(f"test suspended: user table already exists: {table_name}")
    except db.exceptions.ResourceNotFoundException:
        pass

    is_created = False
    try:
        user_table_ensure(dynamodb, table_name)
        is_created = True
        user_table_ensure(dynamodb, table_name)
        table = dynamodb.Table(table_name)
        user_id = user_id_generate()
        auth_id = f"{AUTH_SYSTEM_COGNITO}#test-{prefix_temp}"
        table.put_item(
            Item={
                "user_id": user_id,
                "auth_id": AUTH_SYSTEM_SCRIPT,
                "auth_system_type": AUTH_SYSTEM_SCRIPT,
            }
        )
        table.put_item(
            Item={
                "user_id": user_id,
                "auth_id": auth_id,
                "auth_system_type": AUTH_SYSTEM_COGNITO,
                "user_id_from_auth": f"test-{prefix_temp}",
            }
        )
        mappings = table.query(
            IndexName=GSI_AUTH_ID,
            KeyConditionExpression=Key("auth_id").eq(auth_id),
        )["Items"]
        check(len(mappings) == 1, "temporary cognito identity has one mapping")
        check(mappings[0]["user_id"] == user_id, "mapping resolves the internal user id")
        origin = table.get_item(
            Key={"user_id": user_id, "auth_id": AUTH_SYSTEM_SCRIPT}
        ).get("Item")
        check(origin is not None, "internal user has the script origin row")
    finally:
        if is_created:
            db.delete_table(TableName=table_name)
            print(f"\ntemporary user table deletion started: {table_name}")


def prefix_temp_make(prefix):
    timestamp = timestamp_make()
    return f"{prefix}_temp_{timestamp}"


def args_parse():
    parser = argparse.ArgumentParser(description="test deployed cognito resources")
    parser.add_argument(
        "items",
        nargs="*",
        choices=ITEM_LIST,
        help=f"test items; defaults to all: {', '.join(ITEM_LIST)}",
    )
    args = parser.parse_args()
    return args.items or ITEM_LIST


if __name__ == "__main__":
    try:
        test_run(args_parse())
    except TestFail:
        raise SystemExit("\ntest FAILED")
