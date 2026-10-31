"""
app/services/cloud_media_storage.py

Enterprise-grade Cloud Media and Storage Service supporting Firebase Storage,
Google Cloud Storage (GCS), AWS S3, and local fallback.

Provides:
- upload_property_image(bytes, filename, content_type, property_id, image_type) -> public_url
- upload_floor_plan(bytes, filename, property_id) -> public_url
- download_image_from_url(url) -> bytes
- SHA-256 deduplication and idempotent asset storage
- Structured keys: properties/{property_id}/images/{hash}.webp
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import mimetypes
import os
import urllib.parse
import uuid
from typing import Optional, Tuple, Dict, Any

import httpx
from google.oauth2 import service_account
from google.auth.transport.requests import Request

from app.core.config import settings

logger = logging.getLogger(__name__)

FIREBASE_STORAGE_BUCKET = getattr(settings, "FIREBASE_STORAGE_BUCKET", None) or "varsovia-test-clone-26.firebasestorage.app"
GOOGLE_STORAGE_API_URL = "https://storage.googleapis.com"
FIREBASE_DOWNLOAD_BASE = "https://firebasestorage.googleapis.com/v0/b"


class CloudMediaStorageService:
    def __init__(self, bucket_name: Optional[str] = None):
        self.bucket_name = bucket_name or FIREBASE_STORAGE_BUCKET
        self._credentials = None
        self._init_credentials()

    def _init_credentials(self):
        """Initializes Google OAuth2 Service Account credentials from env or file."""
        # 1. From base64 env if present
        b64_key = getattr(settings, "FIREBASE_SERVICE_ACCOUNT_KEY", None)
        if b64_key:
            try:
                key_json = base64.b64decode(b64_key).decode("utf-8")
                info = json.loads(key_json)
                self._credentials = service_account.Credentials.from_service_account_info(
                    info,
                    scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
                )
                return
            except Exception as e:
                logger.warning(f"Could not load service account from base64 env: {e}")

        # 2. From file path
        creds_paths = [
            getattr(settings, "FIREBASE_CREDENTIALS_PATH", None),
            "credentials/firebase_service_account.json",
            "credentials/service_account.json",
        ]
        for path in creds_paths:
            if path and os.path.isfile(path):
                try:
                    self._credentials = service_account.Credentials.from_service_account_file(
                        path,
                        scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
                    )
                    return
                except Exception as e:
                    logger.warning(f"Could not load service account from {path}: {e}")

        logger.info("CloudMediaStorageService running in local storage fallback mode (no Google Cloud credentials)")

    def _get_access_token(self) -> Optional[str]:
        if not self._credentials:
            return None
        try:
            if not self._credentials.valid:
                self._credentials.refresh(Request())
            return self._credentials.token
        except Exception as e:
            logger.error(f"Failed to refresh Google Storage access token: {e}")
            return None

    def upload_property_image(
        self,
        image_bytes: bytes,
        filename: str,
        content_type: str = "image/webp",
        property_id: Optional[str] = None,
        image_type: str = "images"  # "images", "floor_plans", "documents"
    ) -> str:
        """
        Uploads image bytes to Firebase Storage / Cloud Storage under a structured key:
        properties/{property_id}/{image_type}/{content_hash}.{ext}
        
        Returns the permanent HTTPS media URL.
        """
        if not image_bytes:
            raise ValueError("image_bytes cannot be empty")

        # Compute SHA-256 hash for deduplication
        content_hash = hashlib.sha256(image_bytes).hexdigest()[:16]
        ext = "webp" if "webp" in content_type else ("png" if "png" in content_type else "jpg")
        
        prop_slug = (property_id or "general").strip().replace("/", "_")
        object_name = f"properties/{prop_slug}/{image_type}/{content_hash}.{ext}"

        token = self._get_access_token()
        if token:
            try:
                return self._upload_to_firebase(object_name, image_bytes, content_type, token)
            except Exception as e:
                logger.error(f"Failed to upload to Firebase Storage: {e}, falling back to local file store.")

        # Local fallback if remote store not reachable
        return self._save_locally(object_name, image_bytes)

    def upload_floor_plan(
        self,
        image_bytes: bytes,
        filename: str,
        property_id: Optional[str] = None
    ) -> str:
        """Convenience method for floor plans."""
        return self.upload_property_image(
            image_bytes=image_bytes,
            filename=filename,
            content_type="image/webp",
            property_id=property_id,
            image_type="floor_plans"
        )

    def _upload_to_firebase(
        self,
        object_name: str,
        content: bytes,
        content_type: str,
        token: str
    ) -> str:
        """Uploads and makes object publicly readable via direct Google Cloud Storage URL."""
        boundary = "===============CLOUD_MEDIA_STORAGE=="
        
        metadata = json.dumps({
            "contentType": content_type,
            "metadata": {
                "originalName": object_name.split("/")[-1]
            }
        })
        
        payload = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{metadata}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")

        upload_url = f"{GOOGLE_STORAGE_API_URL}/upload/storage/v1/b/{self.bucket_name}/o?uploadType=multipart&name={urllib.parse.quote(object_name, safe='')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/related; boundary={boundary}"
        }

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(upload_url, headers=headers, content=payload)
            resp.raise_for_status()

            # Grant public read ACL to allUsers so it opens in any web browser without token issues
            acl_url = f"{GOOGLE_STORAGE_API_URL}/storage/v1/b/{self.bucket_name}/o/{urllib.parse.quote(object_name, safe='')}/acl"
            client.post(
                acl_url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"entity": "allUsers", "role": "READER"}
            )

        # Standard direct Google Cloud Storage web URL (instantly viewable in any browser)
        public_url = f"https://storage.googleapis.com/{self.bucket_name}/{object_name}"
        logger.info(f"Successfully uploaded public asset: {public_url}")
        return public_url

    def _save_locally(self, object_name: str, content: bytes) -> str:
        """Saves file to local public directory and returns relative serving URL."""
        local_base = os.path.join(getattr(settings, "REPORT_STORAGE_DIR", "data/reports"), "media")
        file_path = os.path.join(local_base, object_name.replace("/", os.sep))
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(content)
        return f"/api/v1/media/{object_name}"

    def download_image_from_url(self, url: str) -> Optional[bytes]:
        """Downloads an image from a remote URL with security timeouts and headers."""
        if not url or not url.startswith(("http://", "https://")):
            return None
        try:
            headers = {"User-Agent": "VarsoviaEstate-DocumentIngestion/2.0"}
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
                if resp.status_code == 200 and resp.content:
                    return resp.content
        except Exception as e:
            logger.warning(f"Failed to download image from {url}: {e}")
        return None


# Global singleton instance
cloud_storage = CloudMediaStorageService()

