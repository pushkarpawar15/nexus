from pathlib import Path

import pytest

from validation_images.build_images import (
    MAX_MANIFEST_SIZE,
    REQUIRED_MANIFESTS,
    ManifestError,
    _write_context,
    collect_manifests,
)


def write_manifests(root: Path) -> None:
    for relative_path in REQUIRED_MANIFESTS:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")


def test_collects_only_the_four_expected_manifests(tmp_path: Path) -> None:
    write_manifests(tmp_path)
    (tmp_path / "README.md").write_text("ignored", encoding="utf-8")

    manifests = collect_manifests(tmp_path)

    assert tuple(manifests) == REQUIRED_MANIFESTS


def test_build_context_contains_only_allowlisted_files_and_dockerfile(
    tmp_path: Path,
) -> None:
    write_manifests(tmp_path)
    manifests = collect_manifests(tmp_path)
    context = tmp_path / "context"
    dockerfile = tmp_path / "Dockerfile.source"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")

    _write_context(context, manifests, dockerfile)

    files = {path.relative_to(context) for path in context.rglob("*") if path.is_file()}
    assert files == {*REQUIRED_MANIFESTS, Path("Dockerfile")}


def test_rejects_manifest_symlinks(tmp_path: Path) -> None:
    write_manifests(tmp_path)
    manifest = tmp_path / REQUIRED_MANIFESTS[0]
    original = manifest.read_text(encoding="utf-8")
    manifest.unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text(original, encoding="utf-8")
    manifest.symlink_to(outside)

    with pytest.raises(ManifestError, match="symlinks"):
        collect_manifests(tmp_path)


def test_rejects_oversized_manifests(tmp_path: Path) -> None:
    write_manifests(tmp_path)
    (tmp_path / REQUIRED_MANIFESTS[0]).write_bytes(b"x" * (MAX_MANIFEST_SIZE + 1))

    with pytest.raises(ManifestError, match="15 MiB"):
        collect_manifests(tmp_path)


def test_rejects_credential_like_manifest_content(tmp_path: Path) -> None:
    write_manifests(tmp_path)
    (tmp_path / REQUIRED_MANIFESTS[0]).write_text(
        "GITHUB_TOKEN=ghp_" + "a" * 30,
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="credential-like"):
        collect_manifests(tmp_path)
