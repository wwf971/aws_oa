# config load logic and shared helpers for all sqs scripts.
#
# config layers, later overrides earlier:
#   /config/config.yaml     aws account config, example
#   /config/config.0.yaml   aws account config, authentic (git-ignored)
#   ./config.yaml           sqs test config, example
#   ./config.0.yaml         sqs test config, authentic (git-ignored)
#
# ./config_gen.yaml: info fetched back from aws after resource creation (url/arn).

import re
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError

RETENTION_RE = re.compile(r"^(\d+)([smhd])?$")
RETENTION_SEC_MIN = 60
RETENTION_SEC_MAX = 1209600  # 14 days, aws sqs limit

DIR_SELF = Path(__file__).resolve().parent
PATH_CONFIG_LIST = [
    DIR_SELF.parent / "config" / "config.yaml",
    DIR_SELF.parent / "config" / "config.0.yaml",
    DIR_SELF / "config.yaml",
    DIR_SELF / "config.0.yaml",
]
PATH_CONFIG_GEN = DIR_SELF / "config_gen.yaml"


def dict_update_deep(base, patch):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            dict_update_deep(base[key], value)
        else:
            base[key] = value


def config_load():
    config = {}
    for path in PATH_CONFIG_LIST:
        if not path.exists():
            continue
        with open(path) as f:
            config_patch = yaml.safe_load(f) or {}
        dict_update_deep(config, config_patch)
    return config


def config_gen_load():
    if not PATH_CONFIG_GEN.exists():
        return {}
    with open(PATH_CONFIG_GEN) as f:
        return yaml.safe_load(f) or {}


def config_gen_save(config_gen):
    with open(PATH_CONFIG_GEN, "w") as f:
        yaml.safe_dump(config_gen, f, sort_keys=False)


def sqs_client_make(config):
    aws = config["aws"]
    return boto3.client(
        "sqs",
        region_name=aws["region_name"],
        aws_access_key_id=aws["access_key_id"],
        aws_secret_access_key=aws["secret_access_key"],
    )


def retention_parse(text):
    """parse retention string like 1d, 12h, 30m, or raw seconds. returns seconds."""
    text = str(text).strip()
    if text.isdigit():
        sec = int(text)
    else:
        match = RETENTION_RE.match(text)
        if match is None:
            raise ValueError(f"invalid retention: {text!r}, use like 1d, 12h, 30m, 3600")
        num = int(match.group(1))
        unit = match.group(2) or "s"
        if unit == "s":
            sec = num
        elif unit == "m":
            sec = num * 60
        elif unit == "h":
            sec = num * 3600
        elif unit == "d":
            sec = num * 86400
        else:
            raise ValueError(f"invalid retention unit: {unit!r}")
    if sec < RETENTION_SEC_MIN or sec > RETENTION_SEC_MAX:
        raise ValueError(f"retention {sec}s out of range (60s .. 14d)")
    return sec


def retention_format(sec):
    """format seconds as human-readable string like 4d, 12h."""
    sec = int(sec)
    if sec % 86400 == 0:
        return f"{sec // 86400}d"
    if sec % 3600 == 0:
        return f"{sec // 3600}h"
    if sec % 60 == 0:
        return f"{sec // 60}m"
    return f"{sec}s"


def msg_retention_sec_get(config):
    """read msg_retention from sqs config. default 4d (aws default)."""
    return retention_parse(config["sqs"].get("msg_retention", "4d"))


def queue_url_find(client, queue_name):
    """look up queue url by name. returns None if the queue does not exist."""
    try:
        return client.get_queue_url(QueueName=queue_name)["QueueUrl"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("QueueDoesNotExist", "AWS.SimpleQueueService.NonExistentQueue"):
            return None
        raise


def queue_url_from_query(client, query):
    """resolve query (name / url / arn) to queue url. returns None if not found."""
    if query.startswith("http"):
        return query
    if query.startswith("arn:"):
        queue_name = query.rsplit(":", 1)[-1]
    else:
        queue_name = query
    return queue_url_find(client, queue_name)
