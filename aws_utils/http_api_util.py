# api gateway http api with cognito jwt authorizer + lambda proxy integration:
# ensure/delete, shared by all sub-projects. the typical wiring order is:
#
#   http_api_ensure -> jwt_authorizer_ensure -> lambda_integration_ensure
#   -> api_route_ensure -> api_stage_ensure -> lambda_invoke_permission_ensure
#     (last one in lambda_util)

AUTHORIZER_NAME = "cognito-jwt"

# cors allows any origin, so during development a local dev server can call
# the deployed api directly
CORS_CONFIG = {
    "AllowOrigins": ["*"],
    "AllowMethods": ["*"],
    "AllowHeaders": ["authorization", "content-type"],
    "MaxAge": 3600,
}


def http_api_find(apigw, api_name):
    params = {}
    while True:
        page = apigw.get_apis(**params)
        for api in page["Items"]:
            if api["Name"] == api_name:
                return api
        if "NextToken" not in page:
            return None
        params["NextToken"] = page["NextToken"]


def http_api_ensure(apigw, api_name):
    api = http_api_find(apigw, api_name)
    if api is None:
        api = apigw.create_api(
            Name=api_name, ProtocolType="HTTP", CorsConfiguration=CORS_CONFIG
        )
        print(f"http api created: {api_name} ({api['ApiId']})")
    else:
        print(f"http api already exists: {api_name} ({api['ApiId']})")
    return api["ApiId"], api["ApiEndpoint"]


def jwt_authorizer_ensure(apigw, api_id, issuer, audience):
    jwt_config = {"Audience": [audience], "Issuer": issuer}
    existing = None
    for authorizer in apigw.get_authorizers(ApiId=api_id)["Items"]:
        if authorizer["Name"] == AUTHORIZER_NAME:
            existing = authorizer
    if existing is None:
        authorizer_id = apigw.create_authorizer(
            ApiId=api_id,
            Name=AUTHORIZER_NAME,
            AuthorizerType="JWT",
            IdentitySource=["$request.header.Authorization"],
            JwtConfiguration=jwt_config,
        )["AuthorizerId"]
        print(f"jwt authorizer created: {AUTHORIZER_NAME}")
        return authorizer_id

    authorizer_id = existing["AuthorizerId"]
    if existing.get("JwtConfiguration") == jwt_config:
        print(f"jwt authorizer already exists: {AUTHORIZER_NAME}, ok")
    else:
        apigw.update_authorizer(
            ApiId=api_id, AuthorizerId=authorizer_id, JwtConfiguration=jwt_config
        )
        print(f"jwt authorizer updated: {AUTHORIZER_NAME}")
    return authorizer_id


def lambda_integration_ensure(apigw, api_id, lambda_arn):
    for integration in apigw.get_integrations(ApiId=api_id)["Items"]:
        if integration.get("IntegrationUri") == lambda_arn:
            print("lambda integration already exists, ok")
            return integration["IntegrationId"]
    integration_id = apigw.create_integration(
        ApiId=api_id,
        IntegrationType="AWS_PROXY",
        IntegrationUri=lambda_arn,
        PayloadFormatVersion="2.0",
    )["IntegrationId"]
    print("lambda integration created")
    return integration_id


def api_route_ensure(apigw, api_id, route_key, integration_id, authorizer_id):
    target = f"integrations/{integration_id}"
    existing = None
    for route in apigw.get_routes(ApiId=api_id)["Items"]:
        if route["RouteKey"] == route_key:
            existing = route
    if existing is None:
        apigw.create_route(
            ApiId=api_id,
            RouteKey=route_key,
            Target=target,
            AuthorizationType="JWT",
            AuthorizerId=authorizer_id,
        )
        print(f"route created: {route_key}")
        return
    if existing.get("Target") == target and existing.get("AuthorizerId") == authorizer_id:
        print(f"route already exists: {route_key}, ok")
    else:
        apigw.update_route(
            ApiId=api_id,
            RouteId=existing["RouteId"],
            Target=target,
            AuthorizationType="JWT",
            AuthorizerId=authorizer_id,
        )
        print(f"route updated: {route_key}")


def api_stage_ensure(apigw, api_id):
    for stage in apigw.get_stages(ApiId=api_id)["Items"]:
        if stage["StageName"] == "$default":
            print("stage $default already exists, ok")
            return
    apigw.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True)
    print("stage $default created")


def http_api_delete(apigw, api_name):
    api = http_api_find(apigw, api_name)
    if api is None:
        print(f"http api does not exist: {api_name}")
        return
    apigw.delete_api(ApiId=api["ApiId"])
    print(f"http api deleted: {api_name}")
