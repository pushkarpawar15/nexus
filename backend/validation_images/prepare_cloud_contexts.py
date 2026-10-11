from __future__ import annotations

import argparse
from pathlib import Path

from build_images import _write_context, collect_manifests


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target_checkout", type=Path)
    parser.add_argument("output_root", type=Path)
    args = parser.parse_args()

    manifests = collect_manifests(args.target_checkout)
    image_dir = Path(__file__).resolve().parent
    _write_context(
        args.output_root / "python",
        manifests,
        image_dir / "Dockerfile.python",
    )
    _write_context(
        args.output_root / "node",
        manifests,
        image_dir / "Dockerfile.node",
    )


if __name__ == "__main__":
    main()
