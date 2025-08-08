# app/storage.py
import os
import boto3
from botocore.client import Config
from flask import current_app

# --- S3/MinIO Client Configuration ---
# It's better to initialize the client inside a function to ensure it runs
# within the application context and can access environment variables correctly.

s3_client = None

def get_s3_client():
    """Initializes and returns a boto3 S3 client configured for MinIO."""
    global s3_client
    if s3_client:
        return s3_client

    # These variables are set in your render.yaml
    S3_ENDPOINT = os.environ.get("S3_ENDPOINT")
    S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY")
    S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY")
    S3_USE_SSL = os.environ.get("S3_USE_SSL", "false").lower() == "true"

    if not all([S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY]):
        current_app.logger.error("S3 client configuration is missing environment variables.")
        return None

    s3_client = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",  # Required by boto3, but not used by MinIO
        use_ssl=S3_USE_SSL,
    )
    return s3_client

def get_s3_bucket_name():
    """Returns the configured S3 bucket name."""
    return os.environ.get("S3_BUCKET")

def ensure_bucket():
    """
    Checks if the MinIO bucket exists and creates it if it doesn't.
    This should be run once at application startup.
    """
    s3 = get_s3_client()
    bucket_name = get_s3_bucket_name()
    if not s3 or not bucket_name:
        current_app.logger.error("Cannot ensure bucket, S3 client or bucket name is not configured.")
        return

    try:
        s3.head_bucket(Bucket=bucket_name)
        current_app.logger.info(f"S3 Bucket '{bucket_name}' already exists.")
    except ClientError as e:
        if e.response['Error']['Code'] == '404':
            current_app.logger.info(f"S3 Bucket '{bucket_name}' not found. Creating it...")
            s3.create_bucket(Bucket=bucket_name)
            current_app.logger.info(f"S3 Bucket '{bucket_name}' created successfully.")
        else:
            current_app.logger.error(f"Error checking for S3 bucket: {e}")
            raise

def put_bytes(key: str, data: bytes, content_type="image/png"):
    """Uploads a bytes object to the MinIO bucket."""
    s3 = get_s3_client()
    bucket_name = get_s3_bucket_name()
    s3.put_object(Bucket=bucket_name, Key=key, Body=data, ContentType=content_type)
    return key
