from __future__ import annotations

from liberty_v2.cashflow_reconciliation_v2 import accepted_status
from liberty_v2.import_cashflow_v2 import _exact


def test_v2_acceptance_status_is_narrow() -> None:
    assert accepted_status("ACCEPT_V1")
    assert accepted_status("ACCEPT_OFFICIAL_ADJACENT")
    assert accepted_status("ACCEPT_OFFICIAL_PLUS_FUTU")
    assert not accepted_status("FUTU_ONLY")
    assert not accepted_status("CONFLICT")


def test_v2_import_amount_comparison_has_no_tolerance() -> None:
    assert _exact("100.00", "100")
    assert not _exact("100.0001", "100")
