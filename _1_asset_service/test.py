# end-to-end deployment test of the asset service:
#
#   frontend checks the cloudfront pages and api gateway jwt protection
#   backend  invokes the deployed lambda and runs folder + file CRUD against
#            the real dynamodb and s3 resources, then removes its test data
#
# run all items:       python test.py
# run selected items:  python test.py frontend backend

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aws_utils import TestFail, check, check_count, step, timestamp_make
from config_gen import (
    aws_client_make,
    cognito_gen_load,
    config_gen_load,
    config_load,
    names_build,
)

ITEM_LIST = ["frontend", "backend"]
CONTENT_TEST = b"asset service deployment test\n"


def test_run(item_list):
    config = config_load()
    config_gen = config_gen_load()["asset_service"]
    names = names_build(config)

    if "frontend" in item_list:
        test_frontend(config_gen)
    if "backend" in item_list:
        test_backend(config, names)

    print(f"\nall {check_count()} checks passed")


def test_frontend(config_gen):
    step("frontend: cloudfront pages and api authorization")
    base_url = f"https://{config_gen['cloudfront_domain']}"

    for path in ("/", "/main/"):
        response = urlopen(f"{base_url}{path}", timeout=30)
        content = response.read()
        check(response.status == 200, f"{path} returns http 200")
        check(b"<html" in content.lower(), f"{path} returns an html page")

    try:
        urlopen(f"{base_url}/api/me", timeout=30)
        status = 200
    except HTTPError as error:
        status = error.code
    check(status in (401, 403), "/api/me rejects a request without a jwt")


def test_backend(config, names):
    step("backend: deployed lambda folder and file CRUD")
    lambda_client = aws_client_make(config, "lambda")
    cognito = aws_client_make(config, "cognito-idp")
    claims = admin_claims_get(config, cognito)
    function_name = names["lambda_function"]
    timestamp = timestamp_make()
    name_temp = f"{config['name_prefix']}_temp_{timestamp}"
    folder_id = None

    try:
        response = lambda_api_call(
            lambda_client, function_name, claims, "GET", "/api/me"
        )
        check(response["code"] == 0, f"me request succeeds: {response}")
        check(
            response["data"]["role"] == "admin",
            "selected test user has the admin role",
        )

        response = lambda_api_call(
            lambda_client, function_name, claims, "GET", "/api/tree"
        )
        check(response["code"] == 0, f"initial tree request succeeds: {response}")
        for node in response.get("data", {}).get("nodes", []):
            if node["name"] == name_temp and "asset_id" not in node:
                raise SystemExit(
                    f"test suspended: tree folder already exists: {name_temp}"
                )

        response = lambda_api_call(
            lambda_client,
            function_name,
            claims,
            "POST",
            "/api/folder",
            {"name": name_temp},
        )
        check(response["code"] == 0, f"test folder creation succeeds: {response}")
        folder_id = response["data"]["node"]["node_id"]

        response = lambda_api_call(
            lambda_client,
            function_name,
            claims,
            "POST",
            "/api/asset",
            {
                "name": f"{name_temp}.txt",
                "parent_id": folder_id,
                "asset_type": "file",
                "files": [{"path": "test.txt", "content_type": "text/plain"}],
            },
        )
        check(response["code"] == 0, f"file asset creation succeeds: {response}")
        asset = response["data"]["node"]
        upload = response["data"]["upload_urls"][0]

        request = Request(
            upload["url"],
            data=CONTENT_TEST,
            headers=upload["headers"],
            method="PUT",
        )
        response_http = urlopen(request, timeout=30)
        check(response_http.status == 200, "file upload through presigned url succeeds")

        response = lambda_api_call(
            lambda_client,
            function_name,
            claims,
            "POST",
            "/api/asset-complete",
            {"node_id": asset["node_id"]},
        )
        check(response["code"] == 0, f"asset completion succeeds: {response}")
        check(
            response["data"]["node"]["size"] == len(CONTENT_TEST),
            "completed asset size matches uploaded content",
        )

        response = lambda_api_call(
            lambda_client,
            function_name,
            claims,
            "PATCH",
            f"/api/node/{asset['node_id']}",
            {"name": f"{name_temp}_renamed.txt"},
        )
        check(response["code"] == 0, f"asset rename succeeds: {response}")

        response = lambda_api_call(
            lambda_client,
            function_name,
            claims,
            "GET",
            f"/api/download/{asset['node_id']}",
        )
        check(response["code"] == 0, f"download url request succeeds: {response}")
        content_downloaded = urlopen(response["data"]["url"], timeout=30).read()
        check(content_downloaded == CONTENT_TEST, "downloaded file content matches upload")

        response = lambda_api_call(
            lambda_client, function_name, claims, "GET", "/api/tree"
        )
        check(response["code"] == 0, f"tree request succeeds: {response}")
        node_ids = {
            node["node_id"]
            for node in response.get("data", {}).get("nodes", [])
        }
        check(folder_id in node_ids, "tree contains the test folder")
        check(asset["node_id"] in node_ids, "tree contains the test asset")

        response = lambda_api_call(
            lambda_client,
            function_name,
            claims,
            "DELETE",
            f"/api/node/{folder_id}",
        )
        check(response["code"] == 0, f"test subtree deletion succeeds: {response}")
        folder_id = None
    finally:
        if folder_id is not None:
            lambda_api_call(
                lambda_client,
                function_name,
                claims,
                "DELETE",
                f"/api/node/{folder_id}",
            )
            print(f"\nresidue test folder {folder_id} cleaned up")


def admin_claims_get(config, cognito):
    cognito_gen = cognito_gen_load()
    pool_id = cognito_gen["cognito"]["user_pool_id"]
    group_access = config["asset_service"]["cognito"]["group_access"]
    group_admin = config["asset_service"]["cognito"]["group_admin"]

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


def lambda_api_call(lambda_client, function_name, claims, method, path, body=None):
    event = {
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": claims}},
        },
        "rawPath": path,
    }
    if body is not None:
        event["body"] = json.dumps(body)

    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode(),
    )
    payload = json.loads(response["Payload"].read())
    if response.get("FunctionError"):
        raise TestFail(f"lambda invocation failed: {payload}")
    return json.loads(payload["body"])


def args_parse():
    parser = argparse.ArgumentParser(description="test the deployed asset service")
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
