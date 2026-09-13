# lambda function + its execution role: ensure/delete, shared by all
# sub-projects. the role policy differs per sub-project and is passed in
# by the caller; the trust policy (lambda assumes the role) is fixed here.

import hashlib
import io
import json
import time
import zipfile
from base64 import b64encode
from pathlib import Path

TRUST_POLICY_LAMBDA = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


def lambda_zip_build(dir_backend, path_list_extra=None):
    """deployment zip of all *.py directly under dir_backend (boto3 comes
    with the lambda runtime, no packaging of dependencies). path_list_extra:
    additional files zipped in at the top level, e.g. a library file exposed
    by another sub-project."""
    path_list = sorted(Path(dir_backend).glob("*.py"))
    for path_extra in path_list_extra or []:
        path_list.append(Path(path_extra))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for path in path_list:
            zip_file.write(path, path.name)
    return buffer.getvalue()


def lambda_role_ensure(iam, role_name, policy):
    """execution role with the lambda trust policy and one inline policy
    named {role_name}-policy. returns the role arn."""
    try:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        print(f"role already exists: {role_name}")
    except iam.exceptions.NoSuchEntityException:
        role_arn = iam.create_role(
            RoleName=role_name, AssumeRolePolicyDocument=json.dumps(TRUST_POLICY_LAMBDA)
        )["Role"]["Arn"]
        print(f"role created: {role_name}")

    # put_role_policy overwrites, so it is idempotent by itself
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=f"{role_name}-policy",
        PolicyDocument=json.dumps(policy),
    )
    print("  role inline policy: ok")
    return role_arn


def lambda_function_ensure(lambda_client, function_name, lambda_config, role_arn, env, zip_bytes):
    """lambda_config: {runtime, memory_mb, timeout_sec} block from sub-project
    config. handler is fixed to lambda_function.lambda_handler."""
    try:
        existing = lambda_client.get_function(FunctionName=function_name)
    except lambda_client.exceptions.ResourceNotFoundException:
        # a freshly created role takes time to become assumable by lambda:
        # measured minutes, not seconds (/doc/aws_oa_impl.md#iam-propagation),
        # which is why test scripts reuse persistent roles instead of
        # recreating them. the retry window here covers the remaining gap
        attempt_max = 24  # 24 attempts * 5 sec
        for attempt in range(attempt_max):
            try:
                function_arn = lambda_client.create_function(
                    FunctionName=function_name,
                    Runtime=lambda_config["runtime"],
                    Role=role_arn,
                    Handler="lambda_function.lambda_handler",
                    Code={"ZipFile": zip_bytes},
                    Timeout=lambda_config["timeout_sec"],
                    MemorySize=lambda_config["memory_mb"],
                    Environment={"Variables": env},
                )["FunctionArn"]
                print(f"lambda created: {function_name}")
                return function_arn
            except lambda_client.exceptions.InvalidParameterValueException as error:
                if "assume" not in str(error).lower() or attempt == attempt_max - 1:
                    raise
                print("  waiting for role to become assumable...")
                time.sleep(5)

    function_conf = existing["Configuration"]
    print(f"lambda already exists: {function_name}")

    conf_desired = {
        "Runtime": lambda_config["runtime"],
        "Timeout": lambda_config["timeout_sec"],
        "MemorySize": lambda_config["memory_mb"],
        "Role": role_arn,
    }
    env_current = function_conf.get("Environment", {}).get("Variables", {})
    diff_keys = [key for key, value in conf_desired.items() if function_conf.get(key) != value]
    if env_current != env:
        diff_keys.append("Environment")
    if diff_keys:
        lambda_client.update_function_configuration(
            FunctionName=function_name, Environment={"Variables": env}, **conf_desired
        )
        lambda_client.get_waiter("function_updated_v2").wait(FunctionName=function_name)
        print(f"  configuration updated: {', '.join(diff_keys)}")
    else:
        print("  configuration ok")

    sha_local = b64encode(hashlib.sha256(zip_bytes).digest()).decode()
    if function_conf["CodeSha256"] == sha_local:
        print("  code ok")
    else:
        lambda_client.update_function_code(FunctionName=function_name, ZipFile=zip_bytes)
        lambda_client.get_waiter("function_updated_v2").wait(FunctionName=function_name)
        print("  code updated")
    return function_conf["FunctionArn"]


def lambda_invoke_permission_ensure(lambda_client, function_name, region, account_id, api_id):
    try:
        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId="apigateway-invoke",
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=f"arn:aws:execute-api:{region}:{account_id}:{api_id}/*",
        )
        print("lambda invoke permission added for api gateway")
    except lambda_client.exceptions.ResourceConflictException:
        print("lambda invoke permission already exists, ok")


def lambda_delete(lambda_client, function_name):
    try:
        lambda_client.delete_function(FunctionName=function_name)
        print(f"lambda deleted: {function_name}")
    except lambda_client.exceptions.ResourceNotFoundException:
        print(f"lambda does not exist: {function_name}")


def lambda_role_delete(iam, role_name):
    try:
        iam.get_role(RoleName=role_name)
    except iam.exceptions.NoSuchEntityException:
        print(f"lambda role does not exist: {role_name}")
        return
    try:
        iam.delete_role_policy(RoleName=role_name, PolicyName=f"{role_name}-policy")
    except iam.exceptions.NoSuchEntityException:
        pass
    iam.delete_role(RoleName=role_name)
    print(f"lambda role deleted: {role_name}")
