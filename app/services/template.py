"""Reporting template: fields and rules loaded from YAML, plus identifier helpers."""

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "template_defs"
DEFAULT_TEMPLATE = "fund_return_v1"

TYPE_LABELS = {
    "text": "text",
    "lei": "LEI",
    "date": "date",
    "currency": "currency code",
    "enum": "value",
    "amount": "amount",
    "percent": "percentage",
    "integer": "whole number",
}

TRANSFORMS = {
    "none": "As is",
    "thousands": "Multiply by 1,000",
    "millions": "Multiply by 1,000,000",
    "fraction_to_percent": "Fraction to % (x100)",
}

ISO_CURRENCIES = {
    "EUR", "USD", "GBP", "CHF", "JPY", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON", "BGN", "ISK",
    "CAD", "AUD", "NZD", "HKD", "SGD", "CNY", "KRW", "INR", "BRL", "MXN", "ZAR", "TRY", "ILS", "AED",
    "SAR", "QAR", "KWD", "MAD", "TWD", "THB", "MYR", "IDR", "PHP", "CLP", "COP", "PEN",
}  # fmt: skip

CURRENCY_NAMES = {
    "euro": "EUR", "euros": "EUR", "€": "EUR",
    "us dollar": "USD", "us dollars": "USD", "dollar": "USD", "usd $": "USD", "$": "USD",
    "pound sterling": "GBP", "sterling": "GBP", "british pound": "GBP", "£": "GBP",
    "swiss franc": "CHF", "franc suisse": "CHF", "yen": "JPY", "japanese yen": "JPY",
    "swedish krona": "SEK", "norwegian krone": "NOK", "danish krone": "DKK",
}  # fmt: skip


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    type: str
    required: bool
    description: str
    aliases: tuple[str, ...]
    values: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type)

    @property
    def numeric(self) -> bool:
        return self.type in {"amount", "percent", "integer"}


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    version: str
    fields: tuple[FieldDef, ...]
    rules: tuple[dict, ...]
    regulator: str = "the regulator"

    @property
    def by_key(self) -> dict[str, FieldDef]:
        return {f.key: f for f in self.fields}

    def field(self, key: str) -> FieldDef:
        return self.by_key[key]

    def rule(self, rule_id: str) -> dict:
        return next(r for r in self.rules if r["id"] == rule_id)

    def rule_fields(self, rule_id: str, default_field: str) -> list[str]:
        """Template fields a rule looks at (used to highlight related cells)."""
        rule = self.rule(rule_id)
        if "fields" in rule and rule["check"] == "sum_equals":
            return list(rule["fields"])
        fields = [default_field]
        if rule.get("other"):
            fields.append(rule["other"])
        return fields


@lru_cache
def load_template(template_id: str = DEFAULT_TEMPLATE) -> Template:
    with open(TEMPLATE_DIR / f"{template_id}.yaml", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    fields = tuple(
        FieldDef(
            key=f["key"],
            label=f["label"],
            type=f["type"],
            required=bool(f.get("required")),
            description=f.get("description", ""),
            aliases=tuple(f.get("aliases", [])),
            values={k: tuple(v) for k, v in (f.get("values") or {}).items()},
        )
        for f in raw["fields"]
    )
    meta = raw["template"]
    return Template(
        meta["id"],
        meta["name"],
        str(meta["version"]),
        fields,
        tuple(raw["rules"]),
        meta.get("regulator", "the regulator"),
    )


# --------------------------------------------------------------------------- text helpers
def fold(text: str) -> str:
    """Lowercase, strip accents and punctuation noise: 'Liq. < 1 jour (%)' -> 'liq < 1 jour'."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = text.lower().replace("_", " ")
    text = re.sub(r"\((?:%|in %|en %)\)|%", " ", text)
    text = re.sub(r"[.:;,'\"]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def header_key(header: str) -> str:
    """Stable key used to remember a client's column across files."""
    return re.sub(r"\s+", " ", str(header).strip().lower())


def fund_key(name: str | None, row_index: int) -> str:
    if name and str(name).strip():
        return re.sub(r"\s+", " ", fold(str(name)))
    return f"row-{row_index + 1}"


# --------------------------------------------------------------------------- LEI (ISO 17442)
def _lei_digits(text: str) -> str:
    return "".join(str(int(ch, 36)) for ch in text)


def lei_is_valid(value: str) -> bool:
    value = str(value).strip().upper()
    if not re.fullmatch(r"[0-9A-Z]{18}[0-9]{2}", value):
        return False
    return int(_lei_digits(value)) % 97 == 1


def lei_with_check_digits(base18: str) -> str:
    base18 = base18.upper()
    if not re.fullmatch(r"[0-9A-Z]{18}", base18):
        raise ValueError("LEI base must be 18 alphanumeric characters")
    check = 98 - int(_lei_digits(base18 + "00")) % 97
    return f"{base18}{check:02d}"
