# aws-side client of the local es service: send one task into the task
# queue, then poll the result table until the worker's result appears.
# every method returns {code, data?, message?}; code 0 = success, < 0 = failure.
#
# used as a library by services on aws (lambda, backend servers):
#
#   from es_client import es_client_make
#   client = es_client_make()
#   client.index_ensure("some_index", "char", {
#       "field_list_char": ["title", "url"],
#       "field_list_exact": [{"name": "user_id", "type": "keyword"}]})
#   client.doc_put("some_index", "doc01", {"title": "...", "user_id": "..."})
#   client.search("some_index", "char", "substring", ["title", "url"],
#                 filter_exact={"user_id": "..."})
#
# "char" is the index config name; field_config shape belongs to that config.
# refer to local_es_impl.md#index-configs.
#
# also a small cli for manual ops (-c/-e describe the field_config of the
# char index config):
#
#   python es_client.py index-ensure <index> --config char -c title,url [-e user_id:keyword]
#   python es_client.py index-check <index>
#   python es_client.py index-recreate <index> --config char -c title,url [-e user_id:keyword]
#   python es_client.py index-delete <index>
#   python es_client.py doc-put <index> <doc_id> '<json>'
#   python es_client.py doc-delete <index> <doc_id>
#   python es_client.py search <index> <query> --config char -f title,url [-e user_id:xxx] [-n 20]
#
# iam permissions needed (requester):
#   sqs: SendMessage (+ GetQueueUrl when config_gen.yaml is absent)
#   dynamodb: GetItem

import argparse
import json
import time

from config import (
    aws_client_make,
    config_gen_load,
    config_load,
    id_random,
    names_build,
    queue_url_find,
)


class EsClient:
    def __init__(self, sqs, db, queue_url, table_name,
                 result_timeout_sec, result_poll_sec):
        self.sqs = sqs
        self.db = db
        self.queue_url = queue_url
        self.table_name = table_name
        self.result_timeout_sec = result_timeout_sec
        self.result_poll_sec = result_poll_sec

    # ---- task send + result wait, shared by every api method below ----

    def request_run(self, action, payload):
        task_id = id_random()
        task = {"task_id": task_id, "action": action, "payload": payload}
        self.sqs.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(task),
            MessageGroupId=payload["index_name"],  # keeps task order per index
            MessageDeduplicationId=task_id,
        )
        return self.result_wait(task_id)

    def result_wait(self, task_id):
        deadline = time.monotonic() + self.result_timeout_sec
        while time.monotonic() < deadline:
            resp = self.db.get_item(
                TableName=self.table_name, Key={"task_id": {"S": task_id}})
            if "Item" in resp:
                return json.loads(resp["Item"]["result"]["S"])
            time.sleep(self.result_poll_sec)
        return {
            "code": -1,
            "message": (f"no result after {self.result_timeout_sec}s,"
                        " is the worker running on the home server?"),
        }

    # ---- general index api, one method per action ----

    def index_ensure(self, index_name, config_name, field_config):
        """config_name picks the index config; field_config shape belongs
        to that config, e.g. for "char": {field_list_char, field_list_exact}"""
        return self.request_run("index_ensure", {
            "index_name": index_name,
            "config_name": config_name,
            "field_config": field_config,
        })

    def index_check(self, index_name):
        return self.request_run("index_check", {"index_name": index_name})

    def index_recreate(self, index_name, config_name, field_config):
        return self.request_run("index_recreate", {
            "index_name": index_name,
            "config_name": config_name,
            "field_config": field_config,
        })

    def index_delete(self, index_name):
        return self.request_run("index_delete", {"index_name": index_name})

    def doc_put(self, index_name, doc_id, doc):
        return self.request_run("doc_put", {
            "index_name": index_name, "doc_id": doc_id, "doc": doc})

    def doc_delete(self, index_name, doc_id):
        return self.request_run("doc_delete", {
            "index_name": index_name, "doc_id": doc_id})

    def doc_put_batch(self, index_name, doc_list):
        """doc_list: [{doc_id, doc}]"""
        return self.request_run("doc_put_batch", {
            "index_name": index_name, "doc_list": doc_list})

    def doc_delete_batch(self, index_name, doc_id_list):
        return self.request_run("doc_delete_batch", {
            "index_name": index_name, "doc_id_list": doc_id_list})

    def search(self, index_name, config_name, query_tree, field_list,
               filter_exact=None, limit=100):
        return self.request_run("search", {
            "index_name": index_name,
            "config_name": config_name,
            "query_tree": query_tree,
            "field_list": field_list,
            "filter_exact": filter_exact or {},
            "limit": limit,
        })


def es_client_make(config=None):
    if config is None:
        config = config_load()
    names = names_build(config)
    local_es = config.get("local_es", {})
    sqs = aws_client_make(config, "sqs")
    db = aws_client_make(config, "dynamodb")
    queue_url = config_gen_load().get("queue_task", {}).get("queue_url")
    if queue_url is None:
        queue_url = queue_url_find(sqs, names["queue_task"])
    if queue_url is None:
        raise SystemExit(
            f"task queue not found: {names['queue_task']}, run ensure_architect.py first")
    return EsClient(
        sqs=sqs,
        db=db,
        queue_url=queue_url,
        table_name=names["table_result"],
        result_timeout_sec=float(local_es.get("result_timeout", 20)),
        result_poll_sec=float(local_es.get("result_poll_interval", 0.25)),
    )


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def _name_list_parse(text):
    return [name for name in text.split(",") if name]


def _field_config_build(args):
    # cli flags -c/-e carry the field_config of the char index config
    return {
        "field_list_char": _name_list_parse(args.char),
        "field_list_exact": _field_list_exact_parse(args.exact),
    }


def _field_list_exact_parse(text):
    # "user_id:keyword,is_trashed:boolean" -> [{name, type}]
    field_list = []
    for part in text.split(","):
        if not part:
            continue
        name, field_type = part.split(":")
        field_list.append({"name": name, "type": field_type})
    return field_list


def _filter_exact_parse(text):
    # "user_id:xxx" -> {user_id: "xxx"}. values are passed as strings,
    # which is enough for keyword fields on the cli.
    filter_exact = {}
    for part in text.split(","):
        if not part:
            continue
        name, value = part.split(":", 1)
        filter_exact[name] = value
    return filter_exact


def main():
    parser = argparse.ArgumentParser(description="local es service client")
    sub = parser.add_subparsers(dest="action", required=True)

    p = sub.add_parser("index-ensure", help="create index if missing")
    p.add_argument("index_name")
    p.add_argument("--config", required=True, help="index config name, e.g. char")
    p.add_argument("-c", "--char", required=True, help="char-level text fields, e.g. title,url")
    p.add_argument("-e", "--exact", default="", help="exact fields, e.g. user_id:keyword,is_trashed:boolean")

    p = sub.add_parser("index-check", help="does index exist, how many docs")
    p.add_argument("index_name")

    p = sub.add_parser("index-recreate", help="delete and re-create index (docs are lost)")
    p.add_argument("index_name")
    p.add_argument("--config", required=True, help="index config name, e.g. char")
    p.add_argument("-c", "--char", required=True)
    p.add_argument("-e", "--exact", default="")

    p = sub.add_parser("index-delete", help="delete index (docs are lost)")
    p.add_argument("index_name")

    p = sub.add_parser("doc-put", help="put one doc")
    p.add_argument("index_name")
    p.add_argument("doc_id")
    p.add_argument("doc_json", help='doc as json, e.g. \'{"title": "abc"}\'')

    p = sub.add_parser("doc-delete", help="delete one doc")
    p.add_argument("index_name")
    p.add_argument("doc_id")

    p = sub.add_parser("search", help="search under the given index config")
    p.add_argument("index_name")
    p.add_argument("query")
    p.add_argument("--config", required=True, help="index config name, e.g. char")
    p.add_argument("-f", "--fields", required=True, help="fields to search, e.g. title,url")
    p.add_argument("-e", "--exact", default="", help="exact filter, e.g. user_id:xxx")
    p.add_argument("-n", "--limit", type=int, default=100)

    args = parser.parse_args()
    client = es_client_make()

    if args.action == "index-ensure":
        result = client.index_ensure(
            args.index_name, args.config, _field_config_build(args))
    elif args.action == "index-check":
        result = client.index_check(args.index_name)
    elif args.action == "index-recreate":
        result = client.index_recreate(
            args.index_name, args.config, _field_config_build(args))
    elif args.action == "index-delete":
        result = client.index_delete(args.index_name)
    elif args.action == "doc-put":
        result = client.doc_put(args.index_name, args.doc_id, json.loads(args.doc_json))
    elif args.action == "doc-delete":
        result = client.doc_delete(args.index_name, args.doc_id)
    elif args.action == "search":
        result = client.search(
            args.index_name, args.config, args.query,
            _name_list_parse(args.fields),
            _filter_exact_parse(args.exact), args.limit)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
