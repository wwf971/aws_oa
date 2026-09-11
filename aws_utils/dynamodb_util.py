# dynamodb table ensure/delete, shared by all sub-projects.
# 'ensure' means: create if missing, add missing gsi if the table exists,
# never recreate. all tables use on-demand billing (PAY_PER_REQUEST).


def table_ensure(dynamodb, table_name, attribute_definitions, key_schema, gsi_list=None):
    try:
        desc = dynamodb.describe_table(TableName=table_name)["Table"]
    except dynamodb.exceptions.ResourceNotFoundException:
        params = {
            "TableName": table_name,
            "AttributeDefinitions": attribute_definitions,
            "KeySchema": key_schema,
            "BillingMode": "PAY_PER_REQUEST",
        }
        if gsi_list:
            params["GlobalSecondaryIndexes"] = gsi_list
        dynamodb.create_table(**params)
        dynamodb.get_waiter("table_exists").wait(TableName=table_name)
        print(f"table created: {table_name}")
        return

    print(f"table already exists: {table_name}")
    gsi_names_existing = [gsi["IndexName"] for gsi in desc.get("GlobalSecondaryIndexes", [])]
    for gsi in gsi_list or []:
        if gsi["IndexName"] in gsi_names_existing:
            print(f"  gsi {gsi['IndexName']}: ok")
            continue
        dynamodb.update_table(
            TableName=table_name,
            AttributeDefinitions=attribute_definitions,
            GlobalSecondaryIndexUpdates=[{"Create": gsi}],
        )
        print(f"  gsi {gsi['IndexName']}: creating (can take a few minutes on aws side)")


def table_delete(dynamodb, table_name):
    try:
        dynamodb.delete_table(TableName=table_name)
        print(f"table deletion started: {table_name}")
    except dynamodb.exceptions.ResourceNotFoundException:
        print(f"table does not exist: {table_name}")
