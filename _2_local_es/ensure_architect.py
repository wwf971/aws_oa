# ensure the aws side of the local es service:
#   task queue   (sqs fifo)  carries tasks, aws -> home server
#   result table (dynamodb)  carries results, home server -> aws (ttl cleanup)
# then save region + names/url/arn into config_gen.yaml, the file through
# which the worker and the client locate these resources.
#
# iam permissions needed (deployer):
#   sqs: GetQueueUrl, CreateQueue, GetQueueAttributes, SetQueueAttributes
#   dynamodb: DescribeTable, CreateTable, DescribeTimeToLive, UpdateTimeToLive

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aws_utils import delete_confirm, table_delete, table_ensure
from config import (
    aws_client_make,
    config_gen_load,
    config_gen_save,
    config_load,
    duration_format,
    duration_parse,
    names_build,
    queue_url_find,
)

TASK_RETENTION_SEC_MIN = 60
TASK_RETENTION_SEC_MAX = 1209600  # 14 days, aws sqs limit


def args_parse():
    parser = argparse.ArgumentParser(
        description="ensure or delete the local es architecture"
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


def architecture_delete(config, names):
    delete_confirm(
        f"All local es AWS resources with prefix {config['name_prefix']!r} "
        "will be deleted."
    )
    sqs = aws_client_make(config, "sqs")
    db = aws_client_make(config, "dynamodb")

    queue_url = queue_url_find(sqs, names["queue_task"])
    if queue_url is None:
        print(f"queue does not exist: {names['queue_task']}")
    else:
        sqs.delete_queue(QueueUrl=queue_url)
        print(f"queue deleted: {names['queue_task']}")

    table_delete(db, names["table_result"])


def architecture_ensure(config, names):
    local_es = config["local_es"]
    task_retention_sec = duration_parse(local_es.get("task_retention", "1h"))
    task_visibility_sec = duration_parse(local_es.get("task_visibility", 60))
    if not TASK_RETENTION_SEC_MIN <= task_retention_sec <= TASK_RETENTION_SEC_MAX:
        raise SystemExit(f"task_retention {task_retention_sec}s out of range (60s .. 14d)")

    sqs = aws_client_make(config, "sqs")
    db = aws_client_make(config, "dynamodb")

    queue_url, queue_arn = queue_task_ensure(
        sqs, names["queue_task"], task_retention_sec, task_visibility_sec)
    table_arn = table_result_ensure(db, names["table_result"])

    config_gen = config_gen_load()
    # region goes into config_gen so the worker on the home server can locate
    # the resources through this one file, without any aws account config
    config_gen["region_name"] = config["aws"]["region_name"]
    config_gen["queue_task"] = {
        "queue_name": names["queue_task"],
        "queue_url": queue_url,
        "queue_arn": queue_arn,
    }
    config_gen["table_result"] = {
        "table_name": names["table_result"],
        "table_arn": table_arn,
    }
    config_gen_save(config_gen)
    print("saved to config_gen.yaml")


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


def queue_task_ensure(sqs, queue_name, task_retention_sec, task_visibility_sec):
    attrs_wanted = {
        "MessageRetentionPeriod": str(task_retention_sec),
        "VisibilityTimeout": str(task_visibility_sec),
    }
    queue_url = queue_url_find(sqs, queue_name)
    if queue_url is None:
        attrs_create = dict(attrs_wanted)
        attrs_create["FifoQueue"] = "true"
        queue_url = sqs.create_queue(QueueName=queue_name, Attributes=attrs_create)["QueueUrl"]
        print(f"queue created: {queue_name}")
    else:
        print(f"queue already exists: {queue_name}")
        resp = sqs.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=["MessageRetentionPeriod", "VisibilityTimeout"],
        )
        attrs_current = resp["Attributes"]
        attrs_diff = {
            key: value for key, value in attrs_wanted.items()
            if attrs_current.get(key) != value
        }
        if attrs_diff:
            sqs.set_queue_attributes(QueueUrl=queue_url, Attributes=attrs_diff)
            for key in attrs_diff:
                print(f"  {key}: {attrs_current.get(key)} -> {attrs_wanted[key]}")

    print(f"  task retention     : {duration_format(task_retention_sec)}")
    print(f"  task visibility    : {duration_format(task_visibility_sec)}")
    resp = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])
    queue_arn = resp["Attributes"]["QueueArn"]
    print(f"  url                : {queue_url}")
    print(f"  arn                : {queue_arn}")
    return queue_url, queue_arn


def table_result_ensure(db, table_name):
    table_ensure(
        db,
        table_name,
        attribute_definitions=[{"AttributeName": "task_id", "AttributeType": "S"}],
        key_schema=[{"AttributeName": "task_id", "KeyType": "HASH"}],
    )

    ttl = db.describe_time_to_live(TableName=table_name)["TimeToLiveDescription"]
    if ttl.get("TimeToLiveStatus") in ("ENABLED", "ENABLING"):
        print("  ttl                : already enabled on expire_at")
    else:
        db.update_time_to_live(
            TableName=table_name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "expire_at"},
        )
        print("  ttl                : enabled on expire_at")

    table_arn = db.describe_table(TableName=table_name)["Table"]["TableArn"]
    print(f"  arn                : {table_arn}")
    return table_arn


if __name__ == "__main__":
    main()
