# destroy the sqs queue on aws, and remove its info from config_gen.yaml
# if the destroyed queue matches the one recorded there.
#
# usage:
#   python sqs_destroy.py           delete queue named in config
#   python sqs_destroy.py -n <name>  delete queue with given name
#
# iam permissions needed: sqs:GetQueueUrl, sqs:DeleteQueue

import argparse

from config import (
    config_gen_load,
    config_gen_save,
    config_load,
    queue_url_find,
    sqs_client_make,
)


def main():
    parser = argparse.ArgumentParser(description="delete an sqs queue")
    parser.add_argument(
        "-n", "--name",
        dest="queue_name",
        default=None,
        help="queue name to delete (default: queue_name in config)",
    )
    args = parser.parse_args()

    config = config_load()
    queue_name = args.queue_name if args.queue_name is not None else config["sqs"]["queue_name"]
    client = sqs_client_make(config)

    queue_url = queue_url_find(client, queue_name)
    if queue_url is None:
        print(f"queue already gone: {queue_name}")
    else:
        client.delete_queue(QueueUrl=queue_url)
        print(f"queue deleted: {queue_name}")

    config_gen = config_gen_load()
    sqs_gen = config_gen.get("sqs")
    if sqs_gen is not None and sqs_gen.get("queue_name") == queue_name:
        del config_gen["sqs"]
        config_gen_save(config_gen)
        print("queue info removed from config_gen.yaml")


if __name__ == "__main__":
    main()
