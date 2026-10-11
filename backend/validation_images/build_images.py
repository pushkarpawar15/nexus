"""Build offline validator images from a separate, allowlisted target checkout."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

MAX_MANIFEST_SIZE = 15 * 1024 * 1024
REQUIRED_MANIFESTS = (
    Path("backend/requirements.txt"),
    Path("backend/requirements-dev.txt"),
    Path("frontend/package.json"),
    Path("frontend/package-lock.json"),
)
SECRET_ASSIGNMENT = re.compile(
    r"(?im)\b(?:GITHUB_TOKEN|GOOGLE_API_KEY|GEMINI_API_KEY|AWS_SECRET_ACCESS_KEY|"
    r"(?:API|ACCESS|AUTH|PRIVATE)[_-]?(?:KEY|TOKEN|SECRET)|PASSWORD)\b"
    r"\s*[:=]\s*[\"']?([^\s\"'`,]{12,})"
)
KNOWN_CREDENTIAL = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})"
)
PLACEHOLDERS = ("${", "$", "your-", "example", "placeholder", "changeme", "process.env.")


class ManifestError(ValueError):
    """An unsafe or incomplete target manifest set."""


def collect_manifests(target_checkout: Path) -> dict[Path, bytes]:
    root = target_checkout.absolute()
    if not root.is_dir() or root.is_symlink():
        raise ManifestError("target checkout must be an existing, non-symlink directory")

    manifests: dict[Path, bytes] = {}
    for relative_path in REQUIRED_MANIFESTS:
        current = root
        for part in relative_path.parts:
            current = current / part
            if current.is_symlink():
                raise ManifestError(f"symlinks are not allowed: {relative_path}")
        if not current.is_file():
            raise ManifestError(f"required manifest is missing: {relative_path}")
        resolved = current.resolve(strict=True)
        try:
            resolved.relative_to(root.resolve(strict=True))
        except ValueError as exc:
            raise ManifestError(f"manifest escapes target checkout: {relative_path}") from exc
        if resolved.stat().st_size > MAX_MANIFEST_SIZE:
            raise ManifestError(f"manifest exceeds the 15 MiB limit: {relative_path}")

        contents = resolved.read_bytes()
        try:
            text = contents.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestError(f"manifest is not valid UTF-8: {relative_path}") from exc
        if KNOWN_CREDENTIAL.search(text):
            raise ManifestError(f"credential-like content found in {relative_path}")
        for match in SECRET_ASSIGNMENT.finditer(text):
            value = match.group(1).strip().strip("\"'")
            if not value.lower().startswith(PLACEHOLDERS):
                raise ManifestError(f"credential-like assignment found in {relative_path}")
        manifests[relative_path] = contents
    return manifests


def _write_context(context: Path, manifests: dict[Path, bytes], dockerfile: Path) -> None:
    for relative_path, contents in manifests.items():
        destination = context / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)
    shutil.copyfile(dockerfile, context / "Dockerfile")


def build_images(target_checkout: Path) -> None:
    manifests = collect_manifests(target_checkout)
    script_dir = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="nexus-validator-build-") as temporary:
        temporary_root = Path(temporary)
        python_context = temporary_root / "python"
        node_context = temporary_root / "node"
        python_context.mkdir()
        node_context.mkdir()
        _write_context(python_context, manifests, script_dir / "Dockerfile.python")
        _write_context(node_context, manifests, script_dir / "Dockerfile.node")

        subprocess.run(
            [
                "docker",
                "build",
                "--tag",
                "nexus-payflow-python-validator:local",
                str(python_context),
            ],
            check=True,
        )
        subprocess.run(
            [
                "docker",
                "build",
                "--tag",
                "nexus-payflow-node-validator:local",
                str(node_context),
            ],
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target_checkout", type=Path)
    args = parser.parse_args()
    build_images(args.target_checkout)


if __name__ == "__main__":
    main()
