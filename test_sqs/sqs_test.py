# test the sqs queue: one full test run, or individual put/get/peek actions.
# reads queue url from config_gen.yaml (written by sqs_ensure.py).
#
# usage:
#   python sqs_test.py                     full test: send one msg -> fetch it back -> ack
#   python sqs_test.py put [text] [-n N]   send N messages (default 1) with given text
#   python sqs_test.py get [-n N | --all]  receive N messages (default 1) and ack them
#   python sqs_test.py peek [-n N]         read messages without ack (default: all)
#
# ack means deleting the message from the queue. a message received but not
# acked only becomes invisible for a while (visibility timeout, default 30s),
# then comes back to the queue.
#
# iam permissions needed: sqs:SendMessage, sqs:ReceiveMessage, sqs:DeleteMessage

import argparse
import time

from config import config_gen_load, config_load, sqs_client_make


def msg_put(client, queue_url, text, count):
    for i in range(count):
        msg_body = text if count == 1 else f"{text} #{i + 1}"
        client.send_message(QueueUrl=queue_url, MessageBody=msg_body)
        print(f"msg sent: {msg_body}")


def msg_fetch(client, queue_url, count_max, is_ack):
    """receive and print up to count_max messages (None = all there are).
    ack(delete) each message if is_ack. returns list of message bodies."""
    body_list = []
    while count_max is None or len(body_list) < count_max:
        count_want = 10 if count_max is None else min(10, count_max - len(body_list))
        resp = client.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=count_want, WaitTimeSeconds=2
        )
        msg_list = resp.get("Messages", [])
        if not msg_list:
            break
        for msg in msg_list:
            body_list.append(msg["Body"])
            print(f"msg {len(body_list)}: {msg['Body']}")
            if is_ack:
                client.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
    return body_list


def run_put(client, queue_url, args):
    text = args.text if args.text is not None else f"hello sqs {int(time.time())}"
    msg_put(client, queue_url, text, args.count)


def run_get(client, queue_url, args):
    count_max = None if args.all else args.count
    body_list = msg_fetch(client, queue_url, count_max, is_ack=True)
    print(f"msg received and acked: {len(body_list)}")


def run_peek(client, queue_url, args):
    body_list = msg_fetch(client, queue_url, args.count, is_ack=False)
    print(f"msg peeked (not acked, will come back after visibility timeout): {len(body_list)}")


def run_full(client, queue_url):
    msg_body_sent = f"hello sqs {int(time.time())}"
    msg_put(client, queue_url, msg_body_sent, count=1)
    body_list = msg_fetch(client, queue_url, count_max=1, is_ack=True)
    if not body_list:
        print("test result: FAIL (nothing arrived)")
        return
    is_match = body_list[0] == msg_body_sent
    print(f"test result: {'ok' if is_match else 'MISMATCH'}")


def main():
    parser = argparse.ArgumentParser(description="sqs test actions")
    sub = parser.add_subparsers(dest="action")

    p_put = sub.add_parser("put", help="send message(s) to the queue")
    p_put.add_argument("text", nargs="?", default=None, help="message text (default: hello + timestamp)")
    p_put.add_argument("-n", "--count", type=int, default=1, help="how many messages to send")

    p_get = sub.add_parser("get", help="receive message(s) and ack (delete) them")
    p_get.add_argument("-n", "--count", type=int, default=1, help="how many messages to receive")
    p_get.add_argument("--all", action="store_true", help="receive all messages in the queue")

    p_peek = sub.add_parser("peek", help="read message(s) without ack")
    p_peek.add_argument("-n", "--count", type=int, default=None, help="how many to read (default: all)")

    args = parser.parse_args()

    config = config_load()
    config_gen = config_gen_load()
    if "sqs" not in config_gen:
        print("no queue info in config_gen.yaml. run sqs_ensure.py first.")
        return
    queue_url = config_gen["sqs"]["queue_url"]
    client = sqs_client_make(config)

    if args.action == "put":
        run_put(client, queue_url, args)
    elif args.action == "get":
        run_get(client, queue_url, args)
    elif args.action == "peek":
        run_peek(client, queue_url, args)
    else:
        run_full(client, queue_url)


if __name__ == "__main__":
    main()
