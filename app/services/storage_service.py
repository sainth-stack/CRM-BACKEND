import os
import shutil
from pathlib import Path
import logging

logger = logging.getLogger("Phase1V2")

class StorageService:
    def __init__(self, mode="local", upload_dir="uploads"):
        self.mode = os.getenv("STORAGE_MODE", mode)
        self.upload_dir = Path(upload_dir)
        
        if self.mode == "local":
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Storage initialized in LOCAL mode at {self.upload_dir.absolute()}")

    async def save_file(self, file_bytes: bytes, filename: str, campaign_id: int) -> str:
        """Saves file and returns the path/URL."""
        unique_name = f"campaign_{campaign_id}_{filename}"
        
        if self.mode == "local":
            file_path = self.upload_dir / unique_name
            with open(file_path, "wb") as buffer:
                buffer.write(file_bytes)
            return str(file_path.absolute())
        
        elif self.mode == "s3":
            # Placeholder for S3 implementation (boto3)
            # In production, you'd upload to a bucket here
            logger.warning("S3 Mode requested but not yet configured with credentials.")
            return f"s3://outreach-bucket/campaigns/{unique_name}"
            
        return ""

    def get_file_stream(self, file_path: str):
        """Returns a file-like object for reading."""
        if self.mode == "local":
            return open(file_path, "rb")
        # Add S3 stream logic here for production
        return None
