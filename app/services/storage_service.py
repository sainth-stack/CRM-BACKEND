import os
from pathlib import Path
import logging
import io

try:
    import boto3
    from botocore.exceptions import ClientError
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False
    class ClientError(Exception):
        pass

logger = logging.getLogger("Phase1V2")

class StorageService:
    def __init__(self, mode="local", upload_dir="uploads"):
        self.mode = os.getenv("STORAGE_MODE", mode).lower()
        self.upload_dir = Path(upload_dir)
        
        if self.mode == "s3":
            if not BOTO3_AVAILABLE:
                logger.warning("boto3 package is not installed. Automatically falling back to LOCAL storage mode.")
                self.mode = "local"
            else:
                self.bucket_name = os.getenv("AWS_STORAGE_BUCKET_NAME")
                self.region_name = os.getenv("AWS_REGION", "us-east-1")
                
                # Initialize S3 Client using environment parameters
                self.s3_client = boto3.client(
                    "s3",
                    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                    region_name=self.region_name
                )
                logger.info(f"Storage initialized in S3 mode targeting bucket: {self.bucket_name}")

        if self.mode == "local":
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Storage initialized in LOCAL mode at {self.upload_dir.absolute()}")

    async def save_file(self, file_bytes: bytes, filename: str, campaign_id: str) -> str:
        """Saves file to designated storage and returns the path/URL."""
        unique_name = f"campaign_{campaign_id}_{filename}"
        
        if self.mode == "local":
            file_path = self.upload_dir / unique_name
            with open(file_path, "wb") as buffer:
                buffer.write(file_bytes)
            return str(file_path.absolute())
        
        elif self.mode == "s3":
            try:
                # Upload bytes directly to S3 bucket
                self.s3_client.put_object(
                    Bucket=self.bucket_name,
                    Key=f"campaigns/{unique_name}",
                    Body=file_bytes,
                    ContentType="text/csv"
                )
                logger.info(f"Successfully uploaded {unique_name} to S3 bucket {self.bucket_name}.")
                return f"https://{self.bucket_name}.s3.{self.region_name}.amazonaws.com/campaigns/{unique_name}"
            except ClientError as e:
                logger.error(f"S3 Upload Failure for {unique_name}: {e}")
                # Fallback to local storage on S3 failure to maintain server continuity
                fallback_path = self.upload_dir / unique_name
                self.upload_dir.mkdir(parents=True, exist_ok=True)
                with open(fallback_path, "wb") as buffer:
                    buffer.write(file_bytes)
                logger.warning(f"S3 Upload Failed. Fallback initiated to local storage: {fallback_path}")
                return str(fallback_path.absolute())
            
        return ""

    def get_file_stream(self, file_path: str):
        """Returns a file-like object for reading."""
        if self.mode == "local" or not file_path.startswith("http"):
            # If local path or fallback
            return open(file_path, "rb")
            
        if self.mode == "s3":
            try:
                # Parse the S3 key out of the HTTP S3 URL
                # Format: https://{bucket}.s3.{region}.amazonaws.com/{key}
                parts = file_path.split(".amazonaws.com/")
                if len(parts) < 2:
                    raise ValueError(f"Unable to parse key from S3 URL: {file_path}")
                key = parts[1]
                
                response = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)
                return io.BytesIO(response["Body"].read())
            except Exception as e:
                logger.error(f"Failed to stream file from S3: {e}")
                # Try local fallback if file exists locally
                fallback_filename = file_path.split("/")[-1]
                fallback_path = self.upload_dir / fallback_filename
                if fallback_path.exists():
                    return open(fallback_path, "rb")
                raise e
        return None
