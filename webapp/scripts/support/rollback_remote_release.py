#!/usr/bin/env python3
"""Verify and atomically reactivate an existing remote data release."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath


def unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def safe_relative(value: str) -> Path:
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts or not parsed.parts:
        raise ValueError("unsafe manifest path")
    return Path(*parsed.parts)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(release: Path, channel: str) -> None:
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("channel") != channel:
        raise ValueError("release channel mismatch")
    expected = set()
    for item in manifest.get("files", []):
        relative = safe_relative(str(item["path"]))
        target = release / relative
        if (
            not target.is_file()
            or target.stat().st_size != item["size"]
            or sha256(target) != item["sha256"]
        ):
            raise ValueError(f"release verification failed: {item['path']}")
        expected.add(relative.as_posix())
    checksum_paths = set()
    for line in (release / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        try:
            digest, name = line.split("  ", 1)
        except ValueError as error:
            raise ValueError("invalid SHA256SUMS entry") from error
        relative = safe_relative(name)
        target = release / relative
        if not target.is_file() or sha256(target) != digest:
            raise ValueError(f"SHA256SUMS mismatch: {name}")
        checksum_paths.add(relative.as_posix())
    if checksum_paths != expected | {"manifest.json"}:
        raise ValueError("SHA256SUMS does not cover the exact release file set")
    actual = {
        path.relative_to(release).as_posix()
        for path in release.rglob("*")
        if path.is_file() and path.name not in {"manifest.json", "SHA256SUMS"}
    }
    if actual != expected:
        raise ValueError("release file set mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel-root", type=Path, required=True)
    parser.add_argument("--channel", choices=("structured", "analysis"), required=True)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args()
    if not args.release_id.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise SystemExit("unsafe release ID")
    release = args.channel_root / "releases" / args.release_id
    verify(release, args.channel)
    temporary = args.channel_root / f".current.{os.getpid()}.tmp"
    unlink_if_exists(temporary)
    temporary.symlink_to(release)
    os.replace(temporary, args.channel_root / "current")
    print(args.release_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
