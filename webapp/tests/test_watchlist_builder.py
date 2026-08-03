from __future__ import annotations

from scripts.support.build_watchlist_config import (
    TRACKED_SECURITIES,
    build_watchlist,
)


def test_official_selection_has_one_security_per_issuer_and_prefers_a_share() -> None:
    payload = build_watchlist()

    assert len(TRACKED_SECURITIES) == 67
    assert len({item[0] for item in TRACKED_SECURITIES}) == 67
    assert len({item["issuerId"] for item in payload["securities"]}) == 67
    assert [item[0] for item in TRACKED_SECURITIES].count("迈瑞医疗") == 1
    assert payload["securities"][0]["quoteCode"] == "SH.600900"
    assert payload["securities"][-1]["quoteCode"] == "SH.688235"
