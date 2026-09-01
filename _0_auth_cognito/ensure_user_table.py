# ensure the dynamodb user table (source of truth of user identity) and the
# mapping rows for users listed in config. see test_cognito_impl.md#user-table-dynamodb
#
#   ensure table -> for each config user: ensure mapping rows -> save user ids
#
# table name is {name_prefix}-user, prefix from ./config.yaml (each service
# has its own prefix). other services find this table name via config_gen.yaml.
# run ensure_cognito.py first: this script reads user pool id from config_gen.yaml.

import secrets
import string
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aws_utils import table_ensure
from ensure_cognito import (
    cognito_client_make,
    config_gen_load,
    config_gen_save,
    config_load,
)

GSI_AUTH_ID = "gsi_auth_id"
USER_ID_LENGTH = 16
USER_ID_CHARS = string.digits + string.ascii_lowercase

# auth_system_type value of the special row marking a script-created user.
# that row has no user_id_from_auth, so its auth_id is just "script".
AUTH_SYSTEM_SCRIPT = "script"
AUTH_SYSTEM_COGNITO = "cognito"


def user_id_generate():
    return "".join(secrets.choice(USER_ID_CHARS) for _ in range(USER_ID_LENGTH))


def dynamodb_resource_make(config):
    aws = config["aws"]
    return boto3.resource(
        "dynamodb",
        region_name=aws["region_name"],
        aws_access_key_id=aws["access_key_id"],
        aws_secret_access_key=aws["secret_access_key"],
    )


# ---------------------------------------------------------------------- table


def user_table_ensure(dynamodb, table_name):
    table_ensure(
        dynamodb.meta.client,
        table_name,
        attribute_definitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "auth_id", "AttributeType": "S"},
        ],
        key_schema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "auth_id", "KeyType": "RANGE"},
        ],
        gsi_list=[
            {
                "IndexName": GSI_AUTH_ID,
                "KeySchema": [{"AttributeName": "auth_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            }
        ],
    )


# ----------------------------------------------------------------------- rows


def cognito_sub_get(cognito, pool_id, username):
    user = cognito.admin_get_user(UserPoolId=pool_id, Username=username)
    for attr in user["UserAttributes"]:
        if attr["Name"] == "sub":
            return attr["Value"]
    raise ValueError(f"user {username} has no sub attribute")


def user_mapping_ensure(table, pool_id, cognito, user_config):
    """ensure mapping rows of one config user. returns the user_id."""
    username = user_config["username"]
    sub = cognito_sub_get(cognito, pool_id, username)
    auth_id_cognito = f"{AUTH_SYSTEM_COGNITO}#{sub}"

    found = table.query(
        IndexName=GSI_AUTH_ID,
        KeyConditionExpression=Key("auth_id").eq(auth_id_cognito),
    )["Items"]
    if found:
        user_id = found[0]["user_id"]
        print(f"user {username}: mapping ok (user_id {user_id})")
        return user_id

    user_id = user_id_generate()
    created_at = datetime.now(timezone.utc).isoformat()
    # special row: this user originates from this script (AdminCreateUser),
    # not from any auth service, so there is no user_id_from_auth.
    table.put_item(
        Item={
            "user_id": user_id,
            "auth_id": AUTH_SYSTEM_SCRIPT,
            "auth_system_type": AUTH_SYSTEM_SCRIPT,
            "username": username,
            "created_at": created_at,
        }
    )
    table.put_item(
        Item={
            "user_id": user_id,
            "auth_id": auth_id_cognito,
            "auth_system_type": AUTH_SYSTEM_COGNITO,
            "user_id_from_auth": sub,
            "username": username,
            "email": user_config["email"],
            "created_at": created_at,
        }
    )
    print(f"user {username}: mapping created (user_id {user_id}, sub {sub})")
    return user_id


# ----------------------------------------------------------------------- main


def main():
    config = config_load()
    table_name = f"{config['name_prefix']}-user"

    config_gen = config_gen_load()
    pool_id = config_gen.get("cognito", {}).get("user_pool_id")
    if pool_id is None:
        raise SystemExit("user pool id not found in config_gen.yaml, run ensure_cognito.py first")

    dynamodb = dynamodb_resource_make(config)
    cognito = cognito_client_make(config)

    user_table_ensure(dynamodb, table_name)
    table = dynamodb.Table(table_name)

    user_ids = {}
    for user_config in config["cognito"]["user_pool"].get("users", []):
        username = user_config["username"]
        user_ids[username] = user_mapping_ensure(table, pool_id, cognito, user_config)

    config_gen["user_table"] = {"table_name": table_name, "user_ids": user_ids}
    config_gen_save(config_gen)
    print(f"saved to config_gen.yaml: user table {table_name}, user ids {user_ids}")


if __name__ == "__main__":
    main()
