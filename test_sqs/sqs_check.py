# check the sqs queue: confirm it exists on aws and print its attributes.
# iam permissions needed: sqs:GetQueueUrl, sqs:GetQueueAttributes

from config import config_load, queue_url_find, sqs_client_make


def main():
    config = config_load()
    queue_name = config["sqs"]["queue_name"]
    client = sqs_client_make(config)

    queue_url = queue_url_find(client, queue_name)
    if queue_url is None:
        print(f"queue not found: {queue_name}")
        return

    resp = client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["All"])
    attrs = resp["Attributes"]

    print(f"queue found: {queue_name}")
    print(f"  url                : {queue_url}")
    print(f"  arn                : {attrs['QueueArn']}")
    print(f"  msg count(approx)  : {attrs['ApproximateNumberOfMessages']}")
    print(f"  msg in flight      : {attrs['ApproximateNumberOfMessagesNotVisible']}")


if __name__ == "__main__":
    main()
