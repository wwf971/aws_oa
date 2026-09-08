# local es implementation plan

Elasticsearch runs on a home server; services on aws use it through a task queue and a result table. The home server keeps no open port: its worker long-polls tasks from sqs and writes results into dynamodb, both being outbound https calls from home to aws.

```text
service on aws (lambda / backend server)
  es_client: send task ---------------> sqs task queue (fifo)
  es_client: poll result <---+                     |
                             |                     | long poll (outbound, from home)
     dynamodb result table <-+--- write result --- worker on home server
                                                   |
                                                   v
                                          local elasticsearch
```

## Core Concepts

- **task**: one elasticsearch operation, as a json message `{task_id, action, payload}` in the task queue. `task_id` is a random 0-9a-z id.
- **result**: the outcome of one task, `{code, data?, message?}`, stored in the result table under the task's `task_id`. code 0 = success, code < 0 = failure.
- **requester**: any aws-side code using `es_client.py`. it sends a task and polls the result table until the result appears or its timeout passes.
- **worker**: `es_worker.py`, a daemon on the home server. it fetches tasks, runs them on the local elasticsearch, writes results, then acks the tasks.
- **index config**: a named recipe deciding how an index is built (mapping, analyzer) and how a search runs on it. The task that creates an index, and the task that searches it, both pick one by `config_name`. Currently one index config exists, `char` (case-insensitive substring match with match positions for highlight), and more can be added; refer to [Index Configs](#index-configs).

Why one queue plus one table, instead of two queues: the task direction fits a queue (any worker fetch order is fine, and polling means the home server needs no reachable endpoint). The result direction does not fit a queue, because each requester must fetch exactly the result of its own task; with a shared result queue a requester would receive other requesters' results. A table keyed by `task_id` gives exact correlation, and ttl cleans up results nobody fetched.

## AWS Resource Instances

All names start with `{prefix}` = `name_prefix` from `./config.yaml` (authentic value in `./config.0.yaml`).

| resource | name | purpose |
|---|---|---|
| sqs fifo queue | `{prefix}-es-task.fifo` | tasks, aws -> home server |
| dynamodb table | `{prefix}-es-result` | results, home server -> aws (ttl on `expire_at`) |

### task queue `{prefix}-es-task.fifo`

- fifo, message group id = `index_name`: all tasks of one index are delivered in send order, so doc put / doc delete of the same doc can never be applied swapped (the requirement on write order). different indexes do not block each other.
- message deduplication id = `task_id`: a retried send of the same task is not delivered twice.
- `task_retention` (default 1h): a task no worker ever fetched dies out after this; the requester has long given up by then.
- `task_visibility` (default 60s): a task fetched but not acked (worker crashed mid-task) reappears after this and is run again.

### result table `{prefix}-es-result`

```text
table {prefix}-es-result
  PK  task_id
  attributes:
    result       the {code, data?, message?} json as one string
    expire_at    epoch seconds, ttl attribute
```

A result is written once by the worker, read once by the requester, then abandoned; ttl (`result_expire`, default 10m) deletes it. Billing is on-demand, fitting the small and bursty traffic.

## Task Actions

`payload` per action, and the `data` inside the result:

| action | payload | result data |
|---|---|---|
| `index_ensure` | `index_name, config_name, field_config` | `{is_created}` |
| `index_check` | `index_name` | `{is_existing, doc_count?}` |
| `index_recreate` | `index_name, config_name, field_config` | - |
| `index_delete` | `index_name` | `{is_deleted}` |
| `doc_put` | `index_name, doc_id, doc` | - |
| `doc_delete` | `index_name, doc_id` | - |
| `doc_put_batch` | `index_name, doc_list: [{doc_id, doc}]` | - |
| `doc_delete_batch` | `index_name, doc_id_list` | - |
| `search` | `index_name, config_name, query_tree, field_list, filter_exact?, limit?` | see below |

- `config_name`: which index config the index is built with and searched under, refer to [Index Configs](#index-configs).
- `field_config`: the fields of the index; its shape is defined by the index config (for `char`: `{field_list_char, field_list_exact}`).
- `query_tree`: plain text, or nested `{"term": ...}` / `{"and": [...]}` / `{"or": [...]}` / `{"not": ...}` (same shape as in tab-utils `tab_cloud.md`). how one term matches text is the index config's business.
- `filter_exact`: `{field_name: value}`, each becomes an exact term filter.
- search result data: `[{doc_id, match_list: [{field, index_start, index_end}]}]`, positions over the original text, for the consumer to highlight.

The local elasticsearch is shared by many services (some not even going through this task queue), and they must not bother one another. So every index created here is stamped: the mapping `_meta` records the `config_name` + `field_config` it was built from. `index_ensure` on an existing index compares the stamp with the request: same stamp means it is this requester's own index (`is_created: false`, the normal re-ensure), while no stamp or a different stamp means the index belongs to someone else, and the task fails instead of silently adopting a foreign index.

Destructive actions (`index_recreate`, `index_delete`) are executed as received; asking the user for confirmation is the consumer frontend's business.

## Index Configs

An index config is a named recipe: at index create it decides the index body (mapping, analyzer), at search it decides the query body and how match positions are extracted. Only the worker knows what a config name means: each config is one module `index_config_{name}.py`, registered under its name in `INDEX_CONFIG_MAP` of `es_index_local.py`. Config-agnostic actions (index check/delete, doc put/delete) never look at any config. Adding a new config = one new module + one registry entry; nothing on the aws side changes.

All elasticsearch specifics live in `es_index_local.py` and the config modules on the worker side; nothing on the aws side knows an elasticsearch query body.

Available index configs (currently one):

### index config `char`: substring match with positions

field_config of this config:

- `field_list_char`: names of the char-level text fields, e.g. `["title", "url"]`.
- `field_list_exact`: exact-value fields used for filtering, `[{name, type}]` with type `keyword` / `boolean` / `long`.

Same design as the `char_index` experiments and tab-utils:

- a pattern tokenizer with empty pattern splits text into one token per character, plus a lowercase filter, so a `match_phrase` query is a case-insensitive substring match.
- char fields store `term_vector: with_positions_offsets`, so the fvh highlighter can mark one whole matched substring as one range (the plain highlighter would mark every char separately).
- match positions are extracted from the highlight tags and returned as `index_start` / `index_end`.

## Worker (home server)

```text
es_worker.py
  -> load config; load config_gen.yaml (region, queue url, table name)
  -> connect local es
  -> loop forever
       -> long poll task queue (20s wait, up to 10 msgs)
       -> for each task
            -> run action on local elasticsearch
                 (exception -> result {code: -1, message})
            -> write result item (with ttl expire_at)
            -> ack (delete message)
```

One worker process is expected. Long polling costs almost nothing while idle (one request per 20s) and delivers a task nearly instantly when one arrives.

### How the Worker Finds Its Queue

The worker never derives resource names or region from config values: it reads `./config_gen.yaml`, the file `ensure_architect.py` wrote when it created the resources (region, queue name/url/arn, table name/arn). Deploying this file to the home server together with the code (step 3 of the deploy order below) is what tells the worker which queue to listen to and which table to write. Without this file the worker refuses to start.

## Requester (es_client)

```text
es_client method, e.g. search(...)
  -> build task {task_id, action, payload}
  -> send to task queue (group id = index_name, dedup id = task_id)
  -> poll result table by task_id every result_poll_interval
       -> result found -> return it
       -> result_timeout passed -> return {code: -1, message: "no result ..."}
```

Typical round trip with the worker up: queue delivery + es operation + result write + one poll interval, well under a second for search. `doc_put` waits for an index refresh (~1s) so that a search right after sees the doc; batch actions refresh once at the end instead of per doc.

## Failure Handling

- worker offline: tasks pile up in the queue, requesters time out with `code: -1`. Piled-up tasks die out after `task_retention`.
- worker crashes after the es operation but before ack: the task reappears after `task_visibility` and is run again. Every action is safe to re-run (put overwrites by id, delete of a missing doc is a no-op, ensure/check/search change nothing), and the duplicate result overwrites the same `task_id` item.
- fifo side effect: while one task of an index is fetched but not acked, later tasks of that same index wait. A dead worker therefore stalls an index for up to `task_visibility`.
- this service guarantees per-index order and at-least-once execution, nothing more. Keeping the index consistent with a source-of-truth database is the consumer's business (e.g. the journal + repair design in tab-utils `tab_cloud.md#consistency-between-dynamodb-and-index`).

## Config

Config layers, later overrides earlier:

```text
/config/config.yaml     aws account config (region, iam user keys), example
/config/config.0.yaml   aws account config, authentic (git-ignored)
./config.yaml           local es service config, example
./config.0.yaml         local es service config, authentic (git-ignored)
```

`./config_gen.yaml` is written by `ensure_architect.py`, not by hand. It is the pointer to the aws resource instances: region, queue name/url/arn, table name/arn. The worker locates its queue and table only through this file, refer to [How the Worker Finds Its Queue](#how-the-worker-finds-its-queue).

The elasticsearch block follows the named-endpoint style shared with other projects: named blocks (e.g. `local`) each holding `scheme`/`host`/`port`, and `endpoint_use` choosing the active one. Only the worker reads it.

On the home server, `./config.0.yaml` is generated by the deploy script, never written by hand: it carries this service's authentic local config (e.g. the elasticsearch endpoint), plus the aws iam user keys merged from the config layers on the deploying machine. Since the region comes from `config_gen.yaml`, these keys are the only aws config the home server needs:

```yaml
# ./config.0.yaml on the home server, written by deploy_to_raspi.sh
aws:
  access_key_id: <iam user key, merged from the config layers>
  secret_access_key: <iam user secret>
```

By default the keys are the ones of the global config (`/config/config.0.yaml`). To hand the home server a least-privilege credential instead, put a worker iam user's keys (worker policy in [AWS Config](#aws-config)) under `aws:` in the service-local `./config.0.yaml` on the deploying machine; the service-local layer overrides the global layer, so those keys are the ones deployed.

## How tab-utils Uses This

The tab-utils backend keeps its general index api (`tab_server_index.py`) unchanged for its callers, and swaps the implementation inside from direct elasticsearch calls to `es_client` calls:

```text
index_ensure()        -> client.index_ensure(index_name, "char",
                             {field_list_char: ["title", "url"],
                              field_list_exact: [{name: userId, type: keyword},
                                                 {name: isTrashed, type: boolean},
                                                 {name: contentRevision, type: long}]})
doc_put(tab)          -> client.doc_put(index_name, tab.id, doc_of_tab(tab))
doc_delete(tab_id)    -> client.doc_delete(index_name, tab_id)
search(...)           -> client.search(index_name, "char", query_tree, field_list,
                             filter_exact={userId, isTrashed}, limit)
```

The tab-utils backend then only needs the requester iam permissions below; its search keeps working with the elasticsearch sitting safely in the home network.

## Ensure Script (IaC entry point)

```text
ensure_architect.py   ensure fifo task queue (retention, visibility)
                      -> ensure result table (on-demand, ttl on expire_at)
                      -> save region + names/url/arn to config_gen.yaml
```

'ensure' = create if missing, update if config differs, never blind-recreate.

Deploy order for a fresh setup:

```text
1. ensure_architect.py                (run from a machine with deployer keys;
                                       writes config_gen.yaml)
2. script/deploy_to_raspi.sh          copies code + config.yaml + config_gen.yaml,
                                      generates the remote config.0.yaml (local
                                      authentic config + aws keys from the config
                                      layers), installs dependencies, and
                                      installs/restarts the systemd worker service
3. aws side: use es_client.py (library or cli)
```

## Test (`test.py`)

Run `python test.py` from the local machine (same lan as the elasticsearch), with the worker running on the home server.

Every operation goes through the real pipeline (`es_client` -> task queue -> worker -> local elasticsearch), while the script also reads that same elasticsearch directly, only to verify. The script itself never writes to elasticsearch, so every change it observes proves the worker really fetched and executed the task — the worker is the only task consumer in existence.

Test pattern, on one throwaway index `2_test_{random}`:

```text
test.py
  -> index_ensure (create), verify index + stamp appear
  -> index_ensure again: same config passes, conflicting config must fail
  -> doc_put / doc_put_batch, read docs back directly, index_check doc count
  -> search: substring hits, match positions, filter_exact
  -> doc_delete / doc_delete_batch, verify docs gone
  -> index_delete, verify index gone
  -> (a failed run deletes the test index directly, nothing is left behind)
```

## AWS Config

IAM permissions should be properly set so that ensurement of aws architecture can succeed.
Three roles, each needing its own permissions. `<prefix>` and `<account-id>` are filled the same way as described in `../_1_asset_service/asset_service_impl.md#aws-permission-settings-for-the-deploying-iam-user`.

| role | held by | permissions |
|---|---|---|
| deployer | the machine running `ensure_architect.py` | queue + table create/describe/update |
| requester | aws services using `es_client.py` | send task, read result |
| worker | the home server only | fetch+ack task, write result |

One iam user holding the union of these permissions can serve all three roles (the deploy script then copies that user's keys onto the home server). For a stricter setup, use a separate worker iam user with only the policy below, so leaking the home server exposes tasks and results, never the aws account:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TaskFetch",
      "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage"],
      "Resource": "arn:aws:sqs:*:<account-id>:<prefix>-es-task.fifo"
    },
    {
      "Sid": "ResultWrite",
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem"],
      "Resource": "arn:aws:dynamodb:*:<account-id>:table/<prefix>-es-result"
    }
  ]
}
```

Requester policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TaskSend",
      "Effect": "Allow",
      "Action": ["sqs:GetQueueUrl", "sqs:SendMessage"],
      "Resource": "arn:aws:sqs:*:<account-id>:<prefix>-es-task.fifo"
    },
    {
      "Sid": "ResultRead",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem"],
      "Resource": "arn:aws:dynamodb:*:<account-id>:table/<prefix>-es-result"
    }
  ]
}
```

Deployer policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "QueueEnsure",
      "Effect": "Allow",
      "Action": [
        "sqs:GetQueueUrl", "sqs:CreateQueue",
        "sqs:GetQueueAttributes", "sqs:SetQueueAttributes"
      ],
      "Resource": "arn:aws:sqs:*:<account-id>:<prefix>-es-task.fifo"
    },
    {
      "Sid": "TableEnsure",
      "Effect": "Allow",
      "Action": [
        "dynamodb:DescribeTable", "dynamodb:CreateTable",
        "dynamodb:DescribeTimeToLive", "dynamodb:UpdateTimeToLive"
      ],
      "Resource": "arn:aws:dynamodb:*:<account-id>:table/<prefix>-es-result"
    }
  ]
}
```
