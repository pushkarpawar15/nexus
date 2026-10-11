from __future__ import annotations

import asyncio
import os
import re
from pathlib import PurePosixPath

from app.core.config import Settings


class AttachmentStorageError(RuntimeError):
    pass


class AttachmentStorage:
    """Private task attachment storage backed by local disk or a GCS bucket."""

    def __init__(self, settings: Settings) -> None:
        self.bucket_name = settings.attachments_bucket
        self.root = settings.attachments_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _key(self, task_id: str, attachment_id: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", task_id) or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,100}", attachment_id
        ):
            raise AttachmentStorageError("Invalid attachment storage key.")
        return f"tasks/{task_id}/{attachment_id}"

    async def put(self, task_id: str, attachment_id: str, content: bytes, content_type: str) -> str:
        key = self._key(task_id, attachment_id)
        await asyncio.to_thread(self._put, key, content, content_type)
        return key

    async def get(self, key: str) -> bytes:
        await asyncio.to_thread(self._validate_key, key)
        return await asyncio.to_thread(self._get, key)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._validate_key, key)
        await asyncio.to_thread(self._delete, key)

    def _validate_key(self, key: str) -> None:
        path = PurePosixPath(key)
        if (
            path.is_absolute()
            or path.as_posix() != key
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise AttachmentStorageError("Invalid attachment storage key.")

    def _put(self, key: str, content: bytes, content_type: str) -> None:
        if self.bucket_name:
            try:
                from google.cloud import storage

                storage.Client().bucket(self.bucket_name).blob(key).upload_from_string(
                    content, content_type=content_type
                )
                return
            except Exception as exc:
                raise AttachmentStorageError("Could not store task attachment.") from exc
        destination = self.root / key
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_bytes(content)
        os.chmod(temporary, 0o600)
        temporary.replace(destination)

    def _get(self, key: str) -> bytes:
        if self.bucket_name:
            try:
                from google.cloud import storage

                return storage.Client().bucket(self.bucket_name).blob(key).download_as_bytes()
            except Exception as exc:
                raise AttachmentStorageError("Could not load task attachment.") from exc
        try:
            return (self.root / key).read_bytes()
        except OSError as exc:
            raise AttachmentStorageError("Could not load task attachment.") from exc

    def _delete(self, key: str) -> None:
        if self.bucket_name:
            try:
                from google.cloud import storage

                storage.Client().bucket(self.bucket_name).blob(key).delete()
            except Exception as exc:
                raise AttachmentStorageError("Could not remove task attachment.") from exc
            return
        try:
            (self.root / key).unlink(missing_ok=True)
        except OSError as exc:
            raise AttachmentStorageError("Could not remove task attachment.") from exc


__all__ = ["AttachmentStorage", "AttachmentStorageError"]
