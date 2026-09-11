# s3 bucket ensure/delete, shared by all sub-projects. all buckets are
# private (public access blocked) with versioning kept off.

from botocore.exceptions import ClientError


def bucket_ensure(s3, bucket_name, region):
    try:
        s3.head_bucket(Bucket=bucket_name)
        print(f"bucket already exists: {bucket_name}")
    except s3.exceptions.ClientError:
        params = {"Bucket": bucket_name}
        # us-east-1 is the default location and must not be passed as constraint
        if region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**params)
        print(f"bucket created: {bucket_name}")
    s3.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    versioning = s3.get_bucket_versioning(Bucket=bucket_name)
    if versioning.get("Status") == "Enabled":
        s3.put_bucket_versioning(
            Bucket=bucket_name, VersioningConfiguration={"Status": "Suspended"}
        )
        print("  versioning: was enabled, suspended")


def aws_error_code(error):
    return error.response.get("Error", {}).get("Code")


def bucket_objects_delete(s3, bucket_name):
    """empty the bucket: object versions + delete markers, plain objects,
    and unfinished multipart uploads."""
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket_name):
        objects = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for item in page.get("Versions", []) + page.get("DeleteMarkers", [])
        ]
        if objects:
            s3.delete_objects(
                Bucket=bucket_name,
                Delete={"Objects": objects, "Quiet": True},
            )

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            s3.delete_objects(
                Bucket=bucket_name,
                Delete={"Objects": objects, "Quiet": True},
            )

    paginator = s3.get_paginator("list_multipart_uploads")
    for page in paginator.paginate(Bucket=bucket_name):
        for upload in page.get("Uploads", []):
            s3.abort_multipart_upload(
                Bucket=bucket_name,
                Key=upload["Key"],
                UploadId=upload["UploadId"],
            )


def bucket_delete(s3, bucket_name):
    try:
        s3.head_bucket(Bucket=bucket_name)
    except ClientError as error:
        if aws_error_code(error) in ("404", "NoSuchBucket", "NotFound"):
            print(f"bucket does not exist: {bucket_name}")
            return
        raise
    bucket_objects_delete(s3, bucket_name)
    s3.delete_bucket(Bucket=bucket_name)
    print(f"bucket deleted: {bucket_name}")
