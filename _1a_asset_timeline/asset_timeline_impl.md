# asset timeline implementation plan

An extension of the asset service (`../_1_asset_service`): assets can be collected into **timelines**, list-like structures where every collected asset sits at one time point. Timeline data lives in dynamodb, the api runs in lambda behind api gateway, and login reuses the cognito user pool of `../_0_auth_cognito`. No frontend yet: clients call the http api endpoint directly with a cognito jwt.

```text
client (jwt in Authorization header)
  └─ https://<api-endpoint>/api/*   (api gateway http api, jwt authorizer)
                  │
                  ▼
               lambda ──────┬────────────────┬──────────────────┐
                            ▼                ▼                  ▼
                     timeline info      timeline asset      user table
                     table (info of     table (collect      (of _0_auth_cognito,
                     each timeline)     relationships)      sub -> user_id)
```

## Core Concepts

- **timeline**: a named container owned by one user, with a display `time_zone`. identified by a random `timeline_id`.
- **collect entry**: one row meaning "timeline X collects asset Y at time point T". an asset can be collected by many timelines, each time with its own time point (e.g. upload time in a scanner timeline, transaction time in a transactions timeline).
- **asset id**: a plain reference. this service does not validate it against the asset service, so a timeline can collect anything that has an id; resolving the id to actual asset content is the caller's business.

Time points and create times are stored as unix millisecond numbers, each with a companion timezone attribute in signed **minutes** (e.g. +09:00 -> 540), per `time-format.md`.

## AWS Resource Instances

All names start with `{prefix}` = `name_prefix` from `./config.yaml` (authentic value in `./config.0.yaml`), style `{user alias}-asset-timeline`.

| resource | name | purpose |
|---|---|---|
| dynamodb table | `{prefix}-info` | basic info of each timeline |
| dynamodb table | `{prefix}-asset` | collect entries (timeline x asset x time point) |
| iam role | `{prefix}-api-role` | lambda execution role |
| lambda | `{prefix}-api` | all api logic |
| api gateway (http api) | `{prefix}-api` | jwt authorizer + route `/api/*` -> lambda |
| dynamodb table | (own prefix of `_0_auth_cognito`)`-user` | user id mapping, owned by `_0_auth_cognito`; name read from its `config_gen.yaml` |

### timeline info table `{prefix}-info`

```text
PK  user_id
SK  timeline_id           random 0-9a-z id
attributes:
  name
  time_zone               display timezone of the timeline, signed minutes
  create_at               unix ms
  create_at_timezone      signed minutes
```

One partition per user, so listing/searching a user's timelines is a single query (name search filters in lambda; a user's timeline count is small).

### timeline asset table `{prefix}-asset`

```text
PK  timeline_id
SK  time_key              '{time_stamp zero-padded to 16 digits}#{asset_id}'
attributes:
  asset_id
  user_id                 owner (same as the timeline's owner)
  time_stamp              unix ms
  time_stamp_timezone     signed minutes

gsi_asset_id   PK asset_id, projection INCLUDE
               (user_id, time_stamp, time_stamp_timezone)
```

Why this sort key: plain string order of `time_key` equals time order, and the `#{asset_id}` suffix keeps the key unique when several assets share one time stamp. With `'#' < '0' < ... < 'z' < '~'` in ascii, every query becomes a plain sort key condition:

```text
range [t1, t2] inclusive:  time_key BETWEEN '{pad(t1)}' AND '{pad(t2)}~'
neighbors before t:        time_key <  '{pad(t)}'   descending, Limit n
neighbors after  t:        time_key >  '{pad(t)}~'  ascending,  Limit n
```

`gsi_asset_id` answers "which timelines collect this asset" and locates the entry of (timeline, asset) for remove/time-change. It is eventually consistent, which is acceptable: collecting and immediately changing/removing the same asset is not an expected pattern. Uniqueness of (timeline, asset) is enforced in lambda through this gsi, not by the primary key.

## Access Control

Same two roles as the asset service, from cognito groups in the jwt: no `asset-service` group -> rejected; with it -> guest (all GET apis); plus `admin` -> writes too. Group names are in local config. `user_id` is resolved from jwt `sub` via the user table of `_0_auth_cognito`.

## API

All responses use `{code, data, message}`; code 0 = success, code < 0 = failure. Timestamps in requests/responses are unix ms integers; timezone parameters are signed minutes.

| method + path | role | effect |
|---|---|---|
| GET `/api/me` | guest | user_id, username, email, role |
| GET `/api/timeline?name=` | guest | list own timelines, optional case-insensitive name search |
| POST `/api/timeline` | admin | create `{name, time_zone}` |
| GET `/api/timeline/{id}` | guest | one timeline |
| PATCH `/api/timeline/{id}` | admin | update `{name?, time_zone?}` |
| DELETE `/api/timeline/{id}` | admin | delete timeline + all its collect entries |
| GET `/api/timeline/{id}/asset?time_start=&time_end=&limit=` | guest | assets in inclusive time range, ascending |
| GET `/api/timeline/{id}/asset-neighbor?time_point=&count=&direction=` | guest | up to count assets before/after/both around the time point, nearest first; entries exactly at the time point are on neither side |
| POST `/api/timeline/{id}/asset` | admin | collect `{asset_id, time_stamp, time_stamp_timezone?}` (timezone defaults to the timeline's); fails if already collected |
| PATCH `/api/timeline/{id}/asset/{asset_id}` | admin | change time point `{time_stamp, time_stamp_timezone?}` |
| DELETE `/api/timeline/{id}/asset/{asset_id}` | admin | remove asset from timeline |
| GET `/api/asset/{asset_id}/timeline` | guest | own timelines collecting the asset, with the time point in each |

Since the time stamp is part of the sort key, a time point change is internally put-new-entry + delete-old-entry.

Deleting an asset in `_1_asset_service` also removes that asset from every timeline that collects it. that write is one dynamodb transaction covering the asset-node rows and the collect entries (`gsi_asset_id` locates them); if any collect-entry delete fails, the asset delete is rolled back too. this service's own `DELETE /api/timeline/{id}/asset/{asset_id}` only removes from one timeline and does not delete the asset.

## Scripts

```text
_1a_asset_timeline/
  config_gen.py         sub-project binding of shared config utilities +
                        resource names + cognito generated-config reader
  ensure_architect.py   ensure tables -> lambda role -> lambda -> http api;
                        --delete all (--assume-prefix) for removal
  test.py               see below
  backend/              lambda source, zipped by the ensure script
```

Generic ensure/delete logic (tables, role, lambda, http api, delete confirmation) lives in `/aws_utils/`; refer to `/doc/aws_oa_impl.md#shared-utilities-aws_utils`. This sub-project only defines its table schemas, role policy and lambda env.

Deploy order for a fresh account: `_0_auth_cognito` (ensure_cognito.py, ensure_user_table.py) -> `ensure_architect.py` here.

### test.py

```text
backend (default)
  -> create a TEMP stack from zero under prefix {prefix}-temp-{timestamp}
     (timeline tables/role/lambda, plus a temp asset-node table and
     asset lambda wired to those tables; suspended if same-name
     resources exist). the temp asset lambda reads the deployed asset
     bucket name from _1_asset_service/config_gen.yaml
  -> run the whole api flow through the temp timeline lambda, then the
     asset-delete flow: an asset collected by two timelines is removed
     from both in one transaction; a denied collect-entry delete rolls
     the asset-node delete back
  -> remove every temp resource (attempted even when a check failed),
     except the lambda execution roles: those keep STABLE names
     ({prefix}-temp-api-role, {prefix}-temp-asset-api-role,
     {prefix}-temp-asset-api-denied-role) and are reused across runs,
     because a freshly created role can stay un-assumable by lambda for
     many minutes; only their inline policies are rewritten each run
     (--clean does remove the roles). the denied role never allows
     DeleteItem on the timeline-asset table: the rollback check switches
     the asset lambda to it instead of rewriting the in-use role's
     policy, whose changes can take minutes to take effect

api
  -> the DEPLOYED http api rejects requests without a jwt
     (needs ensure_architect.py to have been run)

--clean [--assume-prefix xxx]
  -> locate residue of failed runs by the {prefix}-temp- name marker
     (lambda / role / tables) and remove it
```

The temp prefix uses `-temp-{timestamp}` with the timezone sign written as `p`/`m` (e.g. `20260830_04571488p09`), because aws resource names do not allow `+`.

## AWS Permission Settings (for the deploying iam user)

Inline policy for `ensure_architect.py` and `test.py`. Replace `<prefix>` (the `name_prefix` in `./config.0.yaml`) and `<account-id>` (the 12-digit aws account id). The `*-temp-*` arns cover the temp resources of `test.py`; account-level permissions shared by the test scripts of all sub-projects (cognito-idp:ListUsers etc.) are listed once in `/doc/aws_oa_impl.md#test-script-permissions`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DynamoDb",
      "Effect": "Allow",
      "Action": [
        "dynamodb:DescribeTable", "dynamodb:CreateTable",
        "dynamodb:UpdateTable", "dynamodb:DeleteTable"
      ],
      "Resource": [
        "arn:aws:dynamodb:*:<account-id>:table/<prefix>-info",
        "arn:aws:dynamodb:*:<account-id>:table/<prefix>-asset",
        "arn:aws:dynamodb:*:<account-id>:table/<prefix>-temp-*"
      ]
    },
    {
      "Sid": "LambdaAndRole",
      "Effect": "Allow",
      "Action": [
        "lambda:GetFunction", "lambda:CreateFunction", "lambda:DeleteFunction",
        "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
        "lambda:AddPermission", "lambda:InvokeFunction",
        "iam:GetRole", "iam:CreateRole", "iam:DeleteRole",
        "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:PassRole"
      ],
      "Resource": [
        "arn:aws:lambda:*:<account-id>:function:<prefix>-api",
        "arn:aws:lambda:*:<account-id>:function:<prefix>-temp-*",
        "arn:aws:iam::<account-id>:role/<prefix>-api-role",
        "arn:aws:iam::<account-id>:role/<prefix>-temp-*"
      ]
    },
    {
      "Sid": "ApiGateway",
      "Effect": "Allow",
      "Action": [
        "apigateway:GET", "apigateway:POST", "apigateway:PATCH",
        "apigateway:PUT", "apigateway:DELETE"
      ],
      "Resource": "arn:aws:apigateway:*::/*"
    }
  ]
}
```

notes:

- `iam:PassRole` lets the deployer attach the role to the lambda (both the deployed one and temp ones from test.py).
- api gateway v2 has no fine-grained action names; access is controlled by http verb on the `apigateway:*` arn space.
- permissions of `_0_auth_cognito` (cognito + user table) are documented in `../_0_auth_cognito/test_cognito_impl.md`.
