from __future__ import annotations

import ast
import json
import stat
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from liberty_v2.release import (
    AtomicReleaseBuilder,
    ReleaseError,
    build_analysis_release,
    verify_release,
)
from liberty_v2.sync import AliReleaseSynchronizer, AliSyncConfig, SyncError


PROJECT = Path(__file__).resolve().parents[1]
ACTIVATOR = PROJECT / "scripts" / "support" / "activate_remote_release.py"
ROLLBACK = PROJECT / "scripts" / "support" / "rollback_remote_release.py"


@pytest.mark.parametrize("helper", [ACTIVATOR, ROLLBACK])
def test_remote_release_helpers_parse_as_python_36(helper: Path) -> None:
    ast.parse(helper.read_text(encoding="utf-8"), filename=str(helper), feature_version=(3, 6))


def test_atomic_release_manifest_switch_and_rollback(tmp_path: Path) -> None:
    root = tmp_path / "structured"
    builder = AtomicReleaseBuilder(root, keep_releases=3)
    first = builder.build({"companies.json": b"{\"version\":1}\n"}, channel="structured", release_id="r1")
    assert verify_release(first)["release_id"] == "r1"
    assert (root / "current").resolve() == first.resolve()
    second = builder.build({"companies.json": b"{\"version\":2}\n"}, channel="structured", release_id="r2")
    assert (root / "current").resolve() == second.resolve()
    builder.activate("r1")
    assert (root / "current").resolve() == first.resolve()


def test_release_verification_rejects_tampering_without_switching(tmp_path: Path) -> None:
    root = tmp_path / "structured"
    builder = AtomicReleaseBuilder(root)
    first = builder.build({"companies.json": b"{}\n"}, channel="structured", release_id="good")
    (first / "companies.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ReleaseError):
        verify_release(first)


def test_release_verification_rejects_unmanifested_file(tmp_path: Path) -> None:
    release = AtomicReleaseBuilder(tmp_path / "structured").build(
        {"companies.json": b"{}\n"}, channel="structured", release_id="good"
    )
    (release / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ReleaseError, match="file set mismatch"):
        verify_release(release)


def test_local_rollback_rejects_unsafe_release_id(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="unsafe release ID"):
        AtomicReleaseBuilder(tmp_path / "structured").activate("../../escape")


def test_analysis_public_release_excludes_events_stderr_and_inputs(tmp_path: Path) -> None:
    payload = {
        "analysis_id": "analysis-1",
        "as_of_date": "2026-08-01",
        "input_snapshot_hash": "a" * 64,
        "verdict": "WATCH",
        "risk_overlay": "MEDIUM",
        "sources": [{"title": "source"}],
        "reviewed_overlay_candidates": {
            "business_durability": {"value": 70},
            "governance_capital_allocation": None,
        },
        "report_markdown": "# Report",
    }
    release = build_analysis_release(
        tmp_path / "analysis",
        analyses={"issuer": (payload, "# Report")},
        statuses={
            "issuer": {
                "status": "WAITING_RETRY",
                "job_id": "analysis-2",
                "latest_analysis_id": "analysis-1",
                "error_message": "/home/private/stderr must remain local",
                "result_path": "/home/private/output",
            }
        },
    )
    files = {path.relative_to(release).as_posix() for path in release.rglob("*") if path.is_file()}
    assert "companies/issuer/latest.json" in files
    assert "companies/issuer/report.md" in files
    assert "companies/issuer/status.json" in files
    assert all("stderr" not in name and "events" not in name and "/input/" not in name for name in files)
    status = json.loads((release / "companies" / "issuer" / "status.json").read_text())
    public_analysis = json.loads((release / "companies" / "issuer" / "latest.json").read_text())
    assert status["status"] == "WAITING_RETRY"
    assert "error_message" not in status and "result_path" not in status
    assert "reviewed_overlay_candidates" not in public_analysis


def test_remote_activator_verifies_incoming_and_switches_atomically(tmp_path: Path) -> None:
    local_root = tmp_path / "local"
    release = AtomicReleaseBuilder(local_root).build(
        {"companies.json": b"{}\n"},
        channel="structured",
        release_id="release-1",
        activate=False,
    )
    remote = tmp_path / "remote" / "structured"
    incoming = remote / "releases" / ".incoming" / "release-1"
    shutil.copytree(release, incoming)
    shutil.copy2(ACTIVATOR, incoming / ".activate.py")
    incoming.chmod(0o770)
    for path in incoming.rglob("*"):
        path.chmod(0o770 if path.is_dir() else 0o640)
    subprocess.run(
        [
            sys.executable,
            str(incoming / ".activate.py"),
            "--channel-root",
            str(remote),
            "--incoming",
            str(incoming),
            "--release-id",
            "release-1",
            "--channel",
            "structured",
        ],
        check=True,
    )
    final = remote / "releases" / "release-1"
    assert final.is_dir()
    assert (remote / "current").resolve() == final.resolve()
    assert verify_release(final)["release_id"] == "release-1"
    assert stat.S_IMODE(final.stat().st_mode) == 0o755
    assert all(
        stat.S_IMODE(path.stat().st_mode) == (0o755 if path.is_dir() else 0o644)
        for path in final.rglob("*")
    )


def test_upload_failure_is_recorded_and_does_not_claim_success(tmp_path: Path, monkeypatch) -> None:
    release = AtomicReleaseBuilder(tmp_path / "local").build(
        {"companies.json": b"{}\n"}, channel="structured", release_id="release-1"
    )
    key = tmp_path / "id_ed25519"
    key.write_text("fake", encoding="utf-8")
    key.chmod(0o600)
    status_path = tmp_path / "sync-status.json"
    synchronizer = AliReleaseSynchronizer(
        AliSyncConfig(
            host="ali",
            port=22,
            user="liberty",
            key_path=key,
            release_root=Path("/usr/LibertyWatch/shared/releases-v2"),
        ),
        activation_script=ACTIVATOR,
        status_path=status_path,
    )

    def fail(*args, **kwargs):
        del args, kwargs
        raise subprocess.CalledProcessError(1, "ssh")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(SyncError):
        synchronizer.sync(release, channel="structured")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "WAITING_RETRY"
    assert status["release_id"] == "release-1"


def test_sync_rejects_permissive_private_key(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("fake", encoding="utf-8")
    key.chmod(0o644)
    config = AliSyncConfig("ali", 22, "liberty", key, Path("/safe/root"))
    with pytest.raises(SyncError, match="0600"):
        config.validate()


def test_sync_can_reuse_existing_ssh_config_alias_without_explicit_key() -> None:
    config = AliSyncConfig(
        "existing-tunnel",
        22,
        None,
        None,
        Path("/safe/release-root"),
    )
    config.validate()
    synchronizer = AliReleaseSynchronizer(config, activation_script=ACTIVATOR)
    assert synchronizer._target() == "existing-tunnel"
    assert "-i" not in synchronizer._ssh_options()


def test_sync_rejects_shell_like_remote_path(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("fake", encoding="utf-8")
    key.chmod(0o600)
    config = AliSyncConfig(
        "ali",
        22,
        "liberty",
        key,
        Path("/safe/root;touch-bad"),
    )
    with pytest.raises(SyncError, match="safe absolute path"):
        config.validate()


def test_remote_rollback_helper_verifies_then_switches(tmp_path: Path) -> None:
    root = tmp_path / "structured"
    builder = AtomicReleaseBuilder(root)
    first = builder.build(
        {"companies.json": b'{"version":1}\n'},
        channel="structured",
        release_id="r1",
    )
    second = builder.build(
        {"companies.json": b'{"version":2}\n'},
        channel="structured",
        release_id="r2",
    )
    assert (root / "current").resolve() == second.resolve()
    first.chmod(0o770)
    for path in first.rglob("*"):
        path.chmod(0o770 if path.is_dir() else 0o640)
    subprocess.run(
        [
            sys.executable,
            str(ROLLBACK),
            "--channel-root",
            str(root),
            "--channel",
            "structured",
            "--release-id",
            "r1",
        ],
        check=True,
    )
    assert (root / "current").resolve() == first.resolve()
    assert stat.S_IMODE(first.stat().st_mode) == 0o755
    assert all(
        stat.S_IMODE(path.stat().st_mode) == (0o755 if path.is_dir() else 0o644)
        for path in first.rglob("*")
    )
