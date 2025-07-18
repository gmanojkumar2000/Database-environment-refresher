import boto3
import logging

def fetch_ddl_from_s3(bucket, key, region):
    """
    Fetch DDL SQL from S3 bucket/key.
    Returns the SQL as a string.
    """
    logger = logging.getLogger(__name__)
    s3 = boto3.client('s3', region_name=region)
    try:
        logger.info(f"Fetching DDL from s3://{bucket}/{key}...")
        obj = s3.get_object(Bucket=bucket, Key=key)
        ddl_sql = obj['Body'].read().decode('utf-8')
        logger.info("DDL fetched from S3.")
        return ddl_sql
    except Exception as e:
        logger.error(f"Failed to fetch DDL from S3: {e}")
        raise 