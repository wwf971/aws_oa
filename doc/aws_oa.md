
Resource instance(s), resource object(s) are used interchangeably, some times simply referred to as resource(s)

## Layered Config

Config should be layered, for example having global config and local config.
Config only belonging to a sub-project should be kept in that sub-project's local layer.
Currently the config format conforms to design in `config-two-layer.md`.

Config that can only be fetched from aws(such as resource instance id(arn)) should be written into `config_gen.yaml`. This file should typically be written by script, not manually.

## Sub-Project Namespace

The resources objects created and maintained by each subproject, should be easily identifiable by a prefix. The prefix should be explicitly written in local config of the sub-project.

The prefix should begin with an user alias, followed by subproject name like alice123-file-service. Some existing sub-project might not conform to this naming style, but leave them as they are for the time being.

Actually, we need to conside the potential problem that the prefix on config is changed, already created resource objects on aws still have old name, and hence not easily identifiable. For the time being, we assume that the prefix does not change. We can make scripts supports a '--assume-prefix' when executing operations like deleting architecture, so that we can remove old architecture without the need to temporarily changing local sub-project config.

It is also engouraged that when a sub-project creates external resource objects, they use their own prefix to identify what it has created. For example, when utilizing external elasticsearch indices provided the local es sub-project(in `_{sub-project index}_local_es/` folder), the index name should be like `{sub-project index}_xxx`.

The prefix should typically start with a user alias, optionally followed by a time string, and then a service name. For exapmle:  alice123-2025-image-processing.

## Arthitecture Ensurement

For each sub-project, there should python script for ensuring the resources objects exists, and have proper configuration on aws.

'Ensure' means that if the same resource object already exists, then do no create/overwrite again, however if the configuration differs from specified, then we need to update it.

The main script should be called `ensure_architect.py`. Apart from directly executing it without parameters, it's ok to support command line arguments to allow operations(ensuring/inspecting/re-creating/deleting) on individual resource objects contained in architecture design.

The script should also be used for removing the sub-project's architecture from aws, using `--delete all` argument. For this operation, there should be prompt for user to type a string containing today's date(for example `confirm-19700101`) for final confirmation. Delete operation should also support assuming a sub-project prefix different from current one in sub-project's local config.

It is encouraged that common logic used by different sub-projecs to be extracted out and then shared to reduce duplication. For example, sub-projects that has the need to support dynamodb instance create/delete/inspect operation can have this logic extracted out as utility methods in utility scripts. Utility scripts should be put in `/aws_utils/`.

## Test Design

Each sub-project should have a test script(for example `test.py`) under the sub-project folder. There can be multiple test items(not mandantory, should be based on actual situation). There should be a default test item that is executed when simply running something like `python test.py`.

One typical test pattern is create resources-->do some operations-->finally delete them. This reproduces the process of ensurement and removal of resource instances, as well as operations performed on the resource instances.

In test, when creating resources, instead of directly using prefix specified in local config of the sub-project, use something like `{prefix}_temp_{19700101_12000000+09}` where +09 indicates timezone. This avoids the risk of collision with existing resource instances. Test should be suspended if detecting that a resource instance of same name and type of the test resource object to be created.

Test script should also support a `--clean` parameter to clean all resources created but not deleted by previous tests that failed halfway. These resources should be located by string matching. The `--clean` feature should also support `--assume-prefix`.

One exception to the create-then-delete pattern: iam roles used as lambda execution roles. A freshly created role can stay un-assumable by lambda for several minutes, and revoking a permission from an in-use role takes similarly long to become effective (granting is fast). Therefore:

- Test scripts should keep their lambda execution roles under STABLE names (still containing the `-temp-` marker so `--clean` finds them), reuse them across runs instead of recreating them, and only rewrite the inline policies each run to point at the current timestamped resources.
- A test that needs an operation to be denied (for example to check transaction rollback) should not revoke the permission from the in-use role and wait; it should switch the lambda to a second persistent role whose policy never allowed the operation.

Measurements and the debugging path behind this rule are in `./aws_oa_impl.md#iam-propagation`.

## Implementation Preference

PK/SK/GSI of DynamoDB should be properly designed. Generally, GSI projection type should be 'KEYS_ONLY' or 'INCLUDE', not 'ALL'. Reqeusting two times(GSI --> primary) to get full information is allowed.

All id format should conform to `id-format.md`. Time stamp display and storage should conform to `time-format.md`. This should be applied when implementing sub-projects.

Core requirement will usually be written in a file with name style like `xxx_req.md` or `xxx_req_min.md` under the sub-project folder. Human developer/ai agent its (incremental) implementation in a `xxx_impl.md` file, in a manner that conforms to principles in `doc-design.md`.