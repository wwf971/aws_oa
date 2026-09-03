# asset service implementation plan

A simple web drive on aws. Users see a virtual file tree; each item in the tree is either a folder of the tree, or an asset (a single file, or a whole uploaded folder). Asset bytes live in s3, tree structure and asset metadata live in dynamodb, api runs in lambda behind api gateway, frontend is static pages on s3 served by cloudfront, and login is done by the cognito user pool already set up in `../_0_auth_cognito`.

```text
browser
  ├─ https://<cloudfront-domain>/           login page   (s3, small bundle)
  ├─ https://<cloudfront-domain>/main/      main page    (s3, loaded after login)
  └─ https://<cloudfront-domain>/api/*      api          (api gateway -> lambda)
                                              │
                        ┌─────────────────────┼──────────────────┐
                        ▼                     ▼                  ▼
                 dynamodb                  s3 asset bucket    cognito (jwt verify
             user table / asset-node      (asset bytes,       is done by api
                  table                    presigned up/down)  gateway authorizer)
```

## Core Concepts

- **asset**: uploaded content. two types: `file` (one file) and `folder` (a folder with nested files). identified by a random `asset_id`; bytes live in the s3 asset bucket under prefix `{asset_id}/`.
- **tree node**: one row in the virtual file tree that users see. a node is either a *tree folder* (no asset attached) or an *asset node* (points to one asset via `asset_id`). each user has their own tree.
- **user id**: our own user identity, independent from cognito. the user table in `../_0_auth_cognito` maps cognito `sub` -> `user_id` (see `../_0_auth_cognito/test_cognito_impl.md#user-table-dynamodb`). all asset service data is keyed by `user_id`, never by `sub`.

note: "tree folder" and "folder asset" are different things. a tree folder organizes nodes in the virtual tree. a folder asset is one asset whose content happens to be a directory of files; in the tree it is a single (leaf) node.

## Access Control

Two roles, derived from cognito groups found in the jwt:

```text
groups in jwt                         role
─────────────────────────────────────────────────────
no "asset-service"          ->  rejected (403)
"asset-service" only        ->  guest   (read: fetch tree, download)
"asset-service" + "admin"   ->  admin   (everything: upload, create folder,
                                         rename, move, delete, download)
```

The `asset-service` group is configured in `../_0_auth_cognito/config.0.yaml`. api gateway jwt authorizer only verifies token signature/issuer/audience; the role check above is done inside lambda.

## AWS Resource Instances

All names start with the unified prefix `{prefix}` = `name_prefix` from `./config.yaml` (authentic value in `./config.0.yaml`). each service has its own prefix; resources owned by another service (like the user table) are found via that service's `config_gen.yaml`, never by rebuilding the name from a prefix.

| resource | name | purpose |
|---|---|---|
| s3 bucket | `{prefix}-asset` | asset bytes (objects at STANDARD_IA, no versioning) |
| s3 bucket | `{prefix}-web` | frontend static files (STANDARD, private, cloudfront-only) |
| dynamodb table | (own prefix of `_0_auth_cognito`)`-user` | user id mapping, owned by `_0_auth_cognito`; name read from its `config_gen.yaml` |
| dynamodb table | `{prefix}-asset-node` | virtual file tree + asset metadata |
| iam role | `{prefix}-asset-api-role` | lambda execution role |
| lambda | `{prefix}-asset-api` | all api logic |
| api gateway (http api) | `{prefix}-asset-api` | jwt authorizer + route `/api/*` -> lambda |
| cloudfront distribution | comment `{prefix}-asset-service` | serves web bucket + `/api/*` to api gateway |
| cloudfront oac | `{prefix}-web-oac` | lets cloudfront read the private web bucket |
| cloudfront function | `{prefix}-url-rewrite` | rewrites `/main/` -> `/main/index.html` (see below) |

Cognito resources (user pool, app client `web`, groups, domain) are reused from `_0_auth_cognito`, whose generated ids are read from `../_0_auth_cognito/config_gen.yaml`.

### s3 asset bucket `{prefix}-asset`

```text
{asset_id}/{file_name}                     file asset
{asset_id}/xx/yy/{file_name}               folder asset content
{asset_id}/xx/yy/__@@FOLDER@@__            empty-folder marker inside folder asset
zip-tmp/{asset_id}-{timestamp}.zip         temporary zip for folder-asset download
                                           (lifecycle rule expires zip-tmp/ after 1 day)
```

- objects uploaded at STANDARD_IA storage class (enforced by the presigned url).
- cors enabled (GET/PUT/HEAD from any origin): browser uploads/downloads directly via presigned urls.
- versioning disabled.

### s3 web bucket `{prefix}-web`

```text
index.html               login page (cloudfront default root object)
login-assets/*           login page js/css
main/index.html          main page
main/assets/*            main page js/css
web-config.json          runtime config for both pages (generated by ensure_frontend.py)
```

login page and main page are two independent vite build targets, so the first visit only downloads the small login bundle; the heavier main bundle is fetched only after the browser navigates to `/main/` (which happens after login succeeds). note: in this version this is a bundle-splitting measure, not a hard protection; hard protection of `/main/*` at cdn level would need cloudfront signed cookies, a possible later extension.

### dynamodb table `{prefix}-asset-node`

One row per tree node. Fetching the whole tree of a user is a single query on the partition key.

```text
table {prefix}-asset-node
  PK  user_id
  SK  node_id      random 0-9a-z id
  attributes:
    name           display name in the tree
    parent_id      node_id of parent tree folder; ABSENT for nodes at root level
    lexorank       order among siblings; string of chars 0-9a-z; sorting
                   siblings by plain string compare gives the display order
    asset_id       ABSENT for tree folder; random id for asset node
    asset_type     "file" / "folder"  (asset nodes only)
    file_name      original file name (file asset only; also the s3 key part)
    size           total bytes (asset nodes; filled when upload completes)
    content_type   mime type (file asset only)
    upload_state   "pending" / "ready"  (asset nodes only)
    created_at     iso datetime

gsi_asset_id   PK asset_id, projection KEYS_ONLY
               (s3 object key starts with asset_id, so this answers
                "which user/node owns this s3 object", for integrity
                check / orphan cleanup)
```

- root level: rows without `parent_id`. there is no explicit root row.
- renaming a node only changes `name`; s3 keys are never renamed (they use `file_name` / relative paths captured at upload time).
- lexorank of a new node = midpoint between last sibling's rank and "end"; on move, frontend computes the midpoint rank between the two neighbor ranks at the drop position and sends it. midpoint algorithm never produces a rank ending in `0`, so a gap always exists.

### lambda `{prefix}-asset-api`

- runtime python 3.12, single source folder `backend/` zipped by `ensure_architect.py` (boto3 comes with the runtime, no packaging of deps).
- env vars: asset bucket name, asset-node table name, user table name.
- role `{prefix}-asset-api-role` allows: cloudwatch logs; Query on user table gsi; Query/Get/Put/Update/BatchWrite on asset-node table; Get/Put/Delete/List on the asset bucket.

### api gateway http api `{prefix}-asset-api`

- jwt authorizer: issuer `https://cognito-idp.{region}.amazonaws.com/{user_pool_id}`, audience = app client id `web`. requests without a valid cognito jwt never reach lambda.
- one route `ANY /api/{proxy+}` -> lambda (lambda does its own path routing).
- cors allows any origin (lets `vite dev` call the deployed api directly during development).

### cloudfront distribution

```text
behavior /*        -> s3 web bucket origin (oac), managed policy CachingOptimized,
                      viewer-request function {prefix}-url-rewrite
behavior /api/*    -> api gateway origin, CachingDisabled +
                      AllViewerExceptHostHeader (forwards Authorization)
default root object: index.html
```

web bucket policy only allows this distribution (service principal + source arn condition), so the bucket is not directly reachable.

why the url-rewrite function: `DefaultRootObject` only covers the root `/`. for `/main/` cloudfront asks the s3 origin for the literal object key `main/`, which does not exist, and s3 answers **AccessDenied** (s3 says AccessDenied instead of NotFound for missing keys when the caller has no list permission). the function runs on viewer-request of the s3 behavior only and rewrites directory-style urls to their `index.html` object:

```text
/            -> /index.html   (also covered by DefaultRootObject)
/main/       -> /main/index.html
/main        -> /main/index.html   (no '.' in last segment)
/api/*       untouched (different behavior, function not attached)
```

## API

All responses use `{code, data, message}`; code 0 = success, code < 0 = failure. lambda resolves `user_id` from jwt `sub` via the user table (rejects with a clear message if the mapping row is missing, i.e. `ensure_user_table.py` was not run).

| method + path | role | effect |
|---|---|---|
| GET `/api/me` | guest | user_id, username, email, role |
| GET `/api/tree` | guest | all tree nodes of the user |
| POST `/api/folder` | admin | create tree folder `{name, parent_id?}` |
| POST `/api/asset` | admin | begin asset upload, returns presigned put urls |
| POST `/api/asset-complete` | admin | `{node_id}` finish upload, verify objects, set ready |
| PATCH `/api/node/{node_id}` | admin | rename `{name}` or move `{parent_id?, lexorank}` |
| DELETE `/api/node/{node_id}` | admin | delete subtree, incl. s3 objects of contained assets |
| GET `/api/download/{node_id}` | guest | presigned download url |

upload flow (browser does the byte transfer, lambda only signs):

```text
frontend: POST /api/asset {name, parent_id, asset_type,
                           files: [{path, size, content_type}, ...]}
  -> lambda: create asset_id + node row (upload_state=pending)
             presign PUT url per file (key {asset_id}/{path},
             storage class STANDARD_IA baked into signature)
  -> frontend: PUT each file to s3 directly
  -> frontend: POST /api/asset-complete {node_id}
  -> lambda: list s3 objects under {asset_id}/, sum size,
             set upload_state=ready
```

file asset = the same flow with a single file whose `path` is the file name. an entry with `is_folder: true` means an empty folder inside a folder asset; its presigned key ends with `__@@FOLDER@@__`.

download flow:

```text
GET /api/download/{node_id}
  -> file asset:   presigned GET of the single object (content-disposition attachment)
  -> folder asset: lambda downloads objects to /tmp, zips, uploads zip to
                   zip-tmp/, returns presigned GET of the zip
                   (limited by lambda /tmp 512MB; fine for a simple web drive)
```

delete flow: collect the subtree of the node (from the user's node rows), delete s3 objects of every contained asset (`{asset_id}/` prefixes), then batch-delete the node rows.

## Login Flow (oauth2 authorization code + pkce)

```text
login page /
  -> "sign in" click
  -> generate pkce verifier, save in sessionStorage
  -> redirect to https://{domain-prefix}.auth.{region}.amazoncognito.com
       /oauth2/authorize?client_id=...&response_type=code
       &redirect_uri=https://{cloudfront-domain}/&code_challenge=...
  -> cognito hosted login (also handles first-login password change)
  -> redirect back to / with ?code=...
  -> login page exchanges code at /oauth2/token (with pkce verifier)
  -> tokens saved to localStorage -> navigate to /main/
```

- app client `web` has no secret, so pkce is the protection of the code exchange.
- the cloudfront url must be registered as callback url of app client `web`: after `ensure_architect.py` prints the cloudfront domain, add `https://{cloudfront-domain}/` to `callback_urls` (and `logout_urls`) in `../_0_auth_cognito/config.0.yaml` and rerun `ensure_cognito.py`.
- main page sends `Authorization: Bearer {access_token}` to `/api/*`; refreshes with the refresh token when expired; redirects back to `/` when no valid token.

## Frontend

Under `frontend/` (pnpm workspace package `@wwf971/asset-service-frontend`). react + mobx, components from `@wwf971/react-comp-misc`. Two vite configs = two build targets:

```text
frontend/
  vite.login.config.js    root src/login, base /,      out dist/login  (assets in login-assets/)
  vite.main.config.js     root src/main,  base /main/, out dist/main
  src/
    shared/     web-config fetch, token storage, pkce + token endpoints, lexorank
    login/      LoginApp: plain react, no mobx (keeps the first bundle small)
    main/       MainApp + Header + TreePanel + NodePanel
    main/store/ authStore (tokens, claims, role, refresh, logout)
                assetStore (nodes by id, selection, expand state, rename/upload/
                            delete operation state, api calls; source of truth
                            for everything the components render)
```

main page layout (same shape as `file-access-smb`):

```text
┌──────────────────────────────────────────────┐
│ header: title · user email · role · logout   │
├───────────────┬──────────────────────────────┤
│ tree panel    │ node panel                   │
│  toolbar row  │  selected node details       │
│  TreeView     │  (name, type, size, state)   │
│  (PanelDual)  │  download button             │
├───────────────┴──────────────────────────────┤
│ message bar (api errors, upload progress)    │
└──────────────────────────────────────────────┘
```

- tree: `TreeView` with folders + asset leaves; drag-move enabled for admin (drop -> lexorank midpoint -> PATCH move); right-click context menu (`MenuComp`): download / rename / new folder / delete.
- rename: in-place via contenteditable tree item (same pattern as file-access-smb).
- delete: custom confirm popup (no browser confirm()).
- upload: toolbar buttons trigger hidden file inputs (single file / folder via webkitdirectory), store runs the presigned upload flow.
- guest sees the same ui with mutating controls disabled.

## Ensure Scripts (IaC entry points)

```text
_1_asset_service/
  config_gen.py         shared module: layered config load, config_gen.yaml
                        load/save, boto3 clients, resource name building
  ensure_architect.py   ensure buckets -> asset-node table -> lambda role
                        -> lambda -> http api (authorizer/route) -> cloudfront
                        -> web bucket policy; save ids to config_gen.yaml
  ensure_frontend.py    ensure web bucket exists -> build frontend (pnpm build)
                        -> generate web-config.json
                        -> upsert changed files to web bucket (md5 vs etag)
                        -> delete stale keys -> cloudfront invalidation
                        (invalidation is skipped until ensure_architect.py
                         has created the distribution)
```

'ensure' = create if missing, update if config differs, never blind-recreate. generated ids (bucket/table/lambda/api/distribution, cloudfront domain) go to `config_gen.yaml` (git-ignored).

deploy order for a fresh account:

```text
1. _0_auth_cognito: ensure_cognito.py -> ensure_user_table.py
2. _1_asset_service: ensure_architect.py
3. add https://{cloudfront-domain}/ to callback_urls/logout_urls in
   _0_auth_cognito/config.0.yaml, rerun ensure_cognito.py
4. _1_asset_service: ensure_frontend.py
```

## AWS Permission Settings (for the deploying iam user)

Inline policy for running `ensure_architect.py` / `ensure_frontend.py` (iam console -> users -> select the user whose access key is in `/2026/aws_oa/config/config.0.yaml` -> add permissions -> create inline policy -> json tab). before pasting, replace the two placeholders:

### how to fill `<prefix>`

`<prefix>` is simply the `name_prefix` value in `./config.0.yaml` of this service. for example, with

```yaml
name_prefix: asset-service-xxx
```

the line `"arn:aws:s3:::<prefix>-asset"` becomes `"arn:aws:s3:::asset-service-xxx-asset"`.

### how to fill `<account-id>`

`<account-id>` is the 12-digit number identifying the whole aws account (it is not the iam user name and not the access key id). any of these gives it:

1. aws console: click the account name at the top-right, the dropdown shows "Account ID" with a copy button.
2. iam console: open any user or role, its arn contains the id: `arn:aws:iam::123456789012:user/somebody` -> `123456789012`.
3. from this folder, using the access key already in config (GetCallerIdentity needs no permission, it always works):

```bash
python3 -c "
import boto3
from config_gen import config_load
aws = config_load()['aws']
sts = boto3.client('sts', region_name=aws['region_name'],
    aws_access_key_id=aws['access_key_id'],
    aws_secret_access_key=aws['secret_access_key'])
print(sts.get_caller_identity()['Account'])
"
```

so with account id `123456789012` and prefix `asset-service-wwf971`, the lambda line becomes:

```text
arn:aws:lambda:*:123456789012:function:asset-service-wwf971-asset-api
```

### the policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3Buckets",
      "Effect": "Allow",
      "Action": ["s3:*"],
      "Resource": [
        "arn:aws:s3:::<prefix>-asset", "arn:aws:s3:::<prefix>-asset/*",
        "arn:aws:s3:::<prefix>-web", "arn:aws:s3:::<prefix>-web/*"
      ]
    },
    {
      "Sid": "DynamoDb",
      "Effect": "Allow",
      "Action": [
        "dynamodb:DescribeTable", "dynamodb:CreateTable", "dynamodb:UpdateTable"
      ],
      "Resource": "arn:aws:dynamodb:*:<account-id>:table/<prefix>-asset-node"
    },
    {
      "Sid": "LambdaAndRole",
      "Effect": "Allow",
      "Action": [
        "lambda:GetFunction", "lambda:CreateFunction", "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration", "lambda:AddPermission",
        "lambda:GetPolicy",
        "iam:GetRole", "iam:CreateRole", "iam:PutRolePolicy", "iam:PassRole"
      ],
      "Resource": [
        "arn:aws:lambda:*:<account-id>:function:<prefix>-asset-api",
        "arn:aws:iam::<account-id>:role/<prefix>-asset-api-role"
      ]
    },
    {
      "Sid": "ApiGateway",
      "Effect": "Allow",
      "Action": ["apigateway:GET", "apigateway:POST", "apigateway:PATCH", "apigateway:PUT"],
      "Resource": "arn:aws:apigateway:*::/*"
    },
    {
      "Sid": "CloudFront",
      "Effect": "Allow",
      "Action": [
        "cloudfront:ListDistributions", "cloudfront:GetDistribution",
        "cloudfront:GetDistributionConfig", "cloudfront:CreateDistribution",
        "cloudfront:UpdateDistribution", "cloudfront:CreateInvalidation",
        "cloudfront:ListOriginAccessControls", "cloudfront:CreateOriginAccessControl",
        "cloudfront:DescribeFunction", "cloudfront:GetFunction",
        "cloudfront:CreateFunction", "cloudfront:UpdateFunction",
        "cloudfront:PublishFunction"
      ],
      "Resource": "*"
    }
  ]
}
```

notes:

- api gateway v2 has no fine-grained action names; access is controlled by http verb on the `apigateway:*` arn space.
- cloudfront list/create actions are account-level, so `Resource: "*"`.
- `iam:PassRole` is what lets the deployer attach `{prefix}-asset-api-role` to the lambda.
- permissions of `_0_auth_cognito` (cognito + user table) are documented in `../_0_auth_cognito/test_cognito_impl.md`.
