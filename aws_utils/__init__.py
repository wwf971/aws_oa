# shared utilities of all sub-projects. import from the package top level
# (from aws_utils import xxx), never from the internal modules, so the
# internal file layout can change without touching sub-projects.
# inventory + usage: /doc/aws_oa_impl.md#shared-utilities-aws_utils
#
# sub-project scripts make this package importable by inserting the project
# root into sys.path before the import:
#
#   sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
#   from aws_utils import table_ensure, ...

from aws_utils.config_util import (
    aws_client_make,
    config_gen_load,
    config_gen_save,
    config_load,
    dict_update_deep,
)
from aws_utils.dynamodb_util import (
    table_delete,
    table_ensure,
)
from aws_utils.http_api_util import (
    api_route_ensure,
    api_stage_ensure,
    http_api_delete,
    http_api_ensure,
    http_api_find,
    jwt_authorizer_ensure,
    lambda_integration_ensure,
)
from aws_utils.lambda_util import (
    lambda_delete,
    lambda_function_ensure,
    lambda_invoke_permission_ensure,
    lambda_role_delete,
    lambda_role_ensure,
    lambda_zip_build,
)
from aws_utils.s3_util import (
    bucket_delete,
    bucket_ensure,
    bucket_objects_delete,
)
from aws_utils.script_util import (
    TestFail,
    check,
    check_count,
    delete_confirm,
    step,
    timestamp_make,
    timestamp_resource_make,
)
