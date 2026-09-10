# sqs test

A minimal IaC-style playground for AWS SQS: the queue is declared in config,
scripts make aws match the config (create/check/destroy), and one script tests
sending/fetching messages.

## Config

Config layers, later overrides earlier:

```text
/config/config.yaml     aws account config (region, iam user keys), example
/config/config.0.yaml   aws account config, authentic (git-ignored)
./config.yaml           sqs test config (queue_name, msg_retention), example
./config.0.yaml         sqs test config, authentic (git-ignored)
```

`./config_gen.yaml` is written by scripts, not by hand. It holds info that only
exists after aws creates the queue (queue url, arn).

Desired state lives in config files, actual state fetched back from aws lives
in `config_gen.yaml`.

`msg_retention` controls how long undelivered messages stay in the queue (aws
default is 4d). Values like `1d`, `2d`, `12h`, `30m`, or raw seconds `3600`
are accepted. Max is 14d.

## Scripts

Run each with `conda run -n wwf python <script>` inside this folder.

```text
sqs_ensure.py    declare --> real: check queue by name, create if missing,
                 apply msg_retention from config, save url/arn to config_gen.yaml
sqs_check.py     look at real: does queue exist, how many messages inside
sqs_ops.py       look at / change real: list queues, read/set msg retention
sqs_test.py      use real: send / receive / peek messages
sqs_destroy.py   remove real: delete queue, clear config_gen.yaml if it matches
```

Typical lifecycle:

```text
sqs_ensure.py
  -> sqs_check.py (queue found, 0 messages)
  -> sqs_test.py  (send one msg -> fetch it back -> ack)
  -> sqs_destroy.py
```

### sqs_ops.py

```text
python sqs_ops.py list                  list all queues
python sqs_ops.py list <query>          list one queue (name, url, or arn)
python sqs_ops.py retention [query]     read msg retention (default: queue in config)
python sqs_ops.py retention [query] -s 2d   set msg retention
```

In aws, a queue has two ids besides its name: the queue url (id used by api
calls) and the arn (global id used in permission policies). Both end with the
queue name, so `<query>` accepts any of the three.

Msg retention can be updated on an existing queue (`SetQueueAttributes`). It is
also applied by `sqs_ensure.py` when config differs from aws.

### sqs_destroy.py

```text
python sqs_destroy.py           delete queue named in config
python sqs_destroy.py -n <name> delete queue with given name
```

If the deleted queue matches the one in `config_gen.yaml`, its entry is removed
there. Deleting a different queue leaves `config_gen.yaml` unchanged.

### sqs_test.py actions

```text
python sqs_test.py                     full test: send one msg -> fetch it back -> ack
python sqs_test.py put [text] [-n N]   send N messages (default 1) with given text
python sqs_test.py get [-n N | --all]  receive N messages (default 1) and ack them
python sqs_test.py peek [-n N]         read messages without ack (default: all)
```

Examples:

```text
python sqs_test.py put "order 42"      send one message "order 42"
python sqs_test.py put -n 10           send 10 messages
python sqs_test.py get --all           receive and ack everything in the queue
python sqs_test.py peek                print all messages, leave them in the queue
```

ack means deleting the message from the queue. In sqs, receiving a message does
not remove it: the message only becomes invisible for a while (visibility
timeout, default 30s), and comes back if not deleted. So a consumer must
explicitly delete(ack) the message after processing it. `peek` uses this on
purpose: it reads without deleting, so messages reappear after the timeout.

## IAM permissions needed

The iam user in config needs these permissions, per operation:

| operation | permissions |
| --- | --- |
| sqs_ensure.py | `sqs:GetQueueUrl`, `sqs:CreateQueue`, `sqs:GetQueueAttributes`, `sqs:SetQueueAttributes` |
| sqs_check.py | `sqs:GetQueueUrl`, `sqs:GetQueueAttributes` |
| sqs_ops.py list | `sqs:ListQueues`, `sqs:GetQueueUrl`, `sqs:GetQueueAttributes` |
| sqs_ops.py retention read | `sqs:GetQueueUrl`, `sqs:GetQueueAttributes` |
| sqs_ops.py retention set | `sqs:GetQueueUrl`, `sqs:GetQueueAttributes`, `sqs:SetQueueAttributes` |
| sqs_test.py | `sqs:SendMessage`, `sqs:ReceiveMessage`, `sqs:DeleteMessage` |
| sqs_destroy.py | `sqs:GetQueueUrl`, `sqs:DeleteQueue` |

For this test, attaching aws managed policy `AmazonSQSFullAccess` to the iam
user covers everything. In real projects, the app usually gets only the
read/write part (`SendMessage`/`ReceiveMessage`/`DeleteMessage` on one queue
arn), while create/destroy is done by a deploy role.
