# build the frontend and upsert the result into the s3 web bucket:
#   ensure the web bucket exists
#   -> pnpm build (login + main targets)
#   -> generate web-config.json (runtime config for the pages)
#   -> upload new/changed files (local md5 vs s3 etag), with content-type
#      and cache-control
#   -> delete s3 keys that no longer exist locally (vite output file names
#      contain content hashes, so stale hashed assets accumulate otherwise)
#   -> cloudfront invalidation for changed/deleted paths
#
# the web bucket is created here if missing, so this script does not depend on
# ensure_architect.py for uploading. the cloudfront invalidation step is
# skipped (with a note) until ensure_architect.py has created the distribution.

import hashlib
import json
import mimetypes
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DIR_SELF = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR_SELF.parent))

from aws_utils import bucket_ensure
from config_gen import (
    aws_client_make,
    cognito_gen_load,
    config_gen_load,
    config_load,
    names_build,
)
DIR_FRONTEND = DIR_SELF / "frontend"

CACHE_CONTROL_NO_CACHE = "no-cache"
CACHE_CONTROL_IMMUTABLE = "public, max-age=31536000, immutable"

# if more paths than this changed, invalidate everything with /*
INVALIDATION_PATHS_MAX = 15


def frontend_build():
    print("building frontend (pnpm build)...")
    subprocess.run(["pnpm", "build"], cwd=DIR_FRONTEND, check=True)


def web_config_build(config, cognito_gen):
    region = config["aws"]["region_name"]
    app_client = config["asset_service"]["cognito"]["app_client"]
    web_config = {
        "region": region,
        "cognito": {
            "domain": f"https://{cognito_gen['cognito']['domain_prefix']}"
                      f".auth.{region}.amazoncognito.com",
            "client_id": cognito_gen["cognito"]["app_client_ids"][app_client],
        },
        "api_base": "/api",
    }
    return json.dumps(web_config, indent=2).encode()


def local_files_collect(web_config_bytes):
    """s3 key -> local file bytes. login build goes to the bucket root,
    main build goes under main/."""
    files = {"web-config.json": web_config_bytes}
    for dist_dir, key_prefix in [
        (DIR_FRONTEND / "dist" / "login", ""),
        (DIR_FRONTEND / "dist" / "main", "main/"),
    ]:
        if not dist_dir.is_dir():
            raise SystemExit(f"build output missing: {dist_dir}")
        for path in dist_dir.rglob("*"):
            if path.is_file():
                key = key_prefix + path.relative_to(dist_dir).as_posix()
                files[key] = path.read_bytes()
    return files


def cache_control_for(key):
    # html and runtime config must always be revalidated; hashed assets never change
    if key.endswith(".html") or key == "web-config.json":
        return CACHE_CONTROL_NO_CACHE
    return CACHE_CONTROL_IMMUTABLE


def content_type_for(key):
    content_type, _ = mimetypes.guess_type(key)
    return content_type or "application/octet-stream"


def bucket_etags_fetch(s3, bucket_name):
    etags = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name):
        for obj in page.get("Contents", []):
            etags[obj["Key"]] = obj["ETag"].strip('"')
    return etags


def bucket_sync(s3, bucket_name, files_local):
    """returns the list of changed/deleted keys (for invalidation)."""
    etags_remote = bucket_etags_fetch(s3, bucket_name)
    keys_changed = []

    for key, content in sorted(files_local.items()):
        # s3 etag of a single-part upload is the md5 of the content
        md5_local = hashlib.md5(content).hexdigest()
        if etags_remote.get(key) == md5_local:
            print(f"  ok      : {key}")
            continue
        s3.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=content,
            ContentType=content_type_for(key),
            CacheControl=cache_control_for(key),
        )
        keys_changed.append(key)
        print(f"  uploaded: {key}")

    keys_stale = sorted(set(etags_remote) - set(files_local))
    for key in keys_stale:
        s3.delete_object(Bucket=bucket_name, Key=key)
        keys_changed.append(key)
        print(f"  deleted : {key}")
    return keys_changed


def cloudfront_invalidate(cloudfront, distribution_id, keys_changed):
    if not keys_changed:
        print("nothing changed, no invalidation needed")
        return
    paths = [f"/{key}" for key in keys_changed]
    if len(paths) > INVALIDATION_PATHS_MAX:
        paths = ["/*"]
    cloudfront.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "Paths": {"Quantity": len(paths), "Items": paths},
            "CallerReference": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"),
        },
    )
    print(f"cloudfront invalidation created: {', '.join(paths)}")


def main():
    config = config_load()
    names = names_build(config)
    region = config["aws"]["region_name"]
    cognito_gen = cognito_gen_load()

    frontend_build()
    web_config_bytes = web_config_build(config, cognito_gen)
    files_local = local_files_collect(web_config_bytes)

    s3 = aws_client_make(config, "s3")
    bucket_ensure(s3, names["bucket_web"], region)

    print(f"syncing {len(files_local)} files to bucket {names['bucket_web']}...")
    keys_changed = bucket_sync(s3, names["bucket_web"], files_local)

    # cloudfront exists only after ensure_architect.py has run
    service_gen = config_gen_load().get("asset_service")
    if service_gen is None:
        print("cloudfront not ensured yet (no asset_service in config_gen.yaml),")
        print("  skipped invalidation. run ensure_architect.py to serve the pages.")
        return
    cloudfront = aws_client_make(config, "cloudfront")
    cloudfront_invalidate(cloudfront, service_gen["cloudfront_distribution_id"], keys_changed)
    print(f"frontend url: https://{service_gen['cloudfront_domain']}/")


if __name__ == "__main__":
    main()
