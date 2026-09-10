# aws_oa implementation

Project-level implementation document. Requirements and cross-cutting rules (naming, config layers, ensure/test design) are in `./aws_oa.md`; this file covers how those rules are implemented across sub-projects: the shared utility package, and the iam permissions common to running the test scripts.

Each sub-project documents itself with a pair of files in its own folder: `{name}_req[_min].md` (requirement) and `{name}_impl.md` (implementation design, including the iam policy its own scripts need).

## Shared Utilities (aws_utils)

Common logic used by the ensure/test scripts of different sub-projects lives in `/aws_utils/`. **Do not re-implement this logic inside a sub-project**: when writing or extending a sub-project, first check whether `/aws_utils/` already covers the operation; and when the same logic appears in a second sub-project, extract it into `/aws_utils/` instead of copying it.

Import from the package top level only (internal module layout may change):

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aws_utils import table_ensure, lambda_delete, delete_confirm
```

What is inside (by topic, one module each):

| topic | functions | note |
|---|---|---|
| layered config | `config_load(dir_sub)`, `config_gen_load(dir_sub)`, `config_gen_save(dir_sub, ...)`, `dict_update_deep`, `aws_client_make` | sub-projects wrap these in their own `config_gen.py`, binding `dir_sub` to their folder |
| dynamodb | `table_ensure` (create if missing, add missing gsi), `table_delete` | |
| lambda | `lambda_zip_build`, `lambda_role_ensure` (trust policy fixed, caller passes the permission policy), `lambda_function_ensure`, `lambda_invoke_permission_ensure`, `lambda_delete`, `lambda_role_delete` | |
| http api | `http_api_ensure/find/delete`, `jwt_authorizer_ensure`, `lambda_integration_ensure`, `api_route_ensure`, `api_stage_ensure` | api gateway http api with cognito jwt authorizer |
| s3 | `bucket_ensure` (private, versioning off), `bucket_objects_delete`, `bucket_delete` | |
| script helpers | `delete_confirm` (typed confirm-YYYYMMDD), `timestamp_make`, `timestamp_resource_make` (`p`/`m` timezone sign for aws resource names), `step`, `check`, `check_count`, `TestFail` | |

What stays inside a sub-project: everything carrying its own semantics — table schemas, role permission policies, lambda env, resource names — written as small wrappers calling the shared functions (see `_1a_asset_timeline/ensure_architect.py` as the reference shape).

One deliberate exception: `_2_local_es/config.py` does not delegate to `/aws_utils/`, because `deploy_to_raspi.sh` copies that single file to the home server where the package does not exist. Leave it self-contained.

## IAM Propagation

The rule in `./aws_oa.md#test-design` (stable reused execution roles; deny by role switch) comes from debugging `_1a_asset_timeline/test.py` on 2026-09-04 (us-east-1). Measured behavior:

- A freshly created role, with a trust policy byte-identical to a working role's, was rejected by `lambda:CreateFunction` ("The role defined for the function cannot be assumed by Lambda") for 274 seconds in one probe and for more than 6 minutes in another, while creating a function with a role made days earlier succeeded instantly. The commonly documented "retry for a few seconds" does not hold.
- Rewriting the inline policy of an IN-USE role to REVOKE an action (`dynamodb:DeleteItem`) was still not effective after 2.5 minutes of polling. Newly GRANTED actions in the same kind of rewrite (each test run points the policies at freshly created tables) are effective within seconds. Propagation is asymmetric: allow is fast, revoke is slow.
- Switching a lambda's execution role (`update_function_configuration` + the `function_updated_v2` waiter) takes effect immediately: configuration changes create new execution environments, which fetch the new role's credentials, so no stale authorization lingers.

How to tell "wrong trust policy" apart from "slow propagation" when `cannot be assumed` appears (a wrong trust policy never heals, so retrying would be pointless):

1. Compare the stored trust policy (`iam.get_role`) with that of a role known to work.
2. Create a throwaway function with the OLD working role: success means account, permissions and trust policy are all fine.
3. Create a throwaway role and retry `create_function` in a loop, logging elapsed time until it succeeds; this measures the actual propagation delay.

Where this is implemented: `lambda_function_ensure` in `/aws_utils/lambda_util.py` retries the assume error for up to 2 minutes (enough when the role already exists, not necessarily for its first-ever run); `_1a_asset_timeline/test.py` keeps three persistent roles: the timeline and asset execution roles plus a denied role for the rollback check (see `../_1a_asset_timeline/asset_timeline_impl.md`).

## Test Script Permissions

Per `./aws_oa.md#test-design`, test scripts create temporary resource instances named `{prefix}-temp-{timestamp}` (aws resource names) or `{prefix}_temp_{timestamp}` (data-level names such as es indices), operate on them, and remove them; `--clean` locates residue of failed runs by listing resources and matching names.

Beyond each sub-project's own deploy policy (documented in its `{name}_impl.md`), the deploying iam user therefore needs the following account-level permissions, shared by the test scripts of the sub-projects. Replace `<account-id>` with the 12-digit aws account id.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TestUserDiscovery",
      "Effect": "Allow",
      "Action": [
        "cognito-idp:ListUsers",
        "cognito-idp:AdminListGroupsForUser"
      ],
      "Resource": "*"
    },
    {
      "Sid": "TestResidueDiscovery",
      "Effect": "Allow",
      "Action": [
        "lambda:ListFunctions",
        "iam:ListRoles",
        "dynamodb:ListTables"
      ],
      "Resource": "*"
    },
    {
      "Sid": "TestTempResources",
      "Effect": "Allow",
      "Action": [
        "dynamodb:DescribeTable", "dynamodb:CreateTable",
        "dynamodb:UpdateTable", "dynamodb:DeleteTable",
        "lambda:GetFunction", "lambda:CreateFunction", "lambda:DeleteFunction",
        "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
        "lambda:InvokeFunction",
        "iam:GetRole", "iam:CreateRole", "iam:DeleteRole",
        "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:PassRole"
      ],
      "Resource": [
        "arn:aws:dynamodb:*:<account-id>:table/*-temp-*",
        "arn:aws:lambda:*:<account-id>:function:*-temp-*",
        "arn:aws:iam::<account-id>:role/*-temp-*"
      ]
    }
  ]
}
```

Why each block exists:

- **TestUserDiscovery**: tests fabricate authorizer claims of a real cognito user; they find an admin user by listing pool users and their groups.
- **TestResidueDiscovery**: `--clean` finds leftover temp resources by name matching, which requires the account-level list operations.
- **TestTempResources**: create/operate/delete of the temp stack itself. The `*-temp-*` arns keep this away from the deployed (non-temp) resources; permissions on deployed resources belong to the sub-project's own deploy policy.

Sub-project specifics on top of this: `_0_auth_cognito/test.py` creates a temporary user pool (cognito permissions in `../_0_auth_cognito/test_cognito_impl.md`); `_1_asset_service/test.py` invokes the DEPLOYED lambda, so it needs `lambda:InvokeFunction` on that function (in `../_1_asset_service/asset_service_impl.md`); `_2_local_es/test.py` needs the raspi worker running and sqs access (in `../_2_local_es/local_es_impl.md`).
