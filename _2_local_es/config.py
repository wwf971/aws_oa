# config load logic and shared helpers for all local es scripts.
#
# unlike other sub-projects, this module does NOT delegate to /aws_utils/:
# deploy_to_raspi.sh copies this file alone to the home server (where the
# worker imports it), so it must stay self-contained.
#
# config layers, later overrides earlier:
#   /config/config.yaml     aws account config, example
#   /config/config.0.yaml   aws account config, authentic (git-ignored)
#   ./config.yaml           local es service config, example
#   ./config.0.yaml         local es service config, authentic (git-ignored)
#
# on the home server, ./config.0.yaml also overrides the aws block with the
# worker iam user keys, so the home server never holds the account admin keys.
# refer to local_es_impl.md#config.
#
# ./config_gen.yaml: written by ensure_architect.py, the pointer to the aws
# resource instances (region, queue name/url/arn, table name/arn). the worker
# locates its queue and table only through this file.

import random
import re
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError

DURATION_RE = re.compile(r"^(\d+)([smhd])?$")
ID_CHARS = "0123456789abcdefghijklmnopqrstuvwxyz"

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


def names_build(config):
    """names of the aws resource instances of the local es service, built
    from this service's own name prefix."""
    prefix = config["name_prefix"]
    return {
        "queue_task": f"{prefix}-es-task.fifo",
        "table_result": f"{prefix}-es-result",
    }


def aws_client_make(config, service, region_name=None):
    """region_name defaults to the one in the aws config block. the worker
    passes the region from config_gen.yaml instead, so the home server config
    holds nothing but the worker iam keys."""
    aws = config["aws"]
    if region_name is None:
        region_name = aws["region_name"]
    return boto3.client(
        service,
        region_name=region_name,
        aws_access_key_id=aws["access_key_id"],
        aws_secret_access_key=aws["secret_access_key"],
    )


def id_random(length=16):
    """random 0-9a-z id, refer to id-format.md."""
    return "".join(random.choice(ID_CHARS) for _ in range(length))


def duration_parse(text):
    """parse duration string like 1d, 12h, 30m, or raw seconds. returns seconds."""
    text = str(text).strip()
    match = DURATION_RE.match(text)
    if match is None:
        raise ValueError(f"invalid duration: {text!r}, use like 1d, 12h, 30m, 3600")
    num = int(match.group(1))
    unit = match.group(2) or "s"
    sec_per_unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return num * sec_per_unit[unit]


def duration_format(sec):
    """format seconds as human-readable string like 4d, 12h."""
    sec = int(sec)
    if sec % 86400 == 0:
        return f"{sec // 86400}d"
    if sec % 3600 == 0:
        return f"{sec // 3600}h"
    if sec % 60 == 0:
        return f"{sec // 60}m"
    return f"{sec}s"


def queue_url_find(client, queue_name):
    """look up queue url by name. returns None if the queue does not exist."""
    try:
        return client.get_queue_url(QueueName=queue_name)["QueueUrl"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("QueueDoesNotExist", "AWS.SimpleQueueService.NonExistentQueue"):
            return None
        raise
