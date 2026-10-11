from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path, PurePosixPath


def upload_workspace(settings, execution_id: str, workspace: Path) -> str:
    """Persist a validated workspace so an operator can publish it after the worker exits."""
    if not settings.attachments_bucket:
        raise RuntimeError(
            "ATTACHMENTS_BUCKET is required to preserve the execution workspace."
        )
    archive_data = io.BytesIO()
    root = workspace.resolve(strict=True)
    with tarfile.open(fileobj=archive_data, mode="w:gz") as archive:
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in (*directories, *files):
                path = current_path / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    raise ValueError(
                        "Execution workspace contains a symlink and cannot be archived safely."
                    )
                if not path.is_dir() and not path.is_file():
                    raise ValueError("Execution workspace contains an unsupported file type.")
                archive.add(path, arcname=relative, recursive=False)
    object_name = f"execution-workspaces/{execution_id}/workspace.tar.gz"
    from google.cloud import storage

    storage.Client(project=settings.google_cloud_project).bucket(settings.attachments_bucket).blob(
        object_name
    ).upload_from_string(archive_data.getvalue(), content_type="application/gzip")
    return f"gs://{settings.attachments_bucket}/{object_name}"


def download_workspace(settings, reference: str, destination: Path) -> Path:
    if not reference.startswith("gs://"):
        raise ValueError("Invalid cloud workspace reference.")
    bucket_name, _, object_name = reference[5:].partition("/")
    if (
        bucket_name != settings.attachments_bucket
        or not object_name.startswith("execution-workspaces/")
    ):
        raise ValueError("Cloud workspace reference is outside the configured artifact bucket.")
    from google.cloud import storage

    content = storage.Client(project=settings.google_cloud_project).bucket(bucket_name).blob(
        object_name
    ).download_as_bytes()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = destination.resolve()
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or any(part in {"", ".", ".."} for part in name.parts):
                raise ValueError("Cloud workspace archive contains an unsafe path.")
            if member.isdev() or member.isfifo() or member.islnk():
                raise ValueError("Cloud workspace archive contains an unsupported file type.")
            target = (root / Path(*name.parts)).resolve()
            if target != root and root not in target.parents:
                raise ValueError("Cloud workspace archive contains a path outside its directory.")
            if member.issym():
                link_target = PurePosixPath(member.linkname)
                if link_target.is_absolute():
                    raise ValueError("Cloud workspace archive contains an unsafe symlink.")
                resolved = (target.parent / Path(*link_target.parts)).resolve()
                if resolved != root and root not in resolved.parents:
                    raise ValueError("Cloud workspace archive contains an unsafe symlink.")
        archive.extractall(root, members=members, filter="data")
    return destination


__all__ = ["download_workspace", "upload_workspace"]
