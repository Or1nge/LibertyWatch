from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class LastValidSnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, company_id: str) -> Path:
        if not company_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for character in company_id):
            raise ValueError("unsafe company id")
        return self.root / company_id / "latest_valid.json"

    def load(self, company_id: str) -> dict[str, Any] | None:
        path = self._path(company_id)
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None

    def select_publishable(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        company_id = str(candidate.get("company_id") or "")
        status = candidate.get("data_status")
        if status == "VALID":
            result = dict(candidate)
            atomic_write_json(self._path(company_id), result)
            return result
        if status == "PARTIAL":
            return dict(candidate)
        previous = self.load(company_id)
        if previous is None:
            result = dict(candidate)
            result["update_status"] = "BLOCKED_NO_VALID_BASELINE"
            return result
        result = dict(previous)
        result["data_status"] = "STALE" if status == "STALE" else "INVALID"
        result["update_status"] = "BLOCKED_USING_LAST_VALID"
        result["blocked_update"] = {
            "attempted_at": candidate.get("calculated_at"),
            "errors": list(candidate.get("validation_errors") or []),
        }
        return result
