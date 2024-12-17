try:

    import os, sys, json
    from datetime import datetime
    from urllib.parse import urlparse
    import boto3
    from io import StringIO
    from pyspark.sql import SparkSession
    from pyspark.sql.functions import input_file_name, current_timestamp
    from pyspark.sql import SparkSession
    from pyspark.sql.types import (
        StructType, StructField, StringType, IntegerType, LongType,
        FloatType, DoubleType, BooleanType, TimestampType, DateType
    )
    from pyspark.sql.avro.functions import from_avro, to_avro

    print("Modules are loaded")
except Exception as e:
    print("Error ")


def avro_schema_to_spark_schema(avro_schema_json):
    def convert_type(avro_type):
        type_mapping = {
            'string': StringType(),
            'int': IntegerType(),
            'long': LongType(),
            'float': FloatType(),
            'double': DoubleType(),
            'boolean': BooleanType(),
            'timestamp-micros': TimestampType(),
            'date': DateType()
        }
        if isinstance(avro_type, dict):
            if avro_type.get('logicalType') == 'timestamp-micros':
                return TimestampType()
            elif avro_type.get('logicalType') == 'date':
                return DateType()
        return type_mapping.get(avro_type, StringType())

    avro_schema = json.loads(avro_schema_json)
    fields = []

    for field in avro_schema['fields']:
        field_type = field['type']
        if isinstance(field_type, list):
            # Handle union types (nullable fields)
            non_null_type = next(t for t in field_type if t != 'null')
            spark_type = convert_type(non_null_type)
            nullable = 'null' in field_type
        else:
            spark_type = convert_type(field_type)
            nullable = False

        fields.append(StructField(field['name'], spark_type, nullable))

    return StructType(fields)


def create_spark_session(catalog_name, namespace, s3_tabel_bucket_arn, region="us-east-1"):
    spark = SparkSession.builder \
        .appName("iceberg_lab") \
        .config("spark.jars.packages",
                "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.7.0,software.amazon.s3tables:s3-tables-catalog-for-iceberg-runtime:0.1.3,software.amazon.awssdk:glue:2.20.143,software.amazon.awssdk:sts:2.20.143,software.amazon.awssdk:s3:2.20.143,software.amazon.awssdk:dynamodb:2.20.143,software.amazon.awssdk:kms:2.20.143") \
        .config(f"spark.sql.catalog.{catalog_name}", "org.apache.iceberg.spark.SparkCatalog") \
        .config(f"spark.sql.catalog.{catalog_name}.client.region", region) \
        .config("spark.sql.catalog.defaultCatalog", catalog_name) \
        .config(f"spark.sql.catalog.{catalog_name}.warehouse", s3_tabel_bucket_arn) \
        .config(f"spark.sql.catalog.{catalog_name}.catalog-impl", "software.amazon.s3tables.iceberg.S3TablesCatalog") \
        .getOrCreate()

    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {catalog_name}.{namespace}")
    print("Showing all namespaces")
    spark.sql(f"SHOW NAMESPACES IN {catalog_name}").show()
    return spark


def load_checkpoint(checkpoint_path):
    checkpoint_parsed_url = urlparse(checkpoint_path)

    if checkpoint_parsed_url.scheme in ['s3', 's3a']:
        # For S3, use boto3 to get the object and read the data
        bucket, key = checkpoint_parsed_url.netloc, checkpoint_parsed_url.path.lstrip('/')
        client = boto3.client('s3')
        try:
            response = client.get_object(Bucket=bucket, Key=key)
            checkpoint_data = response['Body'].read().decode('utf-8')
            return json.loads(checkpoint_data)
        except Exception as e:
            print(f"Error reading checkpoint from S3: {e}")
            return None
    else:
        # For local filesystem
        if os.path.exists(checkpoint_path):
            with open(checkpoint_path, 'r') as f:
                return json.load(f)
        return None


def save_checkpoint(checkpoint_path, snapshot_id):
    """
    Saves the checkpoint with the provided snapshot ID to the specified path.
    """
    checkpoint_data = json.dumps({'last_processed_snapshot': snapshot_id})
    checkpoint_parsed_url = urlparse(checkpoint_path)

    if checkpoint_parsed_url.scheme in ['s3', 's3a']:
        # No need to call urlparse again, just use checkpoint_parsed_url
        bucket, key = checkpoint_parsed_url.netloc, checkpoint_parsed_url.path.lstrip('/')
        client = boto3.client('s3')
        client.put_object(Bucket=bucket, Key=key, Body=checkpoint_data)
        print(f"Checkpoint saved to S3: {checkpoint_path}")


def get_incremental_data(spark, catalog_name, database_name, table_name, checkpoint_path):
    full_table_name = f"{catalog_name}.{database_name}.{table_name}"

    # Get table history
    history_df = spark.sql(f"SELECT * FROM {full_table_name}.history")
    print("***Snapshots history*****")
    history_df.show()
    history_df.createOrReplaceTempView("table_history")

    # Load checkpoint
    checkpoint = load_checkpoint(checkpoint_path)
    print(f"""
    --------------------------------
    CHECKPOINT LOAD {checkpoint}
    --------------------------------
    """)

    if checkpoint is None:
        # If no checkpoint, process all data
        print("No checkpoint found. Processing all data.")
        df = spark.read.format("iceberg").table(full_table_name)
        latest_snapshot = \
            spark.sql("SELECT snapshot_id FROM table_history ORDER BY made_current_at DESC LIMIT 1").collect()[0][0]
    else:
        # Get the latest snapshot ID
        latest_snapshot = \
            spark.sql("SELECT snapshot_id FROM table_history ORDER BY made_current_at DESC LIMIT 1").collect()[0][0]

        if latest_snapshot == checkpoint['last_processed_snapshot']:
            print("No new data to process.")
            return None, latest_snapshot

        # Process incremental data
        print(f"Processing data from snapshot {checkpoint['last_processed_snapshot']} to {latest_snapshot}")
        df = spark.read.format("iceberg") \
            .option("start-snapshot-id", checkpoint['last_processed_snapshot']) \
            .option("end-snapshot-id", latest_snapshot) \
            .table(full_table_name)

    return df, latest_snapshot


def write_iceberg(spark,
                  df,
                  catalog_name,
                  database_name,
                  table_name,
                  merge_query,
                  avro_schema_json,
                  partition_col=None,
                  table_type='COW'):
    full_table_name = f"{catalog_name}.{database_name}.{table_name}"

    # Check if the table exists
    table_exists = spark.catalog.tableExists(f"{catalog_name}.{database_name}.{table_name}")

    if not table_exists:
        print(f"Table {full_table_name} does not exist. Creating new table with provided schema.")
        # Convert Avro schema to Spark schema
        spark_schema = avro_schema_to_spark_schema(avro_schema_json)

        # Create an empty DataFrame with the Spark schema
        empty_df = spark.createDataFrame([], schema=spark_schema)

        # Prepare table properties
        table_properties = {}
        if table_type.upper() == 'COW':
            table_properties.update({
                'write.delete.mode': 'copy-on-write',
                'write.update.mode': 'copy-on-write',
                'write.merge.mode': 'copy-on-write'
            })
        elif table_type.upper() == 'MOR':
            table_properties.update({
                'write.delete.mode': 'merge-on-read',
                'write.update.mode': 'merge-on-read',
                'write.merge.mode': 'merge-on-read'
            })
        else:
            raise ValueError("Invalid table_type. Must be 'COW' or 'MOR'.")

        # Write the empty DataFrame to create the Iceberg table
        writer = empty_df.writeTo(full_table_name).using("iceberg").tableProperty("format-version", "2")

        for key, value in table_properties.items():
            writer = writer.tableProperty(key, value)

        if partition_col and partition_col in empty_df.columns:
            print(f"Partitioning table by column: {partition_col}")
            writer = writer.partitionedBy(partition_col)

        writer.create()
        print(f"Table {full_table_name} created successfully as {table_type} table.")

    # Create a temporary view of the input DataFrame
    df.createOrReplaceTempView("__temp_table")

    print(f"Performing MERGE INTO operation on {full_table_name}.")
    spark.sql(merge_query.format(full_table_name=full_table_name))

    # Drop the temporary view
    spark.catalog.dropTempView("__temp_table")
    print(f"Data merged into {full_table_name} successfully.")

    return True


if __name__ == "__main__":
    # -----------------------------------
    # Settings for Iceberg tables
    # -----------------------------------

    catalog_name = "s3tablesbucket"
    namespace = "example_namespace"
    s3_tabel_bucket_arn = "arn:aws:s3tables:<region>:<account_id>:bucket/<bucket_name>"
    bronze_table_name = "bronze_orders"
    silver_table_name = "silver_orders"
    bucket_name = "<your_bucket_name>"
    checkpoint_path = f"s3://{bucket_name}/checkpoints/silver_checkpoint.json"
    partition_col = "destinationstate"
    table_type = 'COW'  # COW
    avro_schema_json = '''
    {
      "type": "record",
      "name": "orders",
      "fields": [
        {"name": "replicadmstimestamp", "type": {"type": "long", "logicalType": "timestamp-micros"}},
        {"name": "invoiceid", "type": "long"},
        {"name": "itemid", "type": "long"},
        {"name": "category", "type": "string"},
        {"name": "price", "type": "double"},
        {"name": "quantity", "type": "int"},
        {"name": "orderdate", "type": {"type": "int", "logicalType": "date"}},
        {"name": "destinationstate", "type": "string"},
        {"name": "shippingtype", "type": "string"},
        {"name": "referral", "type": "string"}
      ]
    }
    '''
    merge_query = """
MERGE INTO {full_table_name} AS target
USING (
    SELECT *
    FROM (
        SELECT *, 
               ROW_NUMBER() OVER (
                   PARTITION BY invoiceid 
                   ORDER BY processed_time DESC
               ) AS row_num
        FROM __temp_table
    ) AS deduped_source
    WHERE row_num = 1
) AS source
ON target...
"""

    spark = create_spark_session(catalog_name=catalog_name, namespace=namespace,
                                 s3_tabel_bucket_arn=s3_tabel_bucket_arn)

    print("Spark Session is ready ")

    # Get incremental data
    df, latest_snapshot = get_incremental_data(spark,
                                               catalog_name,
                                               namespace,
                                               bronze_table_name,
                                               checkpoint_path)

    if df is not None:
        print("****************")
        # Process the data (replace this with your actual processing logic)
        df.show(truncate=False)

        # Write data to Iceberg table
        success = write_iceberg(spark,
                                df,
                                catalog_name,
                                namespace,
                                silver_table_name,
                                merge_query,
                                avro_schema_json,
                                partition_col,
                                table_type)

        if success:
            save_checkpoint(checkpoint_path, latest_snapshot)
            print(f"Checkpoint updated to snapshot {latest_snapshot}")
    else:
        print("No new data to process.")

    spark.stop()
