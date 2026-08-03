from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .release import verify_release
from .snapshot_store import atomic_write_json


SAFE_HOST = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_USER = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_RELEASE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class AliSyncConfig:
    host: str
    port: int
    user: str | None
    key_path: Path | None
    release_root: Path
    keep_releases: int = 5
    connect_timeout: int = 15

    @classmethod
    def from_environment(cls) -> "AliSyncConfig":
        required = {
            "ALI_SSH_HOST": os.getenv("ALI_SSH_HOST"),
            "ALI_RELEASE_ROOT": os.getenv("ALI_RELEASE_ROOT"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise SyncError("missing Ali SSH configuration: " + ", ".join(missing))
        return cls(
            host=str(required["ALI_SSH_HOST"]),
            port=int(os.getenv("ALI_SSH_PORT", "22")),
            user=(os.getenv("ALI_SSH_USER") or None),
            key_path=(
                Path(os.environ["ALI_SSH_KEY_PATH"]).expanduser()
                if os.getenv("ALI_SSH_KEY_PATH")
                else None
            ),
            release_root=Path(str(required["ALI_RELEASE_ROOT"])),
            keep_releases=int(os.getenv("ALI_KEEP_RELEASES", "5")),
            connect_timeout=int(os.getenv("ALI_CONNECT_TIMEOUT", "15")),
        )

    def validate(self) -> None:
        if (
            not SAFE_HOST.fullmatch(self.host)
            or self.host.startswith("-")
            or (
                self.user is not None
                and (not SAFE_USER.fullmatch(self.user) or self.user.startswith("-"))
            )
        ):
            raise SyncError("unsafe SSH host or user")
        if not 1 <= self.port <= 65535 or not 1 <= self.connect_timeout <= 300:
            raise SyncError("invalid SSH port or timeout")
        if self.key_path is not None:
            if not self.key_path.is_file():
                raise SyncError("SSH identity file does not exist")
            if self.key_path.stat().st_mode & 0o077:
                raise SyncError("SSH identity file must have mode 0600 or stricter")
        if (
            not self.release_root.is_absolute()
            or ".." in self.release_root.parts
            or not SAFE_REMOTE_PATH.fullmatch(self.release_root.as_posix())
        ):
            raise SyncError("ALI_RELEASE_ROOT must be a safe absolute path")
        if not 1 <= self.keep_releases <= 100:
            raise SyncError("ALI_KEEP_RELEASES must be within 1..100")


class AliReleaseSynchronizer:
    def __init__(
        self,
        config: AliSyncConfig,
        *,
        activation_script: Path,
        rollback_script: Path | None = None,
        status_path: Path | None = None,
    ) -> None:
        self.config = config
        self.activation_script = activation_script
        self.rollback_script = rollback_script
        self.status_path = status_path

    def _ssh_options(self) -> list[str]:
        options = [
            "-p",
            str(self.config.port),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.config.connect_timeout}",
        ]
        if self.config.key_path is not None:
            options.extend(
                ["-i", str(self.config.key_path), "-o", "IdentitiesOnly=yes"]
            )
        return options

    def _scp_options(self) -> list[str]:
        options = [
            "-P",
            str(self.config.port),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.config.connect_timeout}",
        ]
        if self.config.key_path is not None:
            options.extend(
                ["-i", str(self.config.key_path), "-o", "IdentitiesOnly=yes"]
            )
        return options

    def _target(self) -> str:
        return (
            f"{self.config.user}@{self.config.host}"
            if self.config.user
            else self.config.host
        )

    def _record(self, payload: dict) -> None:
        if self.status_path:
            atomic_write_json(self.status_path, payload)

    def sync(self, release: Path, *, channel: str) -> None:
        self.config.validate()
        manifest = verify_release(release)
        release_id = str(manifest["release_id"])
        if (
            not SAFE_RELEASE.fullmatch(release_id)
            or channel not in {"structured", "analysis"}
            or manifest.get("channel") != channel
        ):
            raise SyncError("unsafe release ID or channel")
        target = self._target()
        channel_root = self.config.release_root / channel
        incoming = channel_root / "releases" / ".incoming" / release_id
        ssh = ["ssh", *self._ssh_options(), "--", target]
        scp_options = self._scp_options()
        try:
            subprocess.run(
                [*ssh, "install", "-d", "-m", "0755", str(incoming)],
                check=True,
                text=True,
                capture_output=True,
            )
            subprocess.run(
                ["scp", *scp_options, "-r", "--", f"{release}/.", f"{target}:{incoming}/"],
                check=True,
                text=True,
                capture_output=True,
            )
            subprocess.run(
                ["scp", *scp_options, "--", str(self.activation_script), f"{target}:{incoming}/.activate.py"],
                check=True,
                text=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    *ssh,
                    "python3",
                    str(incoming / ".activate.py"),
                    "--channel-root",
                    str(channel_root),
                    "--incoming",
                    str(incoming),
                    "--release-id",
                    release_id,
                    "--channel",
                    channel,
                    "--keep",
                    str(self.config.keep_releases),
                ],
                check=True,
                text=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            self._record(
                {
                    "status": "WAITING_RETRY",
                    "channel": channel,
                    "release_id": release_id,
                    "last_attempt_at": datetime.now(timezone.utc).isoformat(),
                    "error": str(error)[:1000],
                }
            )
            raise SyncError(f"Ali sync failed; current remote release was left unchanged: {error}") from error
        self._record(
            {
                "status": "SUCCEEDED",
                "channel": channel,
                "release_id": release_id,
                "last_success_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def rollback(self, *, channel: str, release_id: str) -> None:
        self.config.validate()
        if channel not in {"structured", "analysis"} or not SAFE_RELEASE.fullmatch(release_id):
            raise SyncError("unsafe release ID or channel")
        if self.rollback_script is None or not self.rollback_script.is_file():
            raise SyncError("remote rollback helper is missing")
        target = self._target()
        channel_root = self.config.release_root / channel
        helper = channel_root / ".rollback-release.py"
        ssh = ["ssh", *self._ssh_options(), "--", target]
        try:
            subprocess.run(
                [*ssh, "install", "-d", "-m", "0755", str(channel_root)],
                check=True,
                text=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "scp",
                    *self._scp_options(),
                    "--",
                    str(self.rollback_script),
                    f"{target}:{helper}",
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    *ssh,
                    "python3",
                    str(helper),
                    "--channel-root",
                    str(channel_root),
                    "--channel",
                    channel,
                    "--release-id",
                    release_id,
                ],
                check=True,
                text=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise SyncError(f"remote rollback failed; current release was left unchanged: {error}") from error
        self._record(
            {
                "status": "SUCCEEDED",
                "channel": channel,
                "release_id": release_id,
                "rollback_at": datetime.now(timezone.utc).isoformat(),
            }
        )
