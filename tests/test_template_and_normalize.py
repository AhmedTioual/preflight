from datetime import date

import pytest

from app.demo_data import NS_EQUITY, NS_EQUITY_TYPO
from app.services.normalize import ParseError, normalize_rows, parse_currency, parse_date, parse_number, parse_value
from app.services.template import fold, header_key, lei_is_valid, lei_with_check_digits, load_template

T = load_template()


def test_template_loads_with_unique_keys_and_valid_rule_checks():
    keys = [f.key for f in T.fields]
    assert len(keys) == len(set(keys)) == 16
    from app.services.validation import Validator

    for rule in T.rules:
        assert hasattr(Validator, f"check_{rule['check']}"), rule["id"]
        for k in [rule.get("field"), rule.get("other"), *rule.get("fields", [])]:
            assert k is None or k in T.by_key, (rule["id"], k)


def test_lei_check_digits():
    assert lei_is_valid(NS_EQUITY)
    assert not lei_is_valid(NS_EQUITY_TYPO)  # letter O typed as digit 0
    assert not lei_is_valid("TOO-SHORT")
    assert lei_with_check_digits("999900NSGLOBEQTY01") == NS_EQUITY


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1 234,56", 1234.56), ("1.234.567,8", 1234567.8), ("1,234,567.8", 1234567.8), ("12,5", 12.5),
     ("98%", 98.0), ("0,125", 0.125), ("1,250", 1250.0), ("€ 431,250", 431250.0), ("(120)", -120.0), (42, 42.0)],
)  # fmt: skip
def test_parse_number_handles_european_and_english_formats(raw, expected):
    assert parse_number(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw", ["30/06/2026", "2026-06-30", "30.06.2026", "30 Jun 2026", "30 juin 2026", "June 30, 2026"]
)
def test_parse_date_formats(raw):
    assert parse_date(raw) == date(2026, 6, 30)


def test_parse_errors_are_raised_not_hidden():
    with pytest.raises(ParseError):
        parse_number("n/a")
    with pytest.raises(ParseError):
        parse_date("31/02/2026")
    with pytest.raises(ParseError):
        parse_value("12.5", T.field("investor_count"))


def test_currency_names_and_enum_synonyms():
    assert parse_currency("Euro") == "EUR"
    assert parse_currency("usd") == "USD"
    assert parse_value("Long/short equity", T.field("strategy")) == "equity"
    assert parse_value("Obligataire", T.field("strategy")) == "fixed_income"
    assert parse_value("Crypto", T.field("strategy")) == "Crypto"  # kept as is, rejected by the rules


def test_transforms():
    assert parse_value("431250", T.field("nav"), "thousands") == 431_250_000
    assert parse_value("0,41", T.field("liq_1d"), "fraction_to_percent") == 41


def test_fold_and_header_key():
    assert fold("Liquidité 1 jour (%)") == "liquidite 1 jour"
    assert header_key("  Net Asset Val (EUR k) ") == "net asset val (eur k)"


def test_normalize_keeps_unreadable_values_as_format_errors():
    rows = [{"Name": "Fund A", "NAV": "about 12m"}]
    mapping = {"Name": {"field": "fund_name"}, "NAV": {"field": "nav", "transform": "none"}}
    [row] = normalize_rows(rows, mapping, T)
    assert row.values["nav"] is None
    assert row.format_errors == {"nav": "about 12m"}
    assert row.key == "fund a"
