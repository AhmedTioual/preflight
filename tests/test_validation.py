from datetime import date

from app.services.normalize import NormalizedRow
from app.services.template import load_template
from app.services.validation import RegisterFund, ValidationContext, validate

from .conftest import by_rule

T = load_template()
LEI = "999900NSGLOBEQTY0151"
GOOD = {
    "fund_name": "Fund A", "fund_lei": LEI, "period_end": "2026-06-30", "base_currency": "EUR",
    "strategy": "equity", "nav": 100_000_000.0, "gross_leverage_pct": 120.0, "commitment_leverage_pct": 110.0,
    "liq_1d": 50.0, "liq_2_7d": 20.0, "liq_8_30d": 20.0, "liq_31_90d": 10.0, "liq_over_90d": 0.0,
    "investor_count": 10, "top5_investor_pct": 60.0, "largest_investor_pct": 20.0,
}  # fmt: skip


def run(overrides=None, previous=None, register=None, **row_extra):
    values = {**GOOD, **(overrides or {})}
    row = NormalizedRow(0, "fund a", "Fund A", values, {}, row_extra.get("format_errors", {}), {})
    ctx = ValidationContext(
        date(2026, 6, 30),
        "Client",
        register if register is not None else [RegisterFund("Fund A", LEI)],
        (lambda lei, key: previous) if previous else (lambda lei, key: None),
    )
    return {f.rule_id for f in validate([row], T, ctx)}


def test_clean_row_has_no_findings():
    assert run() == set()


def test_each_rule_fires():
    assert run({"fund_lei": "999900NSGL0BEQTY0151"}) == {"LEI-CHECKSUM"}
    assert run(register=[]) == {"LEI-REGISTER"}
    assert run({"period_end": "2026-03-31"}) == {"PERIOD-MATCH"}
    assert run({"base_currency": "EURO"}) == {"CCY-ISO"}
    assert run({"strategy": "Crypto"}) == {"STRATEGY-ALLOWED"}
    assert run({"nav": 0.0}) == {"NAV-POSITIVE"}
    assert run({"liq_1d": 48.0}) == {"LIQ-SUM"}
    assert run({"liq_1d": 150.0, "liq_2_7d": -80.0}) == {"PCT-RANGE"}
    assert run({"investor_count": 0}) == {"INV-COUNT"}
    assert run({"largest_investor_pct": 70.0}) == {"INV-CONCENTRATION"}
    assert run({"commitment_leverage_pct": 130.0}) == {"LEV-ORDER"}
    assert run({"gross_leverage_pct": 350.0}) == {"LEV-HIGH"}
    assert run(previous=(70_000_000.0, date(2026, 3, 31))) == {"NAV-JUMP"}
    assert run({"nav": None}) == {"REQ-FIELD"}
    assert run({"nav": None}, format_errors={"nav": "n/a"}) == {"FMT-VALUE"}


def test_liquidity_tolerance_and_small_nav_change_pass():
    assert run({"liq_1d": 49.6}) == set()  # within 0.5 point
    assert run(previous=(90_000_000.0, date(2026, 3, 31))) == set()  # +11%


def test_duplicate_lei_in_file():
    rows = [NormalizedRow(i, f"fund {i}", f"Fund {i}", dict(GOOD), {}, {}, {}) for i in range(2)]
    ctx = ValidationContext(date(2026, 6, 30), "Client", [RegisterFund("Fund A", LEI)])
    assert {f.rule_id for f in validate(rows, T, ctx)} == {"LEI-UNIQUE"}


def test_planted_issues_in_northstar_sample(northstar_q2):
    counts = northstar_q2.current.counts()
    assert (counts["errors"], counts["warnings"]) == (3, 2)
    lei = by_rule(northstar_q2, "LEI-CHECKSUM")[0]
    assert lei.suggestion == LEI  # proposed from the fund register
    assert by_rule(northstar_q2, "LIQ-SUM")[0].value_text == "98"
    assert by_rule(northstar_q2, "NAV-JUMP")[0].value_text == "+45.2%"
    assert by_rule(northstar_q2, "INV-CONCENTRATION")[0].fund_label == "Northstar Real Assets Fund"
    assert by_rule(northstar_q2, "LEV-ORDER")
