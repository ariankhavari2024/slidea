# app/storage.py
import os
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from flask import current_app

# It's better to initialize the client once and reuse it.
# The global scope is fine here as the configuration is loaded from environment variables
# which are available when the application starts.

S3_ENDPOINT   = os.environ.get("S3_ENDPOINT")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY")
S3_BUCKET     = os.environ.get("S3_BUCKET")
S3_USE_SSL    = os.environ.get("S3_USE_SSL", "false").lower() == "true"

s3 = None
if all([S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET]):
    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",  # Required by boto3, but not used by MinIO
        use_ssl=S3_USE_SSL,
    )
else:
    # This will log an error at startup if the configuration is incomplete
    logging.error("S3/MinIO environment variables are not fully configured. File storage will not work.")

def get_s3_client():
    """Returns the globally configured S3 client."""
    return s3

def get_s3_bucket_name():
    """Returns the configured S3 bucket name."""
    return S3_BUCKET

def ensure_bucket():
    """
    Checks if the MinIO bucket exists and creates it if it doesn't.
    This should be run once at application startup.
    """
    if not s3:
        current_app.logger.error("Cannot ensure bucket, S3 client is not configured.")
        return

    try:
        s3.head_bucket(Bucket=S3_BUCKET)
        current_app.logger.info(f"S3 Bucket '{S3_BUCKET}' already exists.")
    except ClientError as e:
        # If the bucket does not exist, a 404 error is returned
        if e.response['Error']['Code'] == '404':
            current_app.logger.info(f"S3 Bucket '{S3_BUCKET}' not found. Creating it...")
            s3.create_bucket(Bucket=S3_BUCKET)
            current_app.logger.info(f"S3 Bucket '{S3_BUCKET}' created successfully.")
        else:
            # For any other error, log it and re-raise
            current_app.logger.error(f"Error checking for S3 bucket '{S3_BUCKET}': {e}")
            raise

def put_bytes(key: str, data: bytes, content_type="image/png"):
    """Uploads a bytes object to the MinIO bucket."""
    if not s3:
        raise Exception("S3 client is not initialized. Check your environment variables.")
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)
    return key
