# layered config load and generated config (config_gen.yaml) load/save,
# shared by all sub-projects (config-two-layer design).
#
# config layers of one sub-project, later overrides earlier:
#   /config/config.yaml          aws account config, example
#   /config/config.0.yaml        aws account config, authentic (git-ignored)
#   {dir_sub}/config.yaml        sub-project config, example
#   {dir_sub}/config.0.yaml      sub-project config, authentic (git-ignored)
#
# each sub-project typically wraps these in its own config module (e.g.
# config_gen.py) binding dir_sub to the sub-project folder, so its scripts
# just call config_load() without arguments.

from pathlib import Path

import boto3
import yaml

DIR_PROJECT = Path(__file__).resolve().parent.parent


def dict_update_deep(base, patch):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            dict_update_deep(base[key], value)
        else:
            base[key] = value


def config_load(dir_sub):
    """layered config of the sub-project living in dir_sub."""
    path_list = [
        DIR_PROJECT / "config" / "config.yaml",
        DIR_PROJECT / "config" / "config.0.yaml",
        Path(dir_sub) / "config.yaml",
        Path(dir_sub) / "config.0.yaml",
    ]
    config = {}
    for path in path_list:
        if not path.exists():
            continue
        with open(path) as f:
            config_patch = yaml.safe_load(f) or {}
        dict_update_deep(config, config_patch)
    return config


def config_gen_load(dir_sub):
    path = Path(dir_sub) / "config_gen.yaml"
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def config_gen_save(dir_sub, config_gen):
    with open(Path(dir_sub) / "config_gen.yaml", "w") as f:
        yaml.safe_dump(config_gen, f, sort_keys=False)


def aws_client_make(config, service, region_name=None):
    """boto3 client from the aws block of the layered config. region_name
    overrides the configured region when given."""
    aws = config["aws"]
    if region_name is None:
        region_name = aws["region_name"]
    return boto3.client(
        service,
        region_name=region_name,
        aws_access_key_id=aws["access_key_id"],
        aws_secret_access_key=aws["secret_access_key"],
    )
