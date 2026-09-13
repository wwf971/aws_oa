# tag / type service implementation plan

Independent tag and type services on aws. Tags/types are named entities owned by one user, organized in parent-child trees, and attachable to resource objects (assets of `../_1_asset_service` or anything else that has an id). All data lives in dynamodb, the api runs in lambda behind api gateway, and login reuses the cognito user pool of `../_0_auth_cognito`. Tag/type names are also char-level searchable through the local es service of `../_2_local_es` (each side owns one index there). No frontend yet: clients call the http api endpoint directly with a cognito jwt.

```text
client (jwt in Authorization header)
  └─ https://<api-endpoint>/api/*   (api gateway http api, jwt authorizer)
                  │
                  ▼
               lambda ──┬──────────────┬──────────────┐
                        ▼              ▼              ▼
                  tag tables      type tables     user table
                  (tag, tag-      (same shape,    (of _0_auth_cognito,
                   history,        own set)        sub -> user_id)
                   obj-tag,
                   obj-tag-history)
                        │
                        │ index tasks / search, via the task queue +
                        │ result table of _2_local_es
                        ▼
                  name indices 3_tag / 3_type
                  (elasticsearch on the home server)
```

## Core Concepts

- **tag / type**: a named entity of one user: `(user_id, tag_id, name, parent_id?, is_history_enabled, create/modify times)`. tag and type are almost the same concept for now, but they are kept **fully independent at data and api level**: each side owns its own four tables and its own `/api/tag/*` / `/api/type/*` paths. only the lambda code is shared, through a 'kind' descriptor holding one side's table set; if the two concepts diverge in the future, the shared function is split instead of growing branches.
- **parent-child**: a tag/type has at most one parent (`parent_id` attribute, absent = root), so the tags/types of a user form trees. circular relationships are prohibited: changing a parent first walks the new parent's ancestor chain and fails if the entity itself appears there.
- **attach entry**: one row meaning "obj X has tag Y at order position lexorank". obj ids are plain references, not validated against the sub-project owning the obj; as long as the owning sub-project can resolve an obj id, these entries work for it.
- **lexorank**: order of the tags/types of one obj; string of chars 0-9 a-z, plain string compare gives the order. server-side append picks the midpoint between the last rank and 'end'; a client can send an explicit midpoint rank when inserting/reordering. ranks never end in `0`, so a gap always exists below every rank.
- **name index**: each side owns one char-level index on the local es service (`../_2_local_es`), holding one doc per tag/type: `{name, user_id}` keyed by the entity id. A search query matches any substring of a name, case-insensitive, and returns match positions for highlighting; `user_id` is an exact filter keeping every search inside the caller's own data. The dynamodb tables are the source of truth: search results are resolved through dynamodb before being returned, so an index doc without its entity is harmless — but an entity that is not indexed would silently never appear in char search. Therefore every write api runs its index operation FIRST and only touches dynamodb after the worker confirmed it: create/rename/delete fail without changing dynamodb when the index is not updated (worker down, sqs message not consumed, no confirmation before the timeout).
- **edit history**: two independent history kinds, both stored with sort key `'{time_stamp zero-padded to 16}#{random}'` so one query gives time order:
  - per tag/type: logged only while its `is_history_enabled` is on. the history record is written in the SAME dynamodb transaction as the edit, so a failed history write fails the edit itself. toggling off removes all history of that entity; toggling on restarts logging with the toggle edit itself. deleting a tag/type drops its history.
  - per obj: attach/reorder/detach records, always logged (same-transaction too). supports deleting records EARLIER than a given time point; deleting a time range or specific records is not supported.

Time points are unix millisecond numbers, each with a companion timezone attribute in signed **minutes** (e.g. +09:00 -> 540), per `time-format.md`. Write apis accept an optional `time_zone` (defaults to 0) recorded alongside the operation time.

## AWS Resource Instances

All names start with `{prefix}--`, where `{prefix}` = `name_prefix` from `./config.yaml` (authentic value in `./config.0.yaml`), style `{user alias}-{time}-{sub-project name}`, and `--` is the prefix separator of `/doc/aws_oa.md#sub-project-namespace`.

| resource | name | purpose |
|---|---|---|
| dynamodb table | `{prefix}--tag` | tag basic info, one row per tag |
| dynamodb table | `{prefix}--tag-history` | edit history of each tag |
| dynamodb table | `{prefix}--obj-tag` | attach entries (obj x tag x lexorank) |
| dynamodb table | `{prefix}--obj-tag-history` | tag edit history of each obj |
| dynamodb table | `{prefix}--type`, `--type-history`, `--obj-type`, `--obj-type-history` | the type side, same shapes |
| iam role | `{prefix}--api-role` | lambda execution role |
| lambda | `{prefix}--api` | all api logic |
| api gateway (http api) | `{prefix}--api` | jwt authorizer + route `/api/*` -> lambda |
| dynamodb table | (own prefix of `_0_auth_cognito`)`-user` | user id mapping, owned by `_0_auth_cognito`; name read from its `config_gen.yaml` |

External resource objects on the local es service (not aws resources, so no aws prefix; the `3_` start marks them as created by this sub-project, `_3_...`):

| resource | name | purpose |
|---|---|---|
| es index (config `char`) | `3_tag` | name index of the tag side |
| es index (config `char`) | `3_type` | name index of the type side |

Both indices are built from the same field config: `field_list_char: [name]`, `field_list_exact: [{user_id, keyword}]`. The task queue and result table used to reach them belong to `../_2_local_es` and are read from that sub-project's `config_gen.yaml`.

### entity table `{prefix}--tag` (type side identical, with `type_id`)

```text
PK  user_id
SK  tag_id                random 0-9a-z id
attributes:
  name
  parent_id               tag_id of the parent; ABSENT for a root tag
  is_history_enabled      bool
  create_at / create_at_timezone      unix ms / signed minutes
  modify_at / modify_at_timezone
```

One partition per user; a user's tag count is small, so children lookups and case-insensitive name search filter in lambda over that one query.

### entity history table `{prefix}--tag-history`

```text
PK  tag_id
SK  time_key              '{time_stamp zero-padded to 16}#{random 4 chars}'
attributes:
  user_id                 who performed the edit
  operation               'create' / 'update'
  detail                  map of the changed attributes
  time_stamp / time_stamp_timezone
```

### attach table `{prefix}--obj-tag`

```text
PK  obj_id
SK  tag_id
attributes:
  user_id
  lexorank
  create_at / create_at_timezone

gsi_tag_id   PK tag_id, projection INCLUDE (user_id)
             ('which objs have this tag'; also locates the entries to
              detach when a tag is deleted)
```

### obj history table `{prefix}--obj-tag-history`

```text
PK  obj_id
SK  time_key              as in the entity history table
attributes:
  user_id
  operation               'attach' / 'reorder' / 'detach'
  tag_id
  lexorank                (attach / reorder)
  time_stamp / time_stamp_timezone
```

`delete history before t` is one key-condition query (`time_key < '{pad(t)}'`) plus a batch delete.

## Access Control

Same two roles as the asset service, from cognito groups in the jwt: no access group -> rejected; with it -> guest (all GET apis); plus the admin group -> writes too. Group names are in local config. `user_id` is resolved from jwt `sub` via the user table of `_0_auth_cognito`. All data is scoped to the caller's `user_id` (including reads of obj partitions).

## API

All responses use `{code, data, message}`; code 0 = success, code < 0 = failure. Every `tag` path below also exists with `type` instead of `tag`. Time stamps are unix ms integers; `time_zone` parameters are signed minutes and optional (default 0).

| method + path | role | effect |
|---|---|---|
| GET `/api/me` | guest | user_id, username, email, role |
| GET `/api/tag?name=` | guest | list own tags, optional case-insensitive name filter (in lambda, over the dynamodb partition) |
| GET `/api/tag/search?query=&limit=` | guest | char-level name search through the name index; each result is the tag plus `match_list` (`[{field, index_start, index_end}]`) for highlighting. `search` is a reserved path word, never a tag id |
| POST `/api/tag` | admin | create `{name, parent_id?, is_history_enabled?, time_zone?}`; fails without indexing the name |
| GET `/api/tag/{id}` | guest | one tag |
| PATCH `/api/tag/{id}` | admin | update `{name?, parent_id? (null = move to root), is_history_enabled?, time_zone?}`; a name change fails without re-indexing |
| DELETE `/api/tag/{id}` | admin | delete; refused while the tag has children; fails without removing the name from the index; detaches it from every obj (logged to each obj's history) and drops its own history |
| GET `/api/tag/{id}/children` | guest | direct children |
| GET `/api/tag/{id}/ancestor` | guest | ancestor chain, nearest first |
| GET `/api/tag/{id}/history?limit=` | guest | edit history, newest first |
| GET `/api/tag/{id}/obj` | guest | ids of objs the tag is attached to |
| GET `/api/obj/{obj_id}/tag` | guest | attach entries in lexorank order |
| POST `/api/obj/{obj_id}/tag` | admin | attach `{tag_id, lexorank?, time_zone?}`; no lexorank = append; fails if already attached |
| PATCH `/api/obj/{obj_id}/tag/{id}` | admin | reorder `{lexorank, time_zone?}` |
| DELETE `/api/obj/{obj_id}/tag/{id}?time_zone=` | admin | detach |
| GET `/api/obj/{obj_id}/tag-history?limit=` | guest | obj tag edit history, newest first |
| DELETE `/api/obj/{obj_id}/tag-history?before=` | admin | delete records earlier than the unix ms point |

## Scripts

```text
_3_tag_and_type/
  config_gen.py         sub-project binding of shared config utilities +
                        resource names + cognito generated-config reader +
                        local es binding (index names, requester client)
  ensure_architect.py   ensure 2 name indices -> 8 tables -> lambda role
                        -> lambda -> http api;
                        --delete all (--assume-prefix) for removal
  test.py               see below
  backend/              lambda source, zipped by the ensure script together
                        with ../_2_local_es/es_client.py
```

Generic ensure/delete logic (tables, role, lambda, http api, delete confirmation) lives in `/aws_utils/`; refer to `/doc/aws_oa_impl.md#shared-utilities-aws_utils`. This sub-project only defines its table schemas, role policy, lambda env and its name indices.

Ensuring or deleting the name indices goes through the local es pipeline, so the worker on the home server must be running; the ensure script ensures the indices FIRST, before any aws resource is touched (the write apis refuse to work without the index anyway). `--delete all` with `--assume-prefix` deletes only the aws resources: the index names carry no aws prefix, so the indices may still belong to the currently deployed architecture.

Deploy order for a fresh account: `_0_auth_cognito` (ensure_cognito.py, ensure_user_table.py) -> `_2_local_es` (ensure_architect.py + worker deployed and running) -> `ensure_architect.py` here.

The lambda reaches the local es service through env variables provided by the ensure script (`ES_QUEUE_URL`, `ES_RESULT_TABLE`, `ES_REGION`, `ES_RESULT_TIMEOUT_SEC`, `INDEX_TAG`, `INDEX_TYPE`), values taken from `../_2_local_es/config_gen.yaml`; its execution role gets the requester permissions (send task, read result) on those two resource instances.

### test.py

```text
backend (default)
  -> create a TEMP stack from zero under prefix {prefix}-temp-{timestamp}
     (2 temp name indices 3_{tag|type}_temp_{timestamp}, all 8 tables, role,
      lambda; suspended if same-name resources exist; the index check also
      proves the es worker is reachable)
  -> run the whole api flow through the temp lambda with claims of a real
     admin cognito user (fabricated authorizer event, fake obj ids):
     tag tree crud, circular parent prohibition, char-level name search
     (match positions, case-insensitive substring, rename/delete reflected),
     history toggling, attach/reorder/detach with lexorank order, obj history
     delete-before, and a type-side check proving the two sides stay
     independent (including separate name indices)
  -> index-confirmation failure check: with the lambda's result timeout set
     to 0 the confirmation never arrives in time, and create/rename/delete
     must fail leaving dynamodb unchanged
  -> remove every temp resource (attempted even when a check failed)

api
  -> the DEPLOYED http api rejects requests without a jwt
     (needs ensure_architect.py to have been run)

--clean [--assume-prefix xxx]
  -> locate residue of failed runs by the {prefix}-temp- name marker
     (lambda / role / tables) and remove it, together with the temp indices
     of those runs (index names are derived from the timestamps inside the
     found aws residue names: the local es service has no index list api, so
     an index whose aws residue is already fully removed must be deleted via
     _2_local_es/es_client.py index-delete instead)
```

The temp prefix uses `-temp-{timestamp}` with the timezone sign written as `p`/`m` (e.g. `20260830_04571488p09`), because aws resource names do not allow `+`.

## AWS Permission Settings (for the deploying iam user)

Inline policy for `ensure_architect.py` and `test.py`. Replace `<prefix>` (the `name_prefix` in `./config.0.yaml`), `<es-prefix>` (the `name_prefix` of `../_2_local_es`) and `<account-id>` (the 12-digit aws account id). The dynamodb arn uses `<prefix>-*`, which covers the 8 deployed tables (`<prefix>--*`) and the `-temp-` tables of `test.py`; account-level permissions shared by the test scripts of all sub-projects (cognito-idp:ListUsers etc.) are listed once in `/doc/aws_oa_impl.md#test-script-permissions`.

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
      "Resource": "arn:aws:dynamodb:*:<account-id>:table/<prefix>-*"
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
        "arn:aws:lambda:*:<account-id>:function:<prefix>--api",
        "arn:aws:lambda:*:<account-id>:function:<prefix>-temp-*",
        "arn:aws:iam::<account-id>:role/<prefix>--api-role",
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
    },
    {
      "Sid": "EsRequesterTaskSend",
      "Effect": "Allow",
      "Action": ["sqs:GetQueueUrl", "sqs:SendMessage"],
      "Resource": "arn:aws:sqs:*:<account-id>:<es-prefix>-es-task.fifo"
    },
    {
      "Sid": "EsRequesterResultRead",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem"],
      "Resource": "arn:aws:dynamodb:*:<account-id>:table/<es-prefix>-es-result"
    }
  ]
}
```

notes:

- `iam:PassRole` lets the deployer attach the role to the lambda (both the deployed one and temp ones from test.py).
- api gateway v2 has no fine-grained action names; access is controlled by http verb on the `apigateway:*` arn space.
- the lambda uses `dynamodb:TransactWriteItems` internally; it needs no extra iam action, transactions are authorized through the per-item actions (PutItem/DeleteItem) already in the lambda role.
- the EsRequester statements are the requester policy of `../_2_local_es` (refer to `../_2_local_es/local_es_impl.md#aws-config`): the ensure/test scripts ensure and delete the name indices themselves. The deployed lambda holds the same two permissions through its execution role.
- permissions of `_0_auth_cognito` (cognito + user table) are documented in `../_0_auth_cognito/test_cognito_impl.md`.
