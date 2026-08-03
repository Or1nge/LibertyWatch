#!/usr/bin/env python3
"""Build the versioned 67-company capital-structure authorization registry."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WATCHLIST = PROJECT_ROOT / "config" / "watchlist.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "config" / "issuer_capital_structure_v1.json"
QUOTE_OBSERVED_AT = "2026-08-03T16:48:05.319Z"


EVIDENCE: dict[str, dict[str, Any]] = {
    "A600406": {"shares": "8031756156", "implied": "8031756156", "difference": "0", "date": "2026-04-30"},
    "HK0322": {"shares": "5636516360", "implied": "5637386360", "difference": "0.0001543506564043752726728535567", "date": "2026-04-27"},
    "HK0669": {"shares": "1829209941", "implied": "1827720441", "difference": "0.0008142859748431686442491294114", "date": "2026-03-30"},
    "SH600298": {"shares": "867978471", "implied": "867848671", "difference": "0.0001495428796182664765656382277", "date": "2026-03-31"},
    "SH600426": {"shares": "2123219998", "implied": "2749943015", "difference": "0.2951757319497515395952859709", "date": "2026-03-31"},
    "SH603899": {"shares": "920970377", "implied": "915795377", "difference": "0.005619073239746559188189828173", "date": "2026-04-01"},
    "SZ000423": {"shares": "643976824", "implied": "639826824", "difference": "0.006444331294754793846431964142", "date": "2026-03-20"},
    "SZ002003": {"shares": "1188889653", "implied": "1188889653", "difference": "0", "date": "2026-04-17"},
    "SZ002028": {"shares": "782057732", "implied": "782573282", "difference": "0.0006592224319316620476811550787", "date": "2026-04-18"},
    "SZ002032": {"shares": "801660653", "implied": "799848156", "difference": "0.002260927978961192698077948451", "date": "2026-04-03"},
    "SZ002138": {"shares": "806318354", "implied": "806318354", "difference": "0", "date": "2026-02-28"},
    "SZ002158": {"shares": "534724139", "implied": "534724139", "difference": "0", "date": "2026-04-25"},
    "SZ002507": {"shares": "1153919028", "implied": "1153919028", "difference": "0", "date": "2026-03-28"},
    "SZ002595": {"shares": "800000000", "implied": "1160000000", "difference": "0.45", "date": "2026-03-31"},
    "SZ300285": {"shares": "997048299", "implied": "997048299", "difference": "0", "date": "2026-04-21"},
    "SH600660": {
        "shares": "2609743532",
        "implied": "2609743532",
        "difference": "0",
        "date": "2026-03-18",
        "source_ids": [
            "SECURITY.SH600660.issued_shares",
            "SECURITY.HK03606.issued_shares",
        ],
        "structure_kind": "A_H",
        "classes": ["A", "H"],
        "rights_equal": True,
    },
    "SH600600": {
        "shares": "1364195121",
        "implied": "1364195121",
        "difference": "0",
        "date": "2026-03-27",
        "source_ids": [
            "SECURITY.SH600600.issued_shares",
            "SECURITY.HK00168.issued_shares",
        ],
        "structure_kind": "A_H",
        "classes": ["A", "H"],
        "rights_equal": None,
        "rights_unresolved": True,
    },
    "SZ002352": {
        "shares": "5039430409",
        "implied": "5265308078",
        "difference": "0.04482206334203989203256800048",
        "date": "2026-03-31",
        "source_ids": [
            "SECURITY.SZ002352.issued_shares",
            "SECURITY.HK06936.issued_shares",
        ],
        "structure_kind": "A_H",
        "classes": ["A", "H"],
        "rights_equal": None,
        "rights_unresolved": True,
    },
}


A_H_UNRESOLVED = {"A000333", "A000921", "A600690", "A600941", "A601728"}
ADS_REPRESENTATIONS = {"HK1179", "HK2057"}
DUAL_COUNTER = {"HK2020"}
MULTI_CLASS = {"HK9987", "SH688235"}


def _default_structure(company_id: str, market: str) -> tuple[str, list[str], bool | None, str | None]:
    if company_id in A_H_UNRESOLVED:
        return "A_H", ["A", "H"], None, "A/H rights and current equivalent shares remain unresolved."
    if company_id in ADS_REPRESENTATIONS:
        return "ADS", ["ORDINARY", "ADS_REPRESENTATION"], None, "ADS ratio is not yet authorized."
    if company_id in DUAL_COUNTER:
        return "DUAL_COUNTER", ["ORDINARY"], True, "HKD/RMB counters represent one legal share class."
    if company_id in MULTI_CLASS:
        classes = ["A", "OVERSEAS_ORDINARY"] if company_id == "SH688235" else ["ORDINARY"]
        return "MULTI_CLASS", classes, None, "Class/listing relationship remains unresolved for SEEV."
    return "SINGLE_CLASS", ["A" if market == "CN" else "H"], True, None


def build_registry(watchlist_path: Path) -> dict[str, Any]:
    payload = json.loads(watchlist_path.read_text(encoding="utf-8"))
    securities = payload.get("securities")
    if not isinstance(securities, list) or len(securities) != 67:
        raise ValueError("capital-structure registry requires the formal 67-security watchlist")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for security in securities:
        company_id = str(security.get("issuerId") or "")
        security_id = str(security.get("id") or "")
        market = str(security.get("market") or "")
        if not company_id or not security_id or company_id in seen:
            raise ValueError("watchlist identities are missing or duplicated")
        structure, classes, rights_equal, note = _default_structure(company_id, market)
        evidence = EVIDENCE.get(company_id)
        if evidence:
            structure = str(evidence.get("structure_kind") or structure)
            classes = list(evidence.get("classes") or classes)
            rights_equal = evidence.get("rights_equal", rights_equal)
            source_ids = list(
                evidence.get("source_ids")
                or [f"SECURITY.{security_id}.issued_shares"]
            )
            difference = Decimal(evidence["difference"])
            rights_ready = not evidence.get("rights_unresolved")
            vendor_authorized = rights_ready and difference <= Decimal("0.02")
            direct_authorized = rights_ready
            semantics = (
                "VENDOR_SELECTED_SECURITY_EQUIVALENT_VALUE"
                if structure == "A_H" and vendor_authorized
                else "VENDOR_COMPANY_MARKET_VALUE"
                if vendor_authorized
                else "UNRESOLVED"
            )
            observed_shares = evidence["implied"]
            if difference > Decimal("0.05"):
                note = "Futu implied shares differ from the official count by more than 5%; vendor value is not authorized."
            elif evidence.get("rights_unresolved"):
                note = "Official class counts exist, but distribution-right equivalence is unresolved; SEEV remains blocked."
            row = {
                "company_id": company_id,
                "selected_security_id": security_id,
                "structure_kind": structure,
                "material_share_classes": classes,
                "distribution_rights_equal": rights_equal,
                "selected_security_rights_factor": "1",
                "vendor_total_market_value_semantics": semantics,
                "vendor_value_authorized": vendor_authorized,
                "direct_equivalent_shares_authorized": direct_authorized,
                "official_equivalent_shares": evidence["shares"],
                "authorization_source_ids": source_ids,
                "as_of_date": evidence["date"],
                "authorization_quote_observed_at": QUOTE_OBSERVED_AT,
                "observed_implied_equivalent_shares": observed_shares,
                "observed_relative_difference": evidence["difference"],
            }
        else:
            row = {
                "company_id": company_id,
                "selected_security_id": security_id,
                "structure_kind": structure,
                "material_share_classes": classes,
                "distribution_rights_equal": rights_equal,
                "selected_security_rights_factor": "1",
                "vendor_total_market_value_semantics": "UNRESOLVED",
                "vendor_value_authorized": False,
                "direct_equivalent_shares_authorized": False,
                "official_equivalent_shares": None,
                "authorization_source_ids": [],
                "as_of_date": None,
                "authorization_quote_observed_at": None,
                "observed_implied_equivalent_shares": None,
                "observed_relative_difference": None,
            }
        if note:
            row["notes"] = note
        rows.append(row)
        seen.add(company_id)
    return {
        "schema_version": "issuer-capital-structure-v1",
        "generated_from": "config/watchlist.json plus controlled share-capital-v1 evidence",
        "authorization_policy": {
            "vendor_implied_share_difference_reliable_max": "0.02",
            "vendor_implied_share_difference_estimated_max": "0.05",
            "vendor_implied_share_difference_blocked_above": "0.05",
            "unknown_is_not_zero": True,
        },
        "companies": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = json.dumps(build_registry(args.watchlist), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            raise SystemExit("capital-structure registry is out of date")
        return 0
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
