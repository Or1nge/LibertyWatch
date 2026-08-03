from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .snapshot_store import atomic_write_json


class MigrationError(RuntimeError):
    pass


def build_v2_staging_records(companies_path: Path, watchlist_path: Path) -> list[dict[str, Any]]:
    companies_payload = json.loads(companies_path.read_text(encoding="utf-8"))
    watchlist_payload = json.loads(watchlist_path.read_text(encoding="utf-8"))
    companies = companies_payload.get("companies")
    securities = watchlist_payload.get("securities")
    if not isinstance(companies, list) or not isinstance(securities, list):
        raise MigrationError("v1 company or watchlist source is invalid")
    by_issuer = {str(item.get("issuerId")): item for item in securities}
    if len(companies) != len(by_issuer):
        raise MigrationError(
            f"v1 source count mismatch: companies={len(companies)} securities={len(by_issuer)}"
        )
    records: list[dict[str, Any]] = []
    for company in companies:
        issuer_id = str(company.get("issuerId") or company.get("issuer_id") or "")
        security = by_issuer.get(issuer_id)
        if security is None:
            raise MigrationError(f"missing watchlist security for {issuer_id}")
        market = str(security.get("market") or company.get("market") or "")
        share_class = "A" if market == "CN" else "H" if market == "HK" else market
        yield_basis = security.get("yieldBasis") if isinstance(security.get("yieldBasis"), dict) else {}
        records.append(
            {
                "migration_version": "v1-to-v2.0.0",
                "company_id": issuer_id,
                "company_name": company.get("name") or security.get("name"),
                "industry_kind": "UNSUPPORTED",
                "expected_share_classes": [share_class],
                "securities": [
                    {
                        "security_id": security.get("id"),
                        "ticker": security.get("ticker"),
                        "market": market,
                        "share_class": share_class,
                        "currency": security.get("currency"),
                    }
                ],
                "share_classes": [
                    {
                        "security_id": security.get("id"),
                        "share_class": share_class,
                        "price": None,
                        "issued_shares": None,
                        "currency": security.get("currency"),
                        "fx_to_base": None,
                        "price_timestamp": None,
                        "quote_status": "MISSING",
                        "rights_verified": False,
                        "economic_rights_factor": "1",
                        "material": True,
                    }
                ],
                "annual_distributions": [],
                "coverage": {},
                "organic_growth_series": [],
                "valuation": {"metric": None, "current": None, "historical_median": None},
                "structured_scores": {},
                "risk_scores": {},
                "veto_inputs": {},
                "source_summary": {
                    "migration_status": "WAITING_SOURCE_BACKFILL",
                    "legacy_security_source": "webapp/config/watchlist.json",
                    "legacy_company_source": "data/source/companies.json",
                },
                "legacy_metrics": {
                    "schema_version": "shareholder-return-v1",
                    "annual_average_per_share_cny": yield_basis.get("annualAveragePerShareCny"),
                    "window_years": yield_basis.get("windowYears"),
                    "method": yield_basis.get("method"),
                },
            }
        )
    return records


def migrate_v1(
    companies_path: Path,
    watchlist_path: Path,
    target_dir: Path,
    *,
    apply: bool = False,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    records = build_v2_staging_records(companies_path, watchlist_path)
    plan = {
        "mode": "apply" if apply else "dry-run",
        "company_count": len(records),
        "target_dir": str(target_dir),
        "existing_target": target_dir.exists(),
        "source_company_count": len(records),
        "note": "No raw data or production snapshot is rewritten.",
    }
    if not apply:
        return plan
    if target_dir.exists():
        if backup_root is None:
            raise MigrationError("an existing migration target requires an explicit backup root")
        backup = backup_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(target_dir, backup)
        plan["backup_dir"] = str(backup)
    target_dir.mkdir(parents=True, exist_ok=True)
    for record in records:
        atomic_write_json(target_dir / "companies" / f"{record['company_id']}.json", record)
    atomic_write_json(
        target_dir / "migration_manifest.json",
        {
            "migration_version": "v1-to-v2.0.0",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "company_count": len(records),
            "company_ids": [record["company_id"] for record in records],
            "writes_production_data": False,
        },
    )
    return plan
