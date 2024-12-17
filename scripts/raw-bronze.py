import os
import sys
import json
from datetime import datetime
from sqlite3 import sqlite_version
from urllib.parse import urlparse
import boto3
from io import StringIO
from pyspark.sql import SparkSession
from pyspark.sql.functions import input_file_name, current_timestamp
from pyspark.sql import functions as F

print("Modules are loaded")


class IncrementalFileProcessor:
    def __init__(self, path, checkpoint_path, minio_config=None):
        self.path = path
        self.checkpoint_path = checkpoint_path
        self.parsed_url = urlparse(self.path)
        self.checkpoint_parsed_url = urlparse(self.checkpoint_path)
        self.client = self._get_client(minio_config)
        self.last_checkpoint_time = self._load_checkpoint()

    def _get_client(self, minio_config):
        if self.parsed_url.scheme in ['s3', 's3a'] or self.checkpoint_parsed_url.scheme in ['s3', 's3a']:
            if minio_config:
                return boto3.client('s3',
                                    endpoint_url=minio_config['endpoint_url'],
                                    aws_access_key_id=minio_config['access_key'],
                                    aws_secret_access_key=minio_config['secret_key'])
            else:
                return boto3.client('s3')
        return None

    def _load_checkpoint(self):
        if self.checkpoint_parsed_url.scheme in ['s3', 's3a']:
            try:
                bucket, key = self._parse_s3_path(self.checkpoint_path)
                response = self.client.get_object(Bucket=bucket, Key=key)
                return json.load(response['Body']).get('last_processed_time', 0)
            except self.client.exceptions.NoSuchKey:
                print(f"Checkpoint file not found: {self.checkpoint_path}")
                return 0
            except Exception as e:
                print(f"Error loading checkpoint: {str(e)}")
                return 0
        else:
            if os.path.exists(self.checkpoint_path):
                with open(self.checkpoint_path, 'r') as f:
                    return json.load(f).get('last_processed_time', 0)
            print(f"Checkpoint file not found: {self.checkpoint_path}")
            return 0

    def _parse_s3_path(self, s3_path):
        parsed = urlparse(s3_path)
        return parsed.netloc, parsed.path.lstrip('/')

    def _list_s3_files(self):
        bucket, prefix = self._parse_s3_path(self.path)
        files = []
        paginator = self.client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                if obj['LastModified'].timestamp() > self.last_checkpoint_time:
                    files.append(f"s3://{bucket}/{obj['Key']}")
        return files

    def _list_local_files(self):
        files = []
        directory = self.parsed_url.path
        for root, _, filenames in os.walk(directory):
            for filename in filenames:
                file_path = os.path.join(root, filename)
                if os.path.getmtime(file_path) > self.last_checkpoint_time:
                    files.append(file_path)
        return files

    def get_new_files(self):
        if self.parsed_url.scheme in ['s3', 's3a']:
            return self._list_s3_files()
        elif self.parsed_url.scheme == 'file' or not self.parsed_url.scheme:
            return self._list_local_files()
        else:
            raise ValueError(f"Unsupported scheme: {self.parsed_url.scheme}")

    def commit_checkpoint(self):
        current_time = datetime.now().timestamp()
        checkpoint_data = json.dumps({'last_processed_time': current_time})

        if self.checkpoint_parsed_url.scheme in ['s3', 's3a']:
            bucket, key = self._parse_s3_path(self.checkpoint_path)
            self.client.put_object(Bucket=bucket, Key=key, Body=checkpoint_data)
        else:
            os.makedirs(os.path.dirname(self.checkpoint_path), exist_ok=True)
            with open(self.checkpoint_path, 'w') as f:
                f.write(checkpoint_data)

        print(f"Checkpoint updated to: {datetime.fromtimestamp(current_time)}")


def create_spark_session(catalog_name, namespace, s3_tabel_bucket_arn, region="us-east-1"):
    spark = SparkSession.builder \
        .appName("iceberg_lab") \
        .config("spark.jars.packages","org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.7.0,software.amazon.s3tables:s3-tables-catalog-for-iceberg-runtime:0.1.3,software.amazon.awssdk:glue:2.20.143,software.amazon.awssdk:sts:2.20.143,software.amazon.awssdk:s3:2.20.143,software.amazon.awssdk:dynamodb:2.20.143,software.amazon.awssdk:kms:2.20.143") \
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


def process_files_with_spark(spark, files):
    if not files:
        print("No files to process")
        return None

    try:
        # Read new files into a Spark DataFrame
        df = spark.read.csv(files, sep='\t', header=True, inferSchema=True)
        return df
    except Exception as e:
        print(f"Error processing files with Spark: {str(e)}")
        return None


def load_data_to_iceberg(spark,
                         df,
                         catalog_name,
                         database_name,
                         table_name,
                         partition_cols=None,
                         table_type='COW',
                         sql_query=None,
                         compression='zst'):
    full_table_name = f"{catalog_name}.{database_name}.{table_name}"
    try:
        if sql_query is not None:
            print("IN sql_query")

            # Create a temporary view
            df.createOrReplaceTempView("temp_view")
            print("Created temp view ")
            transformed_df = spark.sql(sql_query)

            print("******transformed_df SCHEMA*********")
            transformed_df.printSchema()

        else:
            transformed_df = df

        writer = transformed_df.write.format("iceberg")

        # Set table properties based on table type
        if table_type.upper() == 'COW':
            writer = writer.option("write.format.default", "parquet")
            writer = writer.option("write.delete.mode", "copy-on-write")
            writer = writer.option("write.update.mode", "copy-on-write")
            writer = writer.option("write.merge.mode", "copy-on-write")
        elif table_type.upper() == 'MOR':
            writer = writer.option("write.format.default", "parquet")
            writer = writer.option("write.delete.mode", "merge-on-read")
            writer = writer.option("write.update.mode", "merge-on-read")
            writer = writer.option("write.merge.mode", "merge-on-read")
        else:
            raise ValueError("Invalid table_type. Must be 'COW' or 'MOR'.")

        # Set compression codec
        writer = writer.option("write.parquet.compression-codec", compression)

        if partition_cols:
            writer = writer.partitionBy(partition_cols)

        if spark.catalog.tableExists(full_table_name):
            print(f"Appending data to existing table {full_table_name}")
            writer.mode("append").saveAsTable(full_table_name)
        else:
            print(f"Creating new table {full_table_name}")
            writer.mode("overwrite").saveAsTable(full_table_name)

        print(f"Data successfully written to {full_table_name}")

        if sql_query:
            spark.catalog.dropTempView("temp_view")

        return True

    except Exception as e:
        print(f"Error loading data to Iceberg: {str(e)}")
        return False  # Return False on failure


if __name__ == "__main__":
    # ---------------------------------------------------------------

    catalog_name = "s3tablesbucket"
    namespace = "example_namespace"
    table_name = "bronze_orders"
    bucket_name = "<bucket_name>"
    s3_tabel_bucket_arn = "<ARN>"
    table_type = 'COW'
    partition_cols = "processed_date"
    compression = "snappy"
    sql_query = """
SELECT 
    *,
    input_file_name() as input_file,
    current_timestamp as processed_time,
    DATE_FORMAT(current_timestamp, 'yyyy-MM-dd') as processed_date
FROM 
    temp_view
"""
    input_path = f"s3://{bucket_name}/raw/"
    checkpoint_path = f"s3://{bucket_name}/checkpoints/raw_checkpoint.json"
    # ---------------------------------------------------------------

    spark = create_spark_session(catalog_name=catalog_name, namespace=namespace,
                                 s3_tabel_bucket_arn=s3_tabel_bucket_arn)
    print(f"Input path: {input_path}")
    print(f"Checkpoint path: {checkpoint_path}")

    try:
        processor = IncrementalFileProcessor(input_path, checkpoint_path)

        # Get incremental files
        new_files = processor.get_new_files()
        print(f"New files found: {new_files}")

        if new_files:
            df = process_files_with_spark(spark, new_files)

            if df is not None:
                success = load_data_to_iceberg(
                    spark,
                    df,
                    catalog_name,
                    namespace,
                    table_name,
                    partition_cols=partition_cols,
                    table_type=table_type,
                    sql_query=sql_query,
                    compression=compression
                )

                # Commit the checkpoint only if loading to Iceberg was successful
                if success:
                    processor.commit_checkpoint()
                else:
                    print("Data loading to Iceberg failed; checkpoint not committed.")
            else:
                print("No data to process after reading files.")
        else:
            print("No new files to process.")

    except Exception as e:
        print(f"An error occurred: {str(e)}")

    finally:
        # Stop the Spark session
        spark.stop()
