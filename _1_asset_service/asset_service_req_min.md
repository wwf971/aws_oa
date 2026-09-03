<!-- This is a minimalist requirement document, aiming at letting reader get a overall grasp of core concepts/workflows, and design and implementation requiremen, at a few glances-->

This is a simple asset(file) service on aws.

The asset iteself will be stored in s3, and the asset metadata will be stored in dynamodb.

We allow two types of asset:
  1. Asset as a single file. We referred to it as file asset.
  2. Asset as a folder with nested folders/files nested under under it. We refer to it as folder asset.
  
Each asset has an id. And the s3 path will be:

  1. For single file asset: `/{bucket_name}/{id}/{asset_file_name}`
  2. For folder asset: `/{bucket_name}/{id}/xx/yy/{file_name}`. In this case, we naturally know there's a folder `/xx/yy/`. Fully empty folder is also allowed, using `/{bucket_name}/{id}/xx/yy/__@@FOLDER@@__`, and special text content in the s3 object.

Each asset also conceptually has a path(not to be confused with path under folder asset), and assets are in a virtual file tree. which should be represented in dynamodb. Each asset could be conceptually represented as (user_id, asset_id, name, parent_id, lexorank). Lexorank speficies an asset's order under it's parent. Lexorank should use string containing a-z,0-9. Folders in this virtual file tree has asset_id being null value, and root folder has parent_id also being null.

Common operations should be supported.
We don't support versioning for assets for the time being.
When downloading folder asset, it should be zipped and downloaded as a single file.

CloudFront distribution, api gateway, lambda function, s3 buckets, cognito should be able to be ensured(existence and proper configuration) via python script. All aws resource instances should have unified name prefix specified in config.yaml

## Specs

All s3 bucket should not have versioning enabled.
  For s3 bucket for assets, use standard-ia level.
  For s3 bucket for static web resources, use standard storage level.
