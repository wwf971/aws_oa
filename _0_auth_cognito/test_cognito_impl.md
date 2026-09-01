## AWS Permission Settings

there is no aws-managed policy named like "AmazonCognitoFullAccess". the closest managed policy is `AmazonCognitoPowerUser`, which grants nearly all `cognito-idp:*` actions and would be enough for `ensure_cognito.py`. for a scoped setup, attach the inline policy below to the iam user instead (iam console -> users -> select user -> add permissions -> create inline policy -> json tab).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CognitoAccountLevel",
      "Effect": "Allow",
      "Action": [
        "cognito-idp:ListUserPools",
        "cognito-idp:CreateUserPool"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CognitoUserPoolLevel",
      "Effect": "Allow",
      "Action": [
        "cognito-idp:DescribeUserPool",
        "cognito-idp:UpdateUserPool",
        "cognito-idp:AddCustomAttributes",
        "cognito-idp:CreateUserPoolDomain",
        "cognito-idp:DeleteUserPoolDomain",
        "cognito-idp:DescribeResourceServer",
        "cognito-idp:CreateResourceServer",
        "cognito-idp:UpdateResourceServer",
        "cognito-idp:ListUserPoolClients",
        "cognito-idp:DescribeUserPoolClient",
        "cognito-idp:CreateUserPoolClient",
        "cognito-idp:UpdateUserPoolClient",
        "cognito-idp:ListGroups",
        "cognito-idp:CreateGroup",
        "cognito-idp:AdminGetUser",
        "cognito-idp:AdminCreateUser",
        "cognito-idp:AdminUpdateUserAttributes",
        "cognito-idp:AdminListGroupsForUser",
        "cognito-idp:AdminAddUserToGroup",
        "cognito-idp:AdminRemoveUserFromGroup"
      ],
      "Resource": "arn:aws:cognito-idp:*:<account-id>:userpool/*"
    }
  ]
}
```

notes:

- replace `<account-id>` with the 12-digit aws account id.
- `ListUserPools` and `CreateUserPool` are account-level actions, they only accept `Resource: "*"`.
- all `AdminXxx` actions (AdminCreateUser, AdminAddUserToGroup, ...) operate on a user pool arn, so they belong to the pool-level statement. after the pool is created, the pool-level resource can be narrowed from `userpool/*` to `arn:aws:cognito-idp:<region>:<account-id>:userpool/<pool-id>`.
- after saving a policy change, iam can take a few minutes to propagate. if a freshly added action still returns AccessDeniedException, wait a bit and retry before assuming the policy is wrong (this happened with `CreateUserPoolDomain` during development).

## Domain (aws-managed)

the aws-managed domain hosts the login/logout/token pages at `https://<prefix>.auth.<region>.amazoncognito.com`. configured in config under `cognito.user_pool.domain` with `type: aws-managed` and `prefix`.

prefix rules:

- **the prefix is globally unique per region, shared across ALL aws accounts.** a short or common prefix (like `test-cognito`, `demo`, `myapp`) is very likely already taken by someone else, and creation fails with `InvalidParameterException: Domain already associated with another user pool`. that error message is confusing: "another user pool" means a pool in someone else's account, not yours. use a prefix with a unique personal/project suffix, e.g. `test-auth-wwf971`.
- only lowercase letters, numbers, hyphens. no underscore (`test_cognito` is invalid), no leading/trailing hyphen.
- the words `aws`, `amazon`, `cognito` are reserved and can be rejected in some validation paths, avoid them.
- the prefix of an existing domain cannot be renamed in place: `ensure_cognito.py` deletes the old domain and creates a new one when the configured prefix differs.
- right after creation the domain may take up to a minute to become resolvable.

## Complete Registration (temporary password -> real password)

`ensure_cognito.py` ensures the aws-managed domain and prints the hosted login url for each app client (also saved to `config_gen.yaml` under `cognito.login_urls`). the url shape:

```text
https://<domain-prefix>.auth.<region>.amazoncognito.com/login
    ?client_id=<app-client-id>
    &response_type=code
    &redirect_uri=<url-encoded first callback url of the app client>
```

steps:

1. open the login url in a browser.
2. sign in with the username and the temporary password from the invitation email.
3. cognito detects the FORCE_CHANGE_PASSWORD state and shows a "change password" page, enter the real password (must satisfy the pool password policy).
4. after the password is set, cognito redirects to the callback url with `?code=...` appended. while the callback url is still the placeholder `https://app.example.com/callback`, the browser shows a cannot-reach-page error, which is fine: the password change already completed before the redirect.

notes:

- the `redirect_uri` parameter must exactly match one of the callback urls configured on the app client, otherwise cognito shows a redirect_mismatch error.


## User Table (dynamodb)

cognito identifies a user by `sub`, but our system should not couple its own user identity to one auth service. so a dynamodb table is the source of truth of user identity: each user has a randomly assigned `user_id`, and rows of the table map `user_id` to identities in auth services (cognito, or other auth services added later). `ensure_user_table.py` ensures the table and the mapping rows for users listed in config.

one user = one `user_id` = possibly many rows (one row per identity in an auth service).

### schema

table name: `{name_prefix}-user` (`name_prefix` comes from `./config.yaml`, the prefix of this sub-project; each service has its own prefix). the actual table name is saved to `config_gen.yaml`, which is how other services (e.g. the asset service) find this table.

```text
table {name_prefix}-user
  PK  user_id   random 0-9a-z string, assigned by our system
  SK  auth_id   "{auth_system_type}#{user_id_from_auth}"
                e.g. "cognito#<sub>"
                special row for script-created user: just "script"
                (dynamodb key cannot hold null, so the "no id from auth
                 service" case is encoded by omitting the "#..." part)
  attributes:
    auth_system_type    "cognito" / "script" / ...
    user_id_from_auth   e.g. cognito sub (absent on the "script" row)
    username, email     for human readability when browsing the table
    created_at          iso datetime

gsi_auth_id   PK auth_id, projection KEYS_ONLY
```

lookups in both directions:

- `user_id` -> all identities: query main table with PK = user_id.
- identity -> `user_id`: query `gsi_auth_id` with auth_id = `cognito#<sub>`. KEYS_ONLY projection already returns `user_id` (table keys are always projected into a gsi).

a user created by this script via AdminCreateUser gets two rows: the special `script` row (records that this user originates from the script, with no id from any auth service), and the `cognito#<sub>` row.

### ensure flow

```text
ensure_user_table.py
  -> load config, read user pool id from config_gen.yaml
       (run ensure_cognito.py first if missing)
  -> ensure table exists (create with gsi_auth_id if missing)
  -> for each user in config:
       -> admin_get_user -> sub
       -> query gsi_auth_id for "cognito#<sub>"
       -> if found: user_id already assigned, ok
          else: generate user_id, put "script" row + "cognito#<sub>" row
  -> save {username: user_id} to config_gen.yaml under user_table
```

### iam permissions

attach as inline policy (same way as the cognito policy above). `<name-prefix>` is the `name_prefix` value in `./config.0.yaml`; `<account-id>` is the 12-digit aws account id (how to find it: see `../_1_asset_service/asset_service_impl.md#how-to-fill-account-id`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "UserTable",
      "Effect": "Allow",
      "Action": [
        "dynamodb:DescribeTable",
        "dynamodb:CreateTable",
        "dynamodb:UpdateTable",
        "dynamodb:Query",
        "dynamodb:PutItem"
      ],
      "Resource": [
        "arn:aws:dynamodb:*:<account-id>:table/<name-prefix>-user",
        "arn:aws:dynamodb:*:<account-id>:table/<name-prefix>-user/index/*"
      ]
    }
  ]
}
```

## Config Settings

Generated config will be in `config_gen.yaml`. Generated config refer to the config whose source of truth is on aws, such as arn of an aws resource object. We need to fetch from aws. For example, 

