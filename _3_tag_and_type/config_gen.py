# sub-project binding of the shared config utilities in /aws_utils/: the
# functions here fix dir_sub to THIS sub-project folder, so scripts call
# config_load() etc. without arguments. also holds the resource instance
# names, the reader of the generated cognito config, and the binding to the
# local es service of _2_local_es (name index of tags/types).

import sys
from pathlib import Path

import yaml

DIR_SELF = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR_SELF.parent))

import aws_utils
from aws_utils import aws_client_make  # noqa: F401  re-exported for scripts

DIR_LOCAL_ES = DIR_SELF.parent / "_2_local_es"
# appended (not inserted before this sub-project's own folder), so module
# names existing on both sides (test, ensure_architect) keep resolving to
# THIS sub-project's files
sys.path.append(str(DIR_LOCAL_ES))
from es_client import EsClient  # noqa: E402  aws-side library of _2_local_es

PATH_COGNITO_CONFIG_GEN = DIR_SELF.parent / "_0_auth_cognito" / "config_gen.yaml"
PATH_LOCAL_ES_CONFIG_GEN = DIR_LOCAL_ES / "config_gen.yaml"
PATH_LOCAL_ES_CLIENT = DIR_LOCAL_ES / "es_client.py"

# the four tables of the tag side and the four tables of the type side, in
# names_build key order. ensure/test scripts loop over this list.
TABLE_KEY_LIST = [
    "table_tag",
    "table_tag_history",
    "table_obj_tag",
    "table_obj_tag_history",
    "table_type",
    "table_type_history",
    "table_obj_type",
    "table_obj_type_history",
]


def config_load():
    return aws_utils.config_load(DIR_SELF)


def config_gen_load():
    return aws_utils.config_gen_load(DIR_SELF)


def config_gen_save(config_gen):
    aws_utils.config_gen_save(DIR_SELF, config_gen)


# how the tag/type name indices are built on the local es service: the 'char'
# index config gives case-insensitive substring search with match positions.
# only the name is indexed; user_id is an exact filter keeping every search
# inside the caller's own data.
ES_INDEX_CONFIG_NAME = "char"
ES_FIELD_CONFIG = {
    "field_list_char": ["name"],
    "field_list_exact": [{"name": "user_id", "type": "keyword"}],
}

# how long a requester waits for the worker's confirmation of one index task
ES_RESULT_TIMEOUT_SEC = 20
ES_RESULT_POLL_SEC = 0.25


def index_names_build(suffix=""):
    """es index names of this sub-project on the shared local elasticsearch.
    the '3_' start marks them as created by this sub-project (_3_...), per
    /doc/aws_oa.md#sub-project-namespace; they do not carry the aws name
    prefix because they are not aws resources. tests pass a suffix like
    '_temp_{timestamp}' to get throwaway index names."""
    return {
        "index_tag": f"3_tag{suffix}",
        "index_type": f"3_type{suffix}",
    }


def local_es_gen_load():
    """generated config of _2_local_es: region, task queue url/arn, result
    table name/arn. the queue and table belong to _2_local_es (own name
    prefix), so they are read from that sub-project's generated config."""
    if not PATH_LOCAL_ES_CONFIG_GEN.exists():
        raise SystemExit(
            f"{PATH_LOCAL_ES_CONFIG_GEN} not found, run _2_local_es/"
            "ensure_architect.py first"
        )
    with open(PATH_LOCAL_ES_CONFIG_GEN) as f:
        return yaml.safe_load(f)


def local_es_client_make(config, es_gen):
    """requester of the local es service for the ensure/test scripts of this
    sub-project (the lambda builds its own EsClient from env variables)."""
    region = es_gen["region_name"]
    return EsClient(
        sqs=aws_client_make(config, "sqs", region),
        db=aws_client_make(config, "dynamodb", region),
        queue_url=es_gen["queue_task"]["queue_url"],
        table_name=es_gen["table_result"]["table_name"],
        result_timeout_sec=ES_RESULT_TIMEOUT_SEC,
        result_poll_sec=ES_RESULT_POLL_SEC,
    )


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


def names_build(config):
    """names of the aws resource instances of the tag/type service, built from
    this service's own name prefix (each service has its own prefix). the
    prefix is terminated by the '--' separator per
    /doc/aws_oa.md#sub-project-namespace: {prefix}--{resource-name}. tag and
    type are kept fully independent at data level: each owns its entity table,
    entity history table, obj attach table and obj attach history table."""
    prefix = config["name_prefix"]
    return {
        "table_tag": f"{prefix}--tag",
        "table_tag_history": f"{prefix}--tag-history",
        "table_obj_tag": f"{prefix}--obj-tag",
        "table_obj_tag_history": f"{prefix}--obj-tag-history",
        "table_type": f"{prefix}--type",
        "table_type_history": f"{prefix}--type-history",
        "table_obj_type": f"{prefix}--obj-type",
        "table_obj_type_history": f"{prefix}--obj-type-history",
        "lambda_role": f"{prefix}--api-role",
        "lambda_function": f"{prefix}--api",
        "http_api": f"{prefix}--api",
    }
