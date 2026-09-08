# the worker daemon that runs on the home server. it initiates all
# connections: long-poll tasks from the sqs task queue, run each task on the
# local elasticsearch, write the result into the dynamodb result table, then
# ack (delete) the task. the home server never listens for anything.
#
# which queue/table to use comes from ./config_gen.yaml (region, queue url,
# table name), written by ensure_architect.py and deployed here together with
# the code. refer to local_es_impl.md#how-the-worker-finds-its-queue.
#
# run on the home server: conda run -n wwf python es_worker.py
#
# iam permissions needed (worker iam user, refer to local_es_impl.md):
#   sqs: ReceiveMessage, DeleteMessage
#   dynamodb: PutItem

import json
import time
from datetime import datetime

import es_index_local as es_index
from config import (
    aws_client_make,
    config_gen_load,
    config_load,
    duration_parse,
)


# core loop: fetch tasks -> run each on local es -> write result -> ack task.
# start reading here.
def worker_run():
    config = config_load()
    config_gen = config_gen_load()
    for key in ("region_name", "queue_task", "table_result"):
        if key not in config_gen:
            raise SystemExit(
                f"config_gen.yaml missing or has no {key}: run ensure_architect.py"
                " on the deployer machine, then deploy this folder"
                " (including config_gen.yaml) to the home server")
    region_name = config_gen["region_name"]
    queue_name = config_gen["queue_task"]["queue_name"]
    queue_url = config_gen["queue_task"]["queue_url"]
    table_result = config_gen["table_result"]["table_name"]
    result_expire_sec = duration_parse(config["local_es"].get("result_expire", "10m"))

    sqs = aws_client_make(config, "sqs", region_name)
    db = aws_client_make(config, "dynamodb", region_name)
    es = es_index.es_connect(config)
    if not es.ping():
        raise SystemExit("local elasticsearch not reachable, check elasticsearch config")

    print(f"worker started, queue: {queue_name}, table: {table_result}")
    while True:
        resp = sqs.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=20)
        for msg in resp.get("Messages", []):
            task = json.loads(msg["Body"])
            result = task_run(es, task)
            result_write(db, table_result, task["task_id"], result, result_expire_sec)
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
            log_suffix = f", {result['message']}" if "message" in result else ""
            print(f"{time_now_text()} task {task['task_id']}"
                  f" {task.get('action')}: code {result['code']}{log_suffix}")


def task_run(es, task):
    action = task.get("action")
    handler = ACTION_HANDLER_MAP.get(action)
    if handler is None:
        return {"code": -1, "message": f"unknown action: {action}"}
    try:
        data = handler(es, task.get("payload", {}))
    except Exception as error:
        return {"code": -1, "message": f"{type(error).__name__}: {error}"}
    result = {"code": 0}
    if data is not None:
        result["data"] = data
    return result


def result_write(db, table_name, task_id, result, result_expire_sec):
    expire_at = int(time.time()) + result_expire_sec
    db.put_item(
        TableName=table_name,
        Item={
            "task_id": {"S": task_id},
            "result": {"S": json.dumps(result)},
            "expire_at": {"N": str(expire_at)},
        },
    )


# one handler per action: unpack payload, call the elasticsearch layer.

def _run_index_ensure(es, payload):
    return es_index.index_ensure(
        es, payload["index_name"], payload["config_name"],
        payload.get("field_config", {}))


def _run_index_check(es, payload):
    return es_index.index_check(es, payload["index_name"])


def _run_index_recreate(es, payload):
    return es_index.index_recreate(
        es, payload["index_name"], payload["config_name"],
        payload.get("field_config", {}))


def _run_index_delete(es, payload):
    return es_index.index_delete(es, payload["index_name"])


def _run_doc_put(es, payload):
    return es_index.doc_put(es, payload["index_name"], payload["doc_id"], payload["doc"])


def _run_doc_delete(es, payload):
    return es_index.doc_delete(es, payload["index_name"], payload["doc_id"])


def _run_doc_put_batch(es, payload):
    return es_index.doc_put_batch(es, payload["index_name"], payload["doc_list"])


def _run_doc_delete_batch(es, payload):
    return es_index.doc_delete_batch(es, payload["index_name"], payload["doc_id_list"])


def _run_search(es, payload):
    return es_index.search(
        es, payload["index_name"], payload["config_name"],
        payload["query_tree"], payload["field_list"],
        payload.get("filter_exact"), payload.get("limit", 100))


ACTION_HANDLER_MAP = {
    "index_ensure": _run_index_ensure,
    "index_check": _run_index_check,
    "index_recreate": _run_index_recreate,
    "index_delete": _run_index_delete,
    "doc_put": _run_doc_put,
    "doc_delete": _run_doc_delete,
    "doc_put_batch": _run_doc_put_batch,
    "doc_delete_batch": _run_doc_delete_batch,
    "search": _run_search,
}


def time_now_text():
    # like 20260824_21301512+09 (10ms precision), refer to time-format.md
    now = datetime.now().astimezone()
    offset_hour = int(now.utcoffset().total_seconds() // 3600)
    return f"{now.strftime('%Y%m%d_%H%M%S')}{now.microsecond // 10000:02d}{offset_hour:+03d}"


if __name__ == "__main__":
    worker_run()
