# livekit-service/rox/utils/gcs_signer.py
# Utility to generate V4 signed URLs for GCS objects.
# Requires google-cloud-storage and service account credentials capable of signing
# (Workload Identity, service account key, or IAM signBlob privileges).

from typing import Optional
from datetime import timedelta
import logging
import os

try:
    from google.cloud import storage  # type: ignore
except Exception:  # pragma: no cover
    storage = None  # Allow import even if not installed at dev time

logger = logging.getLogger(__name__)


def generate_v4_signed_url(bucket_name: str, object_name: str, expiration_seconds: int = 600) -> Optional[str]:
    """
    Generate a V4 signed URL for a GCS object.

    Args:
        bucket_name: Name of the GCS bucket
        object_name: Path of the object within the bucket
        expiration_seconds: Link lifetime in seconds (default 10 minutes)

    Returns:
        The signed URL string, or None if signing failed or library unavailable.
    """
    if not bucket_name or not object_name:
        logger.error("generate_v4_signed_url called with empty bucket/object")
        return None

    if storage is None:
        logger.error("google-cloud-storage is not installed; cannot sign URLs")
        return None

    try:
        client = storage.Client()  # Uses ADC
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=int(max(60, expiration_seconds))),
            method="GET",
            response_disposition=f"inline; filename={os.path.basename(object_name)}",
            content_type="application/json",
        )
        return url
    except Exception as e:
        logger.error(f"Failed to generate signed URL for gs://{bucket_name}/{object_name}: {e}")
        return None
