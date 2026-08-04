#!/usr/bin/env python3
"""Verify and atomically activate one uploaded public release on Ali."""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath


def unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def normalize_public_permissions(root: Path) -> None:
    root.chmod(0o755)
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_relative(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise SystemExit(f"unsafe manifest path: {value}")
    return Path(*path.parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel-root", type=Path, required=True)
    parser.add_argument("--incoming", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--channel", choices=("structured", "analysis"), required=True)
    parser.add_argument("--keep", type=int, default=5)
    args = parser.parse_args()
    root = args.channel_root.resolve()
    incoming = args.incoming.resolve()
    expected_incoming_root = (root / "releases" / ".incoming").resolve()
    if expected_incoming_root not in incoming.parents or incoming.name != args.release_id:
        raise SystemExit("incoming directory is outside the configured channel root")
    manifest = json.loads((incoming / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("release_id") != args.release_id or manifest.get("channel") != args.channel:
        raise SystemExit("release ID or channel mismatch")
    expected = set()
    for item in manifest.get("files", []):
        relative = safe_relative(item["path"])
        target = incoming / relative
        if not target.is_file() or target.stat().st_size != item["size"] or digest(target) != item["sha256"]:
            raise SystemExit(f"manifest mismatch: {relative}")
        expected.add(relative.as_posix())
    checksum_paths = set()
    for line in (incoming / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        try:
            checksum, name = line.split("  ", 1)
        except ValueError as error:
            raise SystemExit("invalid SHA256SUMS entry") from error
        relative = safe_relative(name)
        target = incoming / relative
        if not target.is_file() or digest(target) != checksum:
            raise SystemExit(f"SHA256SUMS mismatch: {name}")
        checksum_paths.add(relative.as_posix())
    if checksum_paths != expected | {"manifest.json"}:
        raise SystemExit("SHA256SUMS does not cover the exact release file set")
    actual = {
        path.relative_to(incoming).as_posix()
        for path in incoming.rglob("*")
        if path.is_file() and path.name not in {"manifest.json", "SHA256SUMS", ".activate.py"}
    }
    if actual != expected:
        raise SystemExit("release file set mismatch")
    unlink_if_exists(incoming / ".activate.py")
    normalize_public_permissions(incoming)
    releases = root / "releases"
    final = releases / args.release_id
    if final.exists():
        raise SystemExit("release already exists")
    os.replace(incoming, final)
    temporary = root / f".current.{args.release_id}.tmp"
    unlink_if_exists(temporary)
    temporary.symlink_to(final)
    os.replace(temporary, root / "current")
    current = final.resolve()
    candidates = sorted(
        (path for path in releases.iterdir() if path.is_dir() and path.name != ".incoming"),
        key=lambda path: path.name,
        reverse=True,
    )
    keep = {path.resolve() for path in candidates[: max(1, args.keep)]} | {current}
    for path in candidates:
        if path.resolve() not in keep:
            shutil.rmtree(path)
    print(json.dumps({"status": "activated", "release_id": args.release_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
