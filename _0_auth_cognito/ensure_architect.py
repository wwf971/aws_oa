import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aws_utils import delete_confirm
from ensure_cognito import (
    cognito_client_make,
    config_load,
    main as cognito_ensure,
    user_pool_find,
)
from ensure_user_table import dynamodb_resource_make, main as user_table_ensure


def args_parse():
    parser = argparse.ArgumentParser(
        description="ensure or delete the test cognito architecture"
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


def architecture_delete(config):
    prefix = config["name_prefix"]
    pool_name = config["cognito"]["user_pool"]["name"]
    table_name = f"{prefix}-user"
    delete_confirm(
        f"Test cognito AWS resources will be deleted: prefix {prefix!r}, "
        f"user pool {pool_name!r}."
    )

    dynamodb = dynamodb_resource_make(config)
    try:
        dynamodb.meta.client.delete_table(TableName=table_name)
        print(f"user table deletion started: {table_name}")
    except dynamodb.meta.client.exceptions.ResourceNotFoundException:
        print(f"user table does not exist: {table_name}")

    cognito = cognito_client_make(config)
    pool_id = user_pool_find(cognito, pool_name)
    if pool_id is None:
        print(f"user pool does not exist: {pool_name}")
    else:
        pool = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
        domain = pool.get("Domain")
        if domain is not None:
            cognito.delete_user_pool_domain(UserPoolId=pool_id, Domain=domain)
            print(f"user pool domain deleted: {domain}")
        cognito.delete_user_pool(UserPoolId=pool_id)
        print(f"user pool deleted: {pool_name} ({pool_id})")


def architecture_ensure():
    cognito_ensure()
    user_table_ensure()


def main():
    args = args_parse()
    if args.delete != "all":
        architecture_ensure()
        return

    config = config_load()
    if args.assume_prefix is not None:
        config["name_prefix"] = args.assume_prefix
    architecture_delete(config)


if __name__ == "__main__":
    main()
