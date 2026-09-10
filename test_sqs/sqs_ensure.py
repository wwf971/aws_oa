# ensure the sqs queue exists on aws: check by name, create if missing.
# apply msg_retention from config on create and on existing queue if different.
# then save queue url/arn into config_gen.yaml.
# iam permissions needed: sqs:GetQueueUrl, sqs:CreateQueue, sqs:GetQueueAttributes,
#                          sqs:SetQueueAttributes

from config import (
    config_gen_load,
    config_gen_save,
    config_load,
    msg_retention_sec_get,
    queue_url_find,
    retention_format,
    sqs_client_make,
)


def msg_retention_ensure(client, queue_url, msg_retention_sec):
    attrs = {"MessageRetentionPeriod": str(msg_retention_sec)}
    resp = client.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["MessageRetentionPeriod"]
    )
    retention_current = int(resp["Attributes"]["MessageRetentionPeriod"])
    if retention_current == msg_retention_sec:
        print(f"  msg retention      : {retention_format(msg_retention_sec)}")
        return
    client.set_queue_attributes(QueueUrl=queue_url, Attributes=attrs)
    print(
        f"  msg retention      : {retention_format(retention_current)}"
        f" -> {retention_format(msg_retention_sec)}"
    )


def main():
    config = config_load()
    queue_name = config["sqs"]["queue_name"]
    msg_retention_sec = msg_retention_sec_get(config)
    client = sqs_client_make(config)

    queue_url = queue_url_find(client, queue_name)
    if queue_url is None:
        queue_url = client.create_queue(
            QueueName=queue_name,
            Attributes={"MessageRetentionPeriod": str(msg_retention_sec)},
        )["QueueUrl"]
        print(f"queue created: {queue_name}")
        print(f"  msg retention      : {retention_format(msg_retention_sec)}")
    else:
        print(f"queue already exists: {queue_name}")
        msg_retention_ensure(client, queue_url, msg_retention_sec)

    resp = client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])
    queue_arn = resp["Attributes"]["QueueArn"]

    config_gen = config_gen_load()
    config_gen["sqs"] = {
        "queue_name": queue_name,
        "queue_url": queue_url,
        "queue_arn": queue_arn,
    }
    config_gen_save(config_gen)

    print(f"  url                : {queue_url}")
    print(f"  arn                : {queue_arn}")


if __name__ == "__main__":
    main()
