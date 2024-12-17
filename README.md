# README.md for AWS S3 Table Management with PySpark

Medallion Architecture using Apache Iceberg-table-buckets

![image](https://github.com/user-attachments/assets/593fcb29-8a6e-4a91-b5ae-dfe45842b089)

![image](https://github.com/user-attachments/assets/b1e6228d-cfc0-4d6d-9782-c5742ccbc5c4)

This README provides a comprehensive guide for setting up and managing AWS S3 Tables using PySpark. It includes steps for configuring your environment, creating S3 table buckets, copying PySpark jobs and data to S3, running data ingestion pipelines, and cleaning up resources.


## Environment Setup

### Step 1: Set Java Home and Update AWS CLI

Before starting, ensure you have Java and AWS CLI installed. Run the following commands to set the Java Home and update the AWS CLI:
```
export JAVA_HOME=/opt/homebrew/opt/openjdk@11
brew upgrade awscli
pip3 install pyspark==3.4.0
```
### Create EMR Cluster on Ec2
```agsl
cd /Users/soumilshah/IdeaProjects/emr-labs/e6data/emr-cluster
chmod +x /Users/soumilshah/IdeaProjects/emr-labs/e6data/emr-cluster/create-emr-cluster.sh
./create-emr-cluster.sh

---------
COPY cluster ID from output
```



## Creating an S3 Table Bucket

### Step 2: Create an S3 Table Bucket and Get ARN

To create a table bucket for managing your data tables, execute:
```
aws s3tables create-table-bucket --region us-east-1 --name iceberg-awsmanaged-tables
aws s3tables list-table-buckets | jq '.tableBuckets[].arn'
```



## Copying PySpark Jobs to S3

### Step 3: Upload PySpark Scripts

Copy your PySpark job scripts to the S3 bucket:
```
# COPY JOB raw-bronze.py and bronze-silver.py to S3

aws s3 cp /Users/soumilshah/IdeaProjects/emr-labs/e6data/scripts/raw-bronze.py s3://soumil-dev-bucket-1995/jobs/raw-bronze.py 
aws s3 cp /Users/soumilshah/IdeaProjects/emr-labs/e6data/scripts/bronze-silver.py s3://soumil-dev-bucket-1995/jobs/bronze-silver.py

```


## Uploading Data to S3 Buckets

### Step 4: Copy Data Files to Raw Folders in S3 Buckets

Upload your initial data files to the designated S3 bucket:

```
aws s3 cp /Users/soumilshah/IdeaProjects/emr-labs/e6data/raw/datafiles/initialsinserts/e37d8fae-51e2-4716-bfb1-7381d7e58bcf.csv  s3://soumil-dev-bucket-1995/raw/e37d8fae-51e2-4716-bfb1-7381d7e58bcf.csv 
```
# step 5 Create Step function for Orchestrate pipeline 

```bash

{
  "Comment": "Medallion Pipeline with S3 tables",
  "StartAt": "RawBronzeStep",
  "States": {
    "RawBronzeStep": {
      "Type": "Task",
      "Resource": "arn:aws:states:::elasticmapreduce:addStep.sync",
      "Parameters": {
        "ClusterId.$": "$.clusterId",
        "Step": {
          "Name.$": "$.stepName",
          "ActionOnFailure": "CONTINUE",
          "HadoopJarStep": {
            "Jar": "command-runner.jar",
            "Args.$": "$.jobs.RawBronze"
          }
        }
      },
      "ResultPath": null,
      "Next": "PreserveInput",
      "Catch": [
        {
          "ErrorEquals": [
            "States.TaskFailed"
          ],
          "Next": "FailState"
        }
      ]
    },
    "PreserveInput": {
      "Type": "Pass",
      "ResultPath": "$.originalInput",
      "Next": "BronzeToSilverStep"
    },
    "BronzeToSilverStep": {
      "Type": "Task",
      "Resource": "arn:aws:states:::elasticmapreduce:addStep.sync",
      "Parameters": {
        "ClusterId.$": "$.originalInput.clusterId",
        "Step": {
          "Name.$": "$.originalInput.stepName",
          "ActionOnFailure": "CONTINUE",
          "HadoopJarStep": {
            "Jar": "command-runner.jar",
            "Args.$": "$.originalInput.jobs.BronzeToSilver"
          }
        }
      },
      "ResultPath": null,
      "Next": "SuccessState",
      "Catch": [
        {
          "ErrorEquals": [
            "States.TaskFailed"
          ],
          "Next": "FailState"
        }
      ]
    },
    "SuccessState": {
      "Type": "Pass",
      "Result": "Job completed successfully!",
      "End": true
    },
    "FailState": {
      "Type": "Fail",
      "Error": "JobFailed",
      "Cause": "The EMR job failed to complete."
    }
  }
}
```


# Step 4: Run the Pipeline this will ingest data from raw-bronze(append only)-silver(deduped)
```bash

{
  "clusterId": "XX",
  "stepName": "BronzeToSilverStep",
  "jobs": {
    "RawBronze": [
      "spark-submit",
      "--packages",
      "software.amazon.s3tables:s3-tables-catalog-for-iceberg-runtime:0.1.3,org.apache.iceberg:iceberg-spark-runtime-3.4_2.12:1.6.0",
      "s3://XXXX/jobs/raw-bronze.py"
    ],
    "BronzeToSilver": [
      "spark-submit",
      "--packages",
      "software.amazon.s3tables:s3-tables-catalog-for-iceberg-runtime:0.1.3,org.apache.iceberg:iceberg-spark-runtime-3.4_2.12:1.6.0",
      "s3://XXX/jobs/bronze-silver.py"
    ]
  }
}
```


# Step 5: Push Updates or new files coming in

```
aws s3 cp /Users/soumilshah/IdeaProjects/emr-labs/e6data/raw/datafiles/updates/6a45a9d1-9ef4-4df1-99d6-2d1add3b2e94.csv  s3://soumil-dev-bucket-1995/raw/6a45a9d1-9ef4-4df1-99d6-2d1add3b2e94.csv
```

# Step 6: Run the Pipeline this will ingest data from raw-bronze(append only)-silver(deduped)
```bash

{
  "clusterId": "XX",
  "stepName": "BronzeToSilverStep",
  "jobs": {
    "RawBronze": [
      "spark-submit",
      "--packages",
      "software.amazon.s3tables:s3-tables-catalog-for-iceberg-runtime:0.1.3,org.apache.iceberg:iceberg-spark-runtime-3.4_2.12:1.6.0",
      "s3://XXXX/jobs/raw-bronze.py"
    ],
    "BronzeToSilver": [
      "spark-submit",
      "--packages",
      "software.amazon.s3tables:s3-tables-catalog-for-iceberg-runtime:0.1.3,org.apache.iceberg:iceberg-spark-runtime-3.4_2.12:1.6.0",
      "s3://XXX/jobs/bronze-silver.py"
    ]
  }
}
```
# Step 7: Local Query 
```
rom pyspark.sql import SparkSession

# Define variables
WAREHOUSE_PATH = "XXXX"

# Initialize SparkSession with required configurations
spark = SparkSession.builder \
    .appName("iceberg_lab") \
    .config("spark.jars.packages", "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.7.0,software.amazon.s3tables:s3-tables-catalog-for-iceberg-runtime:0.1.3,software.amazon.awssdk:glue:2.20.143,software.amazon.awssdk:sts:2.20.143,software.amazon.awssdk:s3:2.20.143,software.amazon.awssdk:dynamodb:2.20.143,software.amazon.awssdk:kms:2.20.143")  \
    .config("spark.sql.catalog.s3tablesbucket", "org.apache.iceberg.spark.SparkCatalog") \
    .config("spark.sql.catalog.s3tablesbucket.client.region", "us-east-1") \
    .config("spark.sql.catalog.defaultCatalog", "s3tablesbucket") \
    .config("spark.sql.catalog.s3tablesbucket.warehouse", WAREHOUSE_PATH) \
    .config("spark.sql.catalog.s3tablesbucket.catalog-impl","software.amazon.s3tables.iceberg.S3TablesCatalog") \
    .getOrCreate()



# Execute your SQL commands
spark.sql("SHOW NAMESPACES IN s3tablesbucket").show()
spark.sql("USE s3tablesbucket.example_namespace").show()
spark.sql("SHOW TABLES").show()

spark.sql("SELECT count(*) FROM bronze_orders ").show()
spark.sql("SELECT count(*) FROM silver_orders ").show()


spark.sql("SELECT invoiceid,category FROM bronze_orders ").show()
spark.sql("SELECT invoiceid,category FROM silver_orders ").show()

```


# CLEANUP
```agsl
aws s3tables delete-table \
--table-bucket-arn arn:aws:s3tables:us-east-1:867098943567:bucket/iceberg-awsmanaged-tables \
--namespace example_namespace --name silver_orders


aws s3tables delete-table \
--table-bucket-arn arn:aws:s3tables:us-east-1:867098943567:bucket/iceberg-awsmanaged-tables \
--namespace example_namespace --name bronze_orders

```

Refernces
```
https://aws.amazon.com/s3/features/tables/

https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables.html

https://medium.com/@naren3883/deep-dive-into-new-amazon-s3-tables-4e1de56394eb

https://press.aboutamazon.com/2024/12/amazon-s3-expands-capabilities-with-managed-apache-iceberg-tables-for-faster-data-lake-analytics-and-automatic-metadata-generation-to-simplify-data-discovery-and-understanding
```

