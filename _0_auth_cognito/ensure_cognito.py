# ensure the cognito resources on aws match the local config, following steps:
#   ensure user pool -> ensure resource servers -> ensure app clients
#   -> disable public signup -> ensure groups -> ensure users
#   -> ensure group memberships
# resource servers are ensured before app clients because app client oauth
# scopes like api/read only exist after resource server 'api' is created.
#
# 'ensure' means: create if missing, update if config differs, never recreate.
# after ensuring, user pool id and app client ids are saved to config_gen.yaml.
# iam permissions needed: see test_cognito_impl.md
#
# config layers, later overrides earlier:
#   /config/config.yaml     aws account config, example
#   /config/config.0.yaml   aws account config, authentic (git-ignored)
#   ./config.yaml           cognito test config, example
#   ./config.0.yaml         cognito test config, authentic (git-ignored)

import sys
from pathlib import Path
from urllib.parse import quote

DIR_SELF = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR_SELF.parent))

import aws_utils
from aws_utils import aws_client_make, dict_update_deep

# update_user_pool() resets every field absent from the request to its default,
# so on update we must send back the existing values of all fields we do not manage.
USER_POOL_UPDATE_KEYS = [
    "Policies",
    "DeletionProtection",
    "LambdaConfig",
    "AutoVerifiedAttributes",
    "SmsVerificationMessage",
    "EmailVerificationMessage",
    "EmailVerificationSubject",
    "VerificationMessageTemplate",
    "SmsAuthenticationMessage",
    "UserAttributeUpdateSettings",
    "MfaConfiguration",
    "DeviceConfiguration",
    "EmailConfiguration",
    "SmsConfiguration",
    "UserPoolTags",
    "AdminCreateUserConfig",
    "UserPoolAddOns",
    "AccountRecoverySetting",
]

# same story for update_user_pool_client().
APP_CLIENT_UPDATE_KEYS = [
    "ClientName",
    "RefreshTokenValidity",
    "AccessTokenValidity",
    "IdTokenValidity",
    "TokenValidityUnits",
    "ReadAttributes",
    "WriteAttributes",
    "ExplicitAuthFlows",
    "SupportedIdentityProviders",
    "CallbackURLs",
    "LogoutURLs",
    "DefaultRedirectURI",
    "AllowedOAuthFlows",
    "AllowedOAuthScopes",
    "AllowedOAuthFlowsUserPoolClient",
    "PreventUserExistenceErrors",
    "EnableTokenRevocation",
    "EnablePropagateAdditionalUserContextData",
    "AuthSessionValidity",
]

# config value -> value accepted by cognito api
OAUTH_FLOW_MAP = {
    "authorization_code": "code",
    "implicit": "implicit",
    "client_credentials": "client_credentials",
}

ATTR_TYPE_MAP = {
    "string": "String",
    "number": "Number",
    "boolean": "Boolean",
    "datetime": "DateTime",
}


def config_load():
    return aws_utils.config_load(DIR_SELF)


def config_gen_load():
    return aws_utils.config_gen_load(DIR_SELF)


def config_gen_save(config_gen):
    aws_utils.config_gen_save(DIR_SELF, config_gen)


def cognito_client_make(config):
    return aws_client_make(config, "cognito-idp")


# ---------------------------------------------------------------- user pool


def password_policy_build(policy_config):
    return {
        "MinimumLength": policy_config["minimum_length"],
        "RequireUppercase": policy_config["require_uppercase"],
        "RequireLowercase": policy_config["require_lowercase"],
        "RequireNumbers": policy_config["require_numbers"],
        "RequireSymbols": policy_config["require_symbols"],
    }


def schema_attr_build(attr_config):
    return {
        "Name": attr_config["name"],
        "AttributeDataType": ATTR_TYPE_MAP[attr_config["type"]],
        "Mutable": attr_config["mutable"],
    }


def user_pool_find(client, pool_name):
    paginator = client.get_paginator("list_user_pools")
    for page in paginator.paginate(MaxResults=60):
        for pool in page["UserPools"]:
            if pool["Name"] == pool_name:
                return pool["Id"]
    return None


def user_pool_update(client, pool, patch):
    """call update_user_pool with existing values kept for unmanaged fields."""
    params = {"UserPoolId": pool["Id"]}
    for key in USER_POOL_UPDATE_KEYS:
        if key in pool:
            params[key] = pool[key]
    dict_update_deep(params, patch)
    # describe returns the deprecated UnusedAccountValidityDays, which conflicts
    # with PasswordPolicy.TemporaryPasswordValidityDays when sent back in update.
    if "AdminCreateUserConfig" in params:
        params["AdminCreateUserConfig"].pop("UnusedAccountValidityDays", None)
    client.update_user_pool(**params)


def password_policy_ensure(client, pool, password_policy):
    current = pool["Policies"]["PasswordPolicy"]
    diff_keys = [key for key in password_policy if current.get(key) != password_policy[key]]
    if not diff_keys:
        print("  password policy    : ok")
        return
    merged = dict(current)
    merged.update(password_policy)
    user_pool_update(client, pool, {"Policies": {"PasswordPolicy": merged}})
    print(f"  password policy    : updated ({', '.join(diff_keys)})")


def custom_attributes_ensure(client, pool, attrs_config):
    # schema attributes can only be added, never removed or modified on aws.
    existing_names = {attr["Name"] for attr in pool.get("SchemaAttributes", [])}
    attrs_missing = []
    for attr_config in attrs_config:
        if f"custom:{attr_config['name']}" in existing_names:
            print(f"  custom attribute   : {attr_config['name']} ok")
        else:
            attrs_missing.append(schema_attr_build(attr_config))
    if attrs_missing:
        client.add_custom_attributes(UserPoolId=pool["Id"], CustomAttributes=attrs_missing)
        for attr in attrs_missing:
            print(f"  custom attribute   : {attr['Name']} added")


def user_pool_ensure(client, pool_config):
    pool_name = pool_config["name"]
    password_policy = password_policy_build(pool_config["password_policy"])
    attrs_config = pool_config.get("custom_attributes", [])

    pool_id = user_pool_find(client, pool_name)
    if pool_id is None:
        params = {
            "PoolName": pool_name,
            "Policies": {"PasswordPolicy": password_policy},
            "AdminCreateUserConfig": {
                "AllowAdminCreateUserOnly": not pool_config["self_signup_enabled"]
            },
            # needed so account recovery (forgot password) can send email codes
            "AutoVerifiedAttributes": ["email"],
        }
        if attrs_config:
            params["Schema"] = [schema_attr_build(a) for a in attrs_config]
        pool_id = client.create_user_pool(**params)["UserPool"]["Id"]
        print(f"user pool created: {pool_name} ({pool_id})")
        return pool_id

    print(f"user pool already exists: {pool_name} ({pool_id})")
    pool = client.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    password_policy_ensure(client, pool, password_policy)
    custom_attributes_ensure(client, pool, attrs_config)
    return pool_id


def public_signup_ensure(client, pool_id, pool_config):
    admin_only = not pool_config["self_signup_enabled"]
    pool = client.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    current = pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"]
    if current == admin_only:
        print(f"public signup: {'disabled' if admin_only else 'enabled'}, ok")
        return
    user_pool_update(
        client, pool, {"AdminCreateUserConfig": {"AllowAdminCreateUserOnly": admin_only}}
    )
    print(f"public signup: set to {'disabled' if admin_only else 'enabled'}")


# --------------------------------------------------------------------- domain


def domain_ensure(client, pool_id, domain_config):
    """ensure the aws-managed domain of the pool. returns the domain prefix."""
    if domain_config["type"] != "aws-managed":
        raise ValueError(f"unsupported domain type: {domain_config['type']}")
    prefix = domain_config["prefix"]
    current = client.describe_user_pool(UserPoolId=pool_id)["UserPool"].get("Domain")
    if current == prefix:
        print(f"domain already exists: {prefix}, ok")
        return prefix
    if current is not None:
        # a pool can have only one aws-managed domain, and the prefix of an
        # existing domain cannot be updated, so delete then recreate.
        client.delete_user_pool_domain(UserPoolId=pool_id, Domain=current)
        print(f"domain deleted: {current}")
    client.create_user_pool_domain(UserPoolId=pool_id, Domain=prefix)
    print(f"domain created: {prefix}")
    return prefix


def login_urls_build(region, domain_prefix, pool_config, client_ids):
    """hosted ui login url per app client, for manual login / first password setup."""
    login_urls = {}
    for client_name, client_id in client_ids.items():
        callback_urls = pool_config["app_clients"][client_name].get("callback_urls", [])
        if not callback_urls:
            continue
        login_urls[client_name] = (
            f"https://{domain_prefix}.auth.{region}.amazoncognito.com/login"
            f"?client_id={client_id}"
            f"&response_type=code"
            f"&redirect_uri={quote(callback_urls[0], safe='')}"
        )
    return login_urls


# ---------------------------------------------------------- resource servers


def resource_servers_ensure(client, pool_id, servers_config):
    for server_config in servers_config:
        identifier = server_config["identifier"]
        name = server_config.get("name", identifier)
        scopes = [
            {"ScopeName": s["name"], "ScopeDescription": s["description"]}
            for s in server_config.get("scopes", [])
        ]
        try:
            existing = client.describe_resource_server(
                UserPoolId=pool_id, Identifier=identifier
            )["ResourceServer"]
        except client.exceptions.ResourceNotFoundException:
            client.create_resource_server(
                UserPoolId=pool_id, Identifier=identifier, Name=name, Scopes=scopes
            )
            print(f"resource server created: {identifier}")
            continue

        scopes_current = {
            (s["ScopeName"], s["ScopeDescription"]) for s in existing.get("Scopes", [])
        }
        scopes_desired = {(s["ScopeName"], s["ScopeDescription"]) for s in scopes}
        if existing["Name"] == name and scopes_current == scopes_desired:
            print(f"resource server already exists: {identifier}, ok")
            continue
        client.update_resource_server(
            UserPoolId=pool_id, Identifier=identifier, Name=name, Scopes=scopes
        )
        print(f"resource server updated: {identifier}")


# ---------------------------------------------------------------- app clients


def app_client_find(client, pool_id, client_name):
    paginator = client.get_paginator("list_user_pool_clients")
    for page in paginator.paginate(UserPoolId=pool_id, MaxResults=60):
        for item in page["UserPoolClients"]:
            if item["ClientName"] == client_name:
                return item["ClientId"]
    return None


def app_client_desired_build(client_name, client_config):
    oauth = client_config.get("oauth", {})
    return {
        "ClientName": client_name,
        "SupportedIdentityProviders": ["COGNITO"],
        "CallbackURLs": client_config.get("callback_urls", []),
        "LogoutURLs": client_config.get("logout_urls", []),
        "AllowedOAuthFlowsUserPoolClient": True,
        "AllowedOAuthFlows": [OAUTH_FLOW_MAP[f] for f in oauth.get("flows", [])],
        "AllowedOAuthScopes": oauth.get("scopes", []),
    }


def app_clients_ensure(client, pool_id, clients_config):
    client_ids = {}
    for client_name, client_config in clients_config.items():
        desired = app_client_desired_build(client_name, client_config)
        client_id = app_client_find(client, pool_id, client_name)

        if client_id is None:
            resp = client.create_user_pool_client(
                UserPoolId=pool_id,
                GenerateSecret=client_config.get("generate_secret", False),
                **desired,
            )
            client_id = resp["UserPoolClient"]["ClientId"]
            print(f"app client created: {client_name} ({client_id})")
            client_ids[client_name] = client_id
            continue

        print(f"app client already exists: {client_name} ({client_id})")
        existing = client.describe_user_pool_client(
            UserPoolId=pool_id, ClientId=client_id
        )["UserPoolClient"]

        has_secret = "ClientSecret" in existing
        if has_secret != client_config.get("generate_secret", False):
            print("  warn: generate_secret differs, cannot be changed after creation")

        diff_keys = []
        for key, desired_value in desired.items():
            if isinstance(desired_value, list):
                current_value = existing.get(key, [])
                if sorted(current_value) != sorted(desired_value):
                    diff_keys.append(key)
            elif existing.get(key) != desired_value:
                diff_keys.append(key)

        if not diff_keys:
            print("  config ok")
        else:
            # update_user_pool_client() resets unspecified fields to default,
            # so send back existing values for the fields we do not manage.
            params = {"UserPoolId": pool_id, "ClientId": client_id}
            for key in APP_CLIENT_UPDATE_KEYS:
                if key in existing:
                    params[key] = existing[key]
            params.update(desired)
            client.update_user_pool_client(**params)
            print(f"  updated: {', '.join(diff_keys)}")

        client_ids[client_name] = client_id
    return client_ids


# --------------------------------------------------------------------- groups


def groups_ensure(client, pool_id, groups_config):
    names_existing = set()
    paginator = client.get_paginator("list_groups")
    for page in paginator.paginate(UserPoolId=pool_id):
        for group in page["Groups"]:
            names_existing.add(group["GroupName"])
    for group_name in groups_config:
        if group_name in names_existing:
            print(f"group already exists: {group_name}")
        else:
            client.create_group(UserPoolId=pool_id, GroupName=group_name)
            print(f"group created: {group_name}")


# ---------------------------------------------------------------------- users


def user_attrs_desired_build(user_config):
    attrs = {
        "email": user_config["email"],
        "email_verified": "true",
    }
    for name, value in (user_config.get("custom_attributes") or {}).items():
        attrs[f"custom:{name}"] = str(value)
    return attrs


def users_ensure(client, pool_id, users_config):
    for user_config in users_config:
        username = user_config["username"]
        attrs_desired = user_attrs_desired_build(user_config)

        try:
            existing = client.admin_get_user(UserPoolId=pool_id, Username=username)
        except client.exceptions.UserNotFoundException:
            client.admin_create_user(
                UserPoolId=pool_id,
                Username=username,
                UserAttributes=[
                    {"Name": name, "Value": value} for name, value in attrs_desired.items()
                ],
                DesiredDeliveryMediums=["EMAIL"],
            )
            print(f"user created: {username} (invitation email sent to {user_config['email']})")
            continue

        print(f"user already exists: {username}")
        attrs_current = {a["Name"]: a["Value"] for a in existing["UserAttributes"]}
        attrs_changed = {
            name: value
            for name, value in attrs_desired.items()
            if attrs_current.get(name) != value
        }
        if attrs_changed:
            client.admin_update_user_attributes(
                UserPoolId=pool_id,
                Username=username,
                UserAttributes=[
                    {"Name": name, "Value": value} for name, value in attrs_changed.items()
                ],
            )
            print(f"  attributes updated: {', '.join(attrs_changed)}")
        else:
            print("  attributes ok")


def group_memberships_ensure(client, pool_id, users_config):
    for user_config in users_config:
        username = user_config["username"]
        groups_desired = set(user_config.get("groups") or [])

        groups_current = set()
        paginator = client.get_paginator("admin_list_groups_for_user")
        for page in paginator.paginate(UserPoolId=pool_id, Username=username):
            for group in page["Groups"]:
                groups_current.add(group["GroupName"])

        if groups_desired == groups_current:
            print(f"user {username} groups ok: {', '.join(sorted(groups_desired)) or '(none)'}")
            continue
        for group_name in sorted(groups_desired - groups_current):
            client.admin_add_user_to_group(
                UserPoolId=pool_id, Username=username, GroupName=group_name
            )
            print(f"user {username} added to group: {group_name}")
        for group_name in sorted(groups_current - groups_desired):
            client.admin_remove_user_from_group(
                UserPoolId=pool_id, Username=username, GroupName=group_name
            )
            print(f"user {username} removed from group: {group_name}")


# ----------------------------------------------------------------------- main


def main():
    config = config_load()
    pool_config = config["cognito"]["user_pool"]
    client = cognito_client_make(config)

    pool_id = user_pool_ensure(client, pool_config)
    resource_servers_ensure(client, pool_id, pool_config.get("resource_servers", []))
    client_ids = app_clients_ensure(client, pool_id, pool_config.get("app_clients", {}))
    public_signup_ensure(client, pool_id, pool_config)
    groups_ensure(client, pool_id, pool_config.get("groups", []))
    users_ensure(client, pool_id, pool_config.get("users", []))
    group_memberships_ensure(client, pool_id, pool_config.get("users", []))

    domain_prefix = None
    login_urls = {}
    if "domain" in pool_config:
        domain_prefix = domain_ensure(client, pool_id, pool_config["domain"])
        login_urls = login_urls_build(
            config["aws"]["region_name"], domain_prefix, pool_config, client_ids
        )

    config_gen = config_gen_load()
    config_gen["cognito"] = {
        "user_pool_id": pool_id,
        "app_client_ids": client_ids,
        "domain_prefix": domain_prefix,
        "login_urls": login_urls,
    }
    config_gen_save(config_gen)
    print(f"saved to config_gen.yaml: pool id {pool_id}, client ids {client_ids}")
    for client_name, login_url in login_urls.items():
        print(f"login url ({client_name}): {login_url}")


if __name__ == "__main__":
    main()
