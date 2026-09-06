# sub-project binding of the shared config utilities in /aws_utils/: the
# functions here fix dir_sub to THIS sub-project folder, so scripts call
# config_load() etc. without arguments. also holds the resource instance
# names and the reader of the generated cognito config.

import sys
from pathlib import Path

import yaml

DIR_SELF = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR_SELF.parent))

import aws_utils
from aws_utils import aws_client_make  # noqa: F401  re-exported for scripts

PATH_COGNITO_CONFIG_GEN = DIR_SELF.parent / "_0_auth_cognito" / "config_gen.yaml"
PATH_TIMELINE_CONFIG_GEN = DIR_SELF.parent / "_1a_asset_timeline" / "config_gen.yaml"


def config_load():
    return aws_utils.config_load(DIR_SELF)


def config_gen_load():
    return aws_utils.config_gen_load(DIR_SELF)


def config_gen_save(config_gen):
    aws_utils.config_gen_save(DIR_SELF, config_gen)


def cognito_gen_load():
    """generated config of _0_auth_cognito: cognito ids (user pool id, app
    client ids) and the user table name. the user table belongs to
    _0_auth_cognito and uses that sub-project's own name prefix, so its name
    is read from there instead of being built from this service's prefix."""
    if not PATH_COGNITO_CONFIG_GEN.exists():
        raise SystemExit(
            f"{PATH_COGNITO_CONFIG_GEN} not found, run _0_auth_cognito/"
            "ensure_cognito.py and ensure_user_table.py first"
        )
    with open(PATH_COGNITO_CONFIG_GEN) as f:
        return yaml.safe_load(f)


def timeline_gen_load():
    """generated config of _1a_asset_timeline. returns None when that
    service has not been ensured yet; asset delete then skips collect-entry
    cleanup. the table belongs to the timeline service (own name prefix),
    so its name is read from there instead of being built from this
    service's prefix."""
    if not PATH_TIMELINE_CONFIG_GEN.exists():
        return None
    with open(PATH_TIMELINE_CONFIG_GEN) as f:
        return yaml.safe_load(f)


def names_build(config):
    """names of the aws resource instances of the asset service, built from
    this service's own name prefix (each service has its own prefix)."""
    prefix = config["name_prefix"]
    return {
        "bucket_asset": f"{prefix}-asset",
        "bucket_web": f"{prefix}-web",
        "table_asset_node": f"{prefix}-asset-node",
        "lambda_role": f"{prefix}-asset-api-role",
        "lambda_function": f"{prefix}-asset-api",
        "http_api": f"{prefix}-asset-api",
        "cloudfront_comment": f"{prefix}-asset-service",
        "cloudfront_oac": f"{prefix}-web-oac",
        "cloudfront_function": f"{prefix}-url-rewrite",
    }
