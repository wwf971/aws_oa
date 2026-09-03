# ensure all aws resource instances of the asset service, in dependency order:
#   s3 buckets -> asset-node table -> lambda role -> lambda function
#   -> http api (jwt authorizer, route) -> cloudfront distribution
#   -> web bucket policy (cloudfront-only access)
#
# 'ensure' means: create if missing, update if config differs, never recreate.
# generated ids are saved to config_gen.yaml. run order and iam permissions:
# see asset_service_impl.md
#
# generic ensure/delete logic lives in /aws_utils/ (see
# /doc/aws_oa_impl.md#shared-utilities-aws_utils); this script only holds
# what is specific to this service: table schema, role policy, lambda env,
# and the cloudfront setup (only this service has a frontend so far).
#
# cognito ids come from ../_0_auth_cognito/config_gen.yaml (run
# ensure_cognito.py + ensure_user_table.py there first).

import argparse
import copy
import json
import sys
import uuid
from pathlib import Path

DIR_SELF = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR_SELF.parent))

from aws_utils import (
    api_route_ensure,
    api_stage_ensure,
    bucket_delete,
    bucket_ensure,
    delete_confirm,
    dict_update_deep,
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

ZIP_TMP_EXPIRE_DAYS = 1

# aws managed policy ids (fixed, same in every account)
CACHE_POLICY_CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"
CACHE_POLICY_CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
ORIGIN_REQUEST_POLICY_ALL_VIEWER_EXCEPT_HOST = "b689b0a8-53d0-40ab-baf2-68738e2966ac"

ROUTE_KEY_API = "ANY /api/{proxy+}"

# cloudfront DefaultRootObject only covers the root "/". for "/main/" the s3
# origin is asked for the literal key "main/", which does not exist, and s3
# answers AccessDenied. this viewer-request function rewrites directory-style
# urls to their index.html object. it runs only on the s3 behavior (default),
# never on /api/*.
FUNCTION_URL_REWRITE_CODE = """\
function handler(event) {
    var request = event.request;
    if (request.uri.endsWith('/')) {
        request.uri += 'index.html';
    } else if (!request.uri.split('/').pop().includes('.')) {
        request.uri += '/index.html';
    }
    return request;
}
"""


def args_parse():
    parser = argparse.ArgumentParser(
        description="ensure or delete the asset service architecture"
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


# ------------------------------------------------------------------ s3 buckets
# bucket_ensure() itself lives in /aws_utils/ (shared with ensure_frontend.py)


def bucket_asset_config_ensure(s3, bucket_name):
    """cors for browser presigned upload/download + lifecycle for zip-tmp/."""
    s3.put_bucket_cors(
        Bucket=bucket_name,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedOrigins": ["*"],
                    "AllowedMethods": ["GET", "PUT", "HEAD"],
                    "AllowedHeaders": ["*"],
                    "ExposeHeaders": ["ETag"],
                    "MaxAgeSeconds": 3600,
                }
            ]
        },
    )
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket_name,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-zip-tmp",
                    "Filter": {"Prefix": "zip-tmp/"},
                    "Status": "Enabled",
                    "Expiration": {"Days": ZIP_TMP_EXPIRE_DAYS},
                }
            ]
        },
    )
    print("  asset bucket cors + zip-tmp lifecycle: ok")


# --------------------------------------------------------------------- dynamodb


def asset_node_table_ensure(dynamodb, table_name):
    """virtual file tree + asset metadata, one row per tree node.
    gsi_asset_id is sparse: tree folders have no asset_id, so they don't
    appear in the index."""
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


# ------------------------------------------------------------------ lambda role


def api_role_ensure(iam, names, region, account_id):
    """execution role of the api lambda: cloudwatch logs, read of the user
    table (owned by _0_auth_cognito), item access on the asset-node table,
    object access on the asset bucket. returns the role arn."""
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
    return lambda_role_ensure(iam, names["lambda_role"], policy)


# --------------------------------------------------------------- lambda function


def lambda_env_build(names, config):
    service_config = config["asset_service"]
    return {
        "BUCKET_ASSET": names["bucket_asset"],
        "TABLE_ASSET_NODE": names["table_asset_node"],
        "TABLE_USER": names["table_user"],
        "GROUP_ACCESS": service_config["cognito"]["group_access"],
        "GROUP_ADMIN": service_config["cognito"]["group_admin"],
    }


def api_lambda_ensure(lambda_client, names, config, role_arn):
    return lambda_function_ensure(
        lambda_client,
        names["lambda_function"],
        config["asset_service"]["lambda"],
        role_arn,
        lambda_env_build(names, config),
        lambda_zip_build(DIR_BACKEND),
    )


# ------------------------------------------------------------------ cloudfront


def oac_ensure(cloudfront, oac_name):
    params = {}
    while True:
        page = cloudfront.list_origin_access_controls(**params)["OriginAccessControlList"]
        for item in page.get("Items", []):
            if item["Name"] == oac_name:
                print(f"origin access control already exists: {oac_name}")
                return item["Id"]
        if not page.get("NextMarker"):
            break
        params["Marker"] = page["NextMarker"]
    oac_id = cloudfront.create_origin_access_control(
        OriginAccessControlConfig={
            "Name": oac_name,
            "OriginAccessControlOriginType": "s3",
            "SigningBehavior": "always",
            "SigningProtocol": "sigv4",
        }
    )["OriginAccessControl"]["Id"]
    print(f"origin access control created: {oac_name}")
    return oac_id


def cloudfront_function_ensure(cloudfront, function_name):
    """ensure the url-rewrite function exists and its LIVE (published) code is
    current. returns the function arn (association requires a published stage)."""
    code = FUNCTION_URL_REWRITE_CODE.encode()
    function_config = {
        "Comment": "rewrite directory-style urls to their index.html",
        "Runtime": "cloudfront-js-2.0",
    }
    try:
        desc = cloudfront.describe_function(Name=function_name)
    except cloudfront.exceptions.NoSuchFunctionExists:
        created = cloudfront.create_function(
            Name=function_name, FunctionConfig=function_config, FunctionCode=code
        )
        cloudfront.publish_function(Name=function_name, IfMatch=created["ETag"])
        print(f"cloudfront function created + published: {function_name}")
        return created["FunctionSummary"]["FunctionMetadata"]["FunctionARN"]

    function_arn = desc["FunctionSummary"]["FunctionMetadata"]["FunctionARN"]
    try:
        code_live = cloudfront.get_function(Name=function_name, Stage="LIVE")[
            "FunctionCode"
        ].read()
    except cloudfront.exceptions.NoSuchFunctionExists:
        code_live = None
    if code_live == code:
        print(f"cloudfront function already exists: {function_name}, ok")
        return function_arn

    updated = cloudfront.update_function(
        Name=function_name,
        IfMatch=desc["ETag"],
        FunctionConfig=function_config,
        FunctionCode=code,
    )
    cloudfront.publish_function(Name=function_name, IfMatch=updated["ETag"])
    print(f"cloudfront function updated + published: {function_name}")
    return function_arn


def distribution_find(cloudfront, comment):
    params = {}
    while True:
        page = cloudfront.list_distributions(**params)["DistributionList"]
        for item in page.get("Items", []):
            if item["Comment"] == comment:
                return item
        if not page.get("NextMarker"):
            return None
        params["Marker"] = page["NextMarker"]


def distribution_config_build(
    names, config, region, api_id, oac_id, function_arn, caller_reference
):
    origin_web = {
        "Id": "web-s3",
        "DomainName": f"{names['bucket_web']}.s3.{region}.amazonaws.com",
        "OriginAccessControlId": oac_id,
        "S3OriginConfig": {"OriginAccessIdentity": ""},
        "OriginPath": "",
        "CustomHeaders": {"Quantity": 0},
    }
    origin_api = {
        "Id": "api-gw",
        "DomainName": f"{api_id}.execute-api.{region}.amazonaws.com",
        "OriginPath": "",
        "CustomHeaders": {"Quantity": 0},
        "CustomOriginConfig": {
            "HTTPPort": 80,
            "HTTPSPort": 443,
            "OriginProtocolPolicy": "https-only",
            "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
        },
    }
    behavior_default = {
        "TargetOriginId": "web-s3",
        "ViewerProtocolPolicy": "redirect-to-https",
        "AllowedMethods": {
            "Quantity": 2,
            "Items": ["GET", "HEAD"],
            "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
        },
        "CachePolicyId": CACHE_POLICY_CACHING_OPTIMIZED,
        "Compress": True,
        # url-rewrite function, e.g. /main/ -> /main/index.html
        "FunctionAssociations": {
            "Quantity": 1,
            "Items": [{"FunctionARN": function_arn, "EventType": "viewer-request"}],
        },
    }
    behavior_api = {
        "PathPattern": "/api/*",
        "TargetOriginId": "api-gw",
        "ViewerProtocolPolicy": "redirect-to-https",
        "AllowedMethods": {
            "Quantity": 7,
            "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
            "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
        },
        "CachePolicyId": CACHE_POLICY_CACHING_DISABLED,
        # forwards all viewer headers (incl. Authorization) except Host
        "OriginRequestPolicyId": ORIGIN_REQUEST_POLICY_ALL_VIEWER_EXCEPT_HOST,
        "Compress": True,
    }
    return {
        "CallerReference": caller_reference,
        "Comment": names["cloudfront_comment"],
        "Enabled": True,
        "DefaultRootObject": "index.html",
        "PriceClass": config["asset_service"]["cloudfront"]["price_class"],
        "Origins": {"Quantity": 2, "Items": [origin_web, origin_api]},
        "DefaultCacheBehavior": behavior_default,
        "CacheBehaviors": {"Quantity": 1, "Items": [behavior_api]},
    }


def distribution_diff_keys(config_existing, config_desired):
    """compare only the fields this script manages. cloudfront fills many
    default fields on read, so a full dict compare would always differ."""
    diff_keys = []
    for key in ["DefaultRootObject", "PriceClass", "Enabled", "Comment"]:
        if config_existing.get(key) != config_desired[key]:
            diff_keys.append(key)

    origins_existing = {
        origin["Id"]: origin["DomainName"] for origin in config_existing["Origins"]["Items"]
    }
    origins_desired = {
        origin["Id"]: origin["DomainName"] for origin in config_desired["Origins"]["Items"]
    }
    if origins_existing != origins_desired:
        diff_keys.append("Origins")

    behavior_existing = config_existing["DefaultCacheBehavior"]
    behavior_desired = config_desired["DefaultCacheBehavior"]
    if behavior_existing.get("CachePolicyId") != behavior_desired["CachePolicyId"]:
        diff_keys.append("DefaultCacheBehavior")

    assoc_existing = {
        (item["EventType"], item["FunctionARN"])
        for item in behavior_existing.get("FunctionAssociations", {}).get("Items", [])
    }
    assoc_desired = {
        (item["EventType"], item["FunctionARN"])
        for item in behavior_desired["FunctionAssociations"]["Items"]
    }
    if assoc_existing != assoc_desired:
        diff_keys.append("FunctionAssociations")

    behaviors_existing = {
        (behavior["PathPattern"], behavior["TargetOriginId"], behavior.get("CachePolicyId"))
        for behavior in config_existing.get("CacheBehaviors", {}).get("Items", [])
    }
    behaviors_desired = {
        (behavior["PathPattern"], behavior["TargetOriginId"], behavior["CachePolicyId"])
        for behavior in config_desired["CacheBehaviors"]["Items"]
    }
    if behaviors_existing != behaviors_desired:
        diff_keys.append("CacheBehaviors")
    return diff_keys


def distribution_config_merge(config_existing, config_desired):
    """merge the desired (managed) values into the fetched full config.

    update_distribution demands the COMPLETE config, including fields that
    cloudfront filled with defaults at creation (OriginReadTimeout,
    ConnectionTimeout, ...). so never replace a whole block with our minimal
    desired version; start from the fetched one and deep-merge desired on top.
    origins are matched by Id, cache behaviors by PathPattern."""
    merged = copy.deepcopy(config_existing)
    for key in ["Comment", "Enabled", "DefaultRootObject", "PriceClass"]:
        merged[key] = config_desired[key]

    def item_merge(item_existing, item_desired):
        if item_existing is None:
            return copy.deepcopy(item_desired)
        item_merged = copy.deepcopy(item_existing)
        dict_update_deep(item_merged, item_desired)
        return item_merged

    origins_existing_by_id = {
        origin["Id"]: origin for origin in config_existing["Origins"]["Items"]
    }
    origins_merged = [
        item_merge(origins_existing_by_id.get(origin["Id"]), origin)
        for origin in config_desired["Origins"]["Items"]
    ]
    merged["Origins"] = {"Quantity": len(origins_merged), "Items": origins_merged}

    merged["DefaultCacheBehavior"] = item_merge(
        config_existing["DefaultCacheBehavior"], config_desired["DefaultCacheBehavior"]
    )

    behaviors_existing_by_path = {
        behavior["PathPattern"]: behavior
        for behavior in config_existing.get("CacheBehaviors", {}).get("Items", [])
    }
    behaviors_merged = [
        item_merge(behaviors_existing_by_path.get(behavior["PathPattern"]), behavior)
        for behavior in config_desired["CacheBehaviors"]["Items"]
    ]
    merged["CacheBehaviors"] = {"Quantity": len(behaviors_merged), "Items": behaviors_merged}
    return merged


def distribution_ensure(cloudfront, names, config, region, api_id, oac_id, function_arn):
    existing = distribution_find(cloudfront, names["cloudfront_comment"])
    if existing is None:
        config_desired = distribution_config_build(
            names, config, region, api_id, oac_id, function_arn,
            caller_reference=str(uuid.uuid4()),
        )
        dist = cloudfront.create_distribution(DistributionConfig=config_desired)["Distribution"]
        print(f"cloudfront distribution created: {dist['Id']} ({dist['DomainName']})")
        print("  (deploying takes several minutes on aws side)")
        return dist["Id"], dist["DomainName"], dist["ARN"]

    dist_id = existing["Id"]
    print(f"cloudfront distribution already exists: {dist_id} ({existing['DomainName']})")
    fetched = cloudfront.get_distribution_config(Id=dist_id)
    config_existing = fetched["DistributionConfig"]
    config_desired = distribution_config_build(
        names, config, region, api_id, oac_id, function_arn,
        caller_reference=config_existing["CallerReference"],
    )
    diff_keys = distribution_diff_keys(config_existing, config_desired)
    if not diff_keys:
        print("  config ok")
    else:
        config_updated = distribution_config_merge(config_existing, config_desired)
        cloudfront.update_distribution(
            Id=dist_id, IfMatch=fetched["ETag"], DistributionConfig=config_updated
        )
        print(f"  updated: {', '.join(diff_keys)}")
    return dist_id, existing["DomainName"], existing["ARN"]


def web_bucket_policy_ensure(s3, bucket_name, distribution_arn):
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "CloudFrontRead",
                "Effect": "Allow",
                "Principal": {"Service": "cloudfront.amazonaws.com"},
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket_name}/*",
                "Condition": {"StringEquals": {"AWS:SourceArn": distribution_arn}},
            }
        ],
    }
    try:
        existing = json.loads(s3.get_bucket_policy(Bucket=bucket_name)["Policy"])
    except s3.exceptions.ClientError:
        existing = None
    if existing == policy:
        print("web bucket policy ok")
        return
    s3.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(policy))
    print("web bucket policy set: cloudfront-only access")


def distribution_delete(cloudfront, comment):
    distribution = distribution_find(cloudfront, comment)
    if distribution is None:
        print(f"cloudfront distribution does not exist: {comment}")
        return

    distribution_id = distribution["Id"]
    fetched = cloudfront.get_distribution_config(Id=distribution_id)
    distribution_config = fetched["DistributionConfig"]
    if distribution_config["Enabled"]:
        distribution_config["Enabled"] = False
        cloudfront.update_distribution(
            Id=distribution_id,
            IfMatch=fetched["ETag"],
            DistributionConfig=distribution_config,
        )
        print(f"cloudfront distribution disabling: {distribution_id}")

    cloudfront.get_waiter("distribution_deployed").wait(Id=distribution_id)
    fetched = cloudfront.get_distribution_config(Id=distribution_id)
    cloudfront.delete_distribution(Id=distribution_id, IfMatch=fetched["ETag"])
    print(f"cloudfront distribution deleted: {distribution_id}")


def cloudfront_function_delete(cloudfront, function_name):
    try:
        function = cloudfront.describe_function(
            Name=function_name, Stage="DEVELOPMENT"
        )
    except cloudfront.exceptions.NoSuchFunctionExists:
        print(f"cloudfront function does not exist: {function_name}")
        return
    cloudfront.delete_function(Name=function_name, IfMatch=function["ETag"])
    print(f"cloudfront function deleted: {function_name}")


def oac_delete(cloudfront, oac_name):
    oac_id = None
    params = {}
    while True:
        page = cloudfront.list_origin_access_controls(**params)[
            "OriginAccessControlList"
        ]
        for item in page.get("Items", []):
            if item["Name"] == oac_name:
                oac_id = item["Id"]
                break
        if oac_id is not None or not page.get("NextMarker"):
            break
        params["Marker"] = page["NextMarker"]

    if oac_id is None:
        print(f"origin access control does not exist: {oac_name}")
        return
    fetched = cloudfront.get_origin_access_control(Id=oac_id)
    cloudfront.delete_origin_access_control(Id=oac_id, IfMatch=fetched["ETag"])
    print(f"origin access control deleted: {oac_name}")


def architecture_delete(config, names):
    delete_confirm(
        f"All asset service AWS resources with prefix {config['name_prefix']!r} "
        "will be deleted."
    )

    s3 = aws_client_make(config, "s3")
    dynamodb = aws_client_make(config, "dynamodb")
    iam = aws_client_make(config, "iam")
    lambda_client = aws_client_make(config, "lambda")
    apigw = aws_client_make(config, "apigatewayv2")
    cloudfront = aws_client_make(config, "cloudfront")

    distribution_delete(cloudfront, names["cloudfront_comment"])
    http_api_delete(apigw, names["http_api"])
    lambda_delete(lambda_client, names["lambda_function"])
    cloudfront_function_delete(cloudfront, names["cloudfront_function"])
    oac_delete(cloudfront, names["cloudfront_oac"])
    lambda_role_delete(iam, names["lambda_role"])
    table_delete(dynamodb, names["table_asset_node"])
    bucket_delete(s3, names["bucket_web"])
    bucket_delete(s3, names["bucket_asset"])


# ----------------------------------------------------------------------- main


def architecture_ensure(config, names):
    region = config["aws"]["region_name"]

    cognito_gen = cognito_gen_load()
    pool_id = cognito_gen["cognito"]["user_pool_id"]
    app_client = config["asset_service"]["cognito"]["app_client"]
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

    s3 = aws_client_make(config, "s3")
    dynamodb = aws_client_make(config, "dynamodb")
    iam = aws_client_make(config, "iam")
    lambda_client = aws_client_make(config, "lambda")
    apigw = aws_client_make(config, "apigatewayv2")
    cloudfront = aws_client_make(config, "cloudfront")
    sts = aws_client_make(config, "sts")
    account_id = sts.get_caller_identity()["Account"]

    bucket_ensure(s3, names["bucket_asset"], region)
    bucket_asset_config_ensure(s3, names["bucket_asset"])
    bucket_ensure(s3, names["bucket_web"], region)

    asset_node_table_ensure(dynamodb, names["table_asset_node"])

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

    oac_id = oac_ensure(cloudfront, names["cloudfront_oac"])
    function_arn = cloudfront_function_ensure(cloudfront, names["cloudfront_function"])
    dist_id, dist_domain, dist_arn = distribution_ensure(
        cloudfront, names, config, region, api_id, oac_id, function_arn
    )
    web_bucket_policy_ensure(s3, names["bucket_web"], dist_arn)

    config_gen = config_gen_load()
    config_gen["asset_service"] = {
        "bucket_asset": names["bucket_asset"],
        "bucket_web": names["bucket_web"],
        "table_asset_node": names["table_asset_node"],
        "lambda_arn": lambda_arn,
        "api_id": api_id,
        "api_endpoint": api_endpoint,
        "cloudfront_distribution_id": dist_id,
        "cloudfront_domain": dist_domain,
        "cognito_client_id": client_id,
        "cognito_issuer": issuer,
    }
    config_gen_save(config_gen)
    print(f"saved to config_gen.yaml: cloudfront domain {dist_domain}")
    print(
        "next steps:\n"
        f"  1. add https://{dist_domain}/ to callback_urls and logout_urls of app\n"
        "     client 'web' in ../_0_auth_cognito/config.0.yaml, rerun ensure_cognito.py\n"
        "  2. run ensure_frontend.py to build and upload the frontend"
    )


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
