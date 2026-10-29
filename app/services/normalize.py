"""Turn a client's raw rows into template values.

Deterministic only. For each row:
  1. pick each template field's raw value through the confirmed column mapping,
  2. apply the mapping's unit transform (thousands, millions, fraction to %),
  3. parse the value for the field type (dates, European number formats, currency names...),
  4. apply analyst overrides (corrections with a written reason).
Unparseable values are kept and reported, never silently dropped.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from app.services.template import CURRENCY_NAMES, ISO_CURRENCIES, FieldDef, Template, fold, fund_key

MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january", "janv", "janvier"),
            ("feb", "february", "fev", "fevrier", "févr", "février"),
            ("mar", "march", "mars"),
            ("apr", "april", "avr", "avril"),
            ("may", "mai"),
            ("jun", "june", "juin"),
            ("jul", "july", "juil", "juillet"),
            ("aug", "august", "aout", "août"),
            ("sep", "sept", "september", "septembre"),
            ("oct", "october", "octobre"),
            ("nov", "november", "novembre"),
            ("dec", "december", "dece", "décembre", "decembre"),
        ],
        start=1,
    )
    for m in names
}


class ParseError(ValueError):
    pass


# --------------------------------------------------------------------------- parsers
def parse_number(raw) -> float:
    if isinstance(raw, bool):
        raise ParseError("boolean")
    if isinstance(raw, int | float):
        return float(raw)
    text = str(raw).strip()
    if not text:
        raise ParseError("empty")
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[€$£%\s  ']|EUR|USD|GBP|CHF", "", text.strip("()"), flags=re.I)
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", text):  # 1.234.567,89
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?[1-9]\d{0,2}(,\d{3})+", text):  # 431,250: comma as thousands separator
        text = text.replace(",", "")
    elif re.fullmatch(r"-?\d+,\d+", text):  # 12,5 or 0,125: decimal comma
        text = text.replace(",", ".")
    else:  # 1,234,567.89
        text = text.replace(",", "")
    try:
        value = float(text)
    except ValueError as exc:
        raise ParseError(text) from exc
    return -value if negative else value


def parse_date(raw) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?", text):
        y, mo, d = map(int, m.groups())
    elif m := re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", text):  # European day first
        d, mo, y = map(int, m.groups())
    elif m := re.fullmatch(r"(\d{1,2})\s+([A-Za-zéû]+)\.?\s+(\d{4})", text):
        d, y = int(m.group(1)), int(m.group(3))
        mo = MONTHS.get(m.group(2).lower())
    elif m := re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", text):
        mo, d, y = MONTHS.get(m.group(1).lower()), int(m.group(2)), int(m.group(3))
    else:
        raise ParseError(text)
    if not mo:
        raise ParseError(text)
    try:
        return date(y, mo, d)
    except ValueError as exc:
        raise ParseError(text) from exc


def parse_currency(raw) -> str:
    text = str(raw).strip()
    if text.upper() in ISO_CURRENCIES:
        return text.upper()
    return CURRENCY_NAMES.get(text.lower(), text.upper())


def parse_enum(raw, field_def: FieldDef) -> str:
    folded = fold(raw)
    for key, synonyms in field_def.values.items():
        if folded == fold(key) or folded in {fold(s) for s in synonyms}:
            return key
    return str(raw).strip()


TRANSFORM_FACTORS = {"none": 1, "thousands": 1_000, "millions": 1_000_000, "fraction_to_percent": 100}


def parse_value(raw, field_def: FieldDef, transform: str = "none"):
    """Parse one raw value for a field. Returns a JSON-serialisable value."""
    t = field_def.type
    if t == "date":
        return parse_date(raw).isoformat()
    if t in {"amount", "percent"}:
        value = parse_number(raw) * TRANSFORM_FACTORS.get(transform, 1)
        return round(value, 6)
    if t == "integer":
        value = parse_number(raw)
        if value != int(value):
            raise ParseError(str(raw))
        return int(value)
    if t == "currency":
        return parse_currency(raw)
    if t == "enum":
        return parse_enum(raw, field_def)
    if t == "lei":
        return re.sub(r"\s+", "", str(raw)).upper()
    return re.sub(r"\s+", " ", str(raw)).strip()


def is_blank(raw) -> bool:
    return raw is None or (isinstance(raw, str) and not raw.strip())


# --------------------------------------------------------------------------- formatting
def display_value(value, field_def: FieldDef) -> str:
    if value is None or value == "":
        return ""
    t = field_def.type
    if t == "amount":
        return f"{value:,.0f}"
    if t == "percent":
        return f"{value:,.2f}".rstrip("0").rstrip(".") + "%"
    if t == "integer":
        return f"{value:,}"
    if t == "date":
        try:
            return date.fromisoformat(value).strftime("%d %b %Y")
        except (TypeError, ValueError):
            return str(value)
    if t == "enum":
        return str(value).replace("_", " ").capitalize()
    return str(value)


def value_text(value) -> str:
    """Canonical string of a parsed value, used to match overrides and justifications."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


# --------------------------------------------------------------------------- rows
@dataclass
class NormalizedRow:
    index: int
    key: str
    label: str
    values: dict = field(default_factory=dict)  # field -> parsed value or None
    raw: dict = field(default_factory=dict)  # field -> raw value (string)
    format_errors: dict = field(default_factory=dict)  # field -> raw value that failed to parse
    overridden: dict = field(default_factory=dict)  # field -> {"from": ..., "note": ..., "by": ...}

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "key": self.key,
            "label": self.label,
            "values": self.values,
            "raw": self.raw,
            "format_errors": self.format_errors,
            "overridden": self.overridden,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NormalizedRow":
        return cls(**data)


def normalize_rows(rows: list[dict], mapping: dict, template: Template, overrides=()) -> list[NormalizedRow]:
    """`mapping` is {header: {"field": key|None, "transform": str}}; `overrides` are CellOverride-like objects."""
    by_field: dict[str, tuple[str, str]] = {}
    for header, m in mapping.items():
        if m.get("field"):
            by_field.setdefault(m["field"], (header, m.get("transform", "none")))

    result = []
    for i, row in enumerate(rows):
        name_header = by_field.get("fund_name", (None,))[0]
        name = row.get(name_header) if name_header else None
        n = NormalizedRow(index=i, key=fund_key(name, i), label=str(name).strip() if name else f"Row {i + 1}")
        for f in template.fields:
            header, transform = by_field.get(f.key, (None, "none"))
            raw = row.get(header) if header else None
            n.raw[f.key] = "" if raw is None else str(raw)
            if is_blank(raw):
                n.values[f.key] = None
                continue
            try:
                n.values[f.key] = parse_value(raw, f, transform)
            except ParseError:
                n.values[f.key] = None
                n.format_errors[f.key] = str(raw)
        result.append(n)

    for o in overrides:
        for n in result:
            if n.key != o.fund_key:
                continue
            f = template.field(o.field)
            current = n.format_errors.get(o.field, value_text(n.values.get(o.field)))
            if current != o.original_value:
                continue  # client sent a different value since: override no longer applies
            try:
                new = parse_value(o.new_value, f)
            except ParseError:
                continue
            n.overridden[o.field] = {"from": o.original_value, "note": o.note, "by": o.user.full_name}
            n.values[o.field] = new
            n.format_errors.pop(o.field, None)
    return result
