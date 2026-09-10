# sqs queue operations: list queues, read/set msg retention.
#
# usage:
#   python sqs_ops.py list                  list all queues
#   python sqs_ops.py list <query>          list one queue (name, url, or arn)
#   python sqs_ops.py retention [query]     read msg retention (default: queue in config)
#   python sqs_ops.py retention [query] -s 2d   set msg retention
#
# note: in aws, the queue url is the id used by api calls, and the arn is the
# global id used in permission policies. both end with the queue name.
#
# iam permissions needed:
#   list: sqs:ListQueues, sqs:GetQueueUrl, sqs:GetQueueAttributes
#   retention read: sqs:GetQueueUrl, sqs:GetQueueAttributes
#   retention set: sqs:GetQueueUrl, sqs:GetQueueAttributes, sqs:SetQueueAttributes

import argparse
from datetime import datetime

from config import (
    config_load,
    queue_url_from_query,
    retention_format,
    retention_parse,
    sqs_client_make,
)


def queue_print(client, queue_url):
    resp = client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["All"])
    attrs = resp["Attributes"]
    queue_name = queue_url.rsplit("/", 1)[-1]
    time_created = datetime.fromtimestamp(int(attrs["CreatedTimestamp"]))
    msg_retention_sec = int(attrs["MessageRetentionPeriod"])
    print(queue_name)
    print(f"  url                : {queue_url}")
    print(f"  arn                : {attrs['QueueArn']}")
    print(f"  msg retention      : {retention_format(msg_retention_sec)} ({msg_retention_sec}s)")
    print(f"  msg count(approx)  : {attrs['ApproximateNumberOfMessages']}")
    print(f"  msg in flight      : {attrs['ApproximateNumberOfMessagesNotVisible']}")
    print(f"  created at         : {time_created}")


def run_list(client, query):
    if query is not None:
        queue_url = queue_url_from_query(client, query)
        if queue_url is None:
            print(f"queue not found: {query}")
            return
        queue_print(client, queue_url)
        return

    queue_url_list = []
    paginator = client.get_paginator("list_queues")
    for page in paginator.paginate():
        queue_url_list += page.get("QueueUrls", [])

    print(f"queue count: {len(queue_url_list)}")
    for queue_url in queue_url_list:
        queue_print(client, queue_url)


def run_retention(client, query, retention_set):
    queue_url = queue_url_from_query(client, query)
    if queue_url is None:
        print(f"queue not found: {query}")
        return
    queue_name = queue_url.rsplit("/", 1)[-1]

    if retention_set is None:
        resp = client.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["MessageRetentionPeriod"]
        )
        msg_retention_sec = int(resp["Attributes"]["MessageRetentionPeriod"])
        print(queue_name)
        print(f"  msg retention      : {retention_format(msg_retention_sec)} ({msg_retention_sec}s)")
        return

    msg_retention_sec = retention_parse(retention_set)
    resp = client.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["MessageRetentionPeriod"]
    )
    retention_current = int(resp["Attributes"]["MessageRetentionPeriod"])
    client.set_queue_attributes(
        QueueUrl=queue_url,
        Attributes={"MessageRetentionPeriod": str(msg_retention_sec)},
    )
    print(queue_name)
    if retention_current == msg_retention_sec:
        print(f"  msg retention      : {retention_format(msg_retention_sec)} (unchanged)")
    else:
        print(
            f"  msg retention      : {retention_format(retention_current)}"
            f" -> {retention_format(msg_retention_sec)}"
        )


def main():
    parser = argparse.ArgumentParser(description="sqs queue operations")
    sub = parser.add_subparsers(dest="action", required=True)

    p_list = sub.add_parser("list", help="list queues visible to the iam user")
    p_list.add_argument(
        "query",
        nargs="?",
        default=None,
        help="optional: queue name, url, or arn. if omitted, list all",
    )

    p_retention = sub.add_parser("retention", help="read or set msg retention of one queue")
    p_retention.add_argument(
        "query",
        nargs="?",
        default=None,
        help="queue name, url, or arn. if omitted, use queue_name in config",
    )
    p_retention.add_argument(
        "-s", "--set",
        dest="retention_set",
        default=None,
        metavar="VALUE",
        help="set retention, e.g. 1d, 12h, 3600. if omitted, read current value",
    )

    args = parser.parse_args()
    config = config_load()
    client = sqs_client_make(config)

    if args.action == "list":
        run_list(client, args.query)
    elif args.action == "retention":
        query = args.query if args.query is not None else config["sqs"]["queue_name"]
        run_retention(client, query, args.retention_set)


if __name__ == "__main__":
    main()
