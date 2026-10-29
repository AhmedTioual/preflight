"""Rule engine: evaluates the template rules on normalised rows.

Every finding states the rule, the field, the value found, what was expected,
and a plain-language explanation built from the rule's `explain` text.
No AI is used here: the same data always gives the same findings.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from app.services.normalize import NormalizedRow, display_value, value_text
from app.services.template import ISO_CURRENCIES, Template, fold, lei_is_valid


@dataclass
class FindingData:
    row_index: int
    fund_key: str
    fund_label: str
    field: str
    rule_id: str
    severity: str
    value_text: str
    expected_text: str
    message: str
    suggestion: str | None = None


@dataclass
class RegisterFund:
    name: str
    lei: str
    last_nav: float | None = None
    last_period: date | None = None


@dataclass
class ValidationContext:
    period_end: date
    client_name: str
    register: list[RegisterFund] = field(default_factory=list)
    # (lei, name_key) -> (previous NAV, period) from the last approved filing, if any
    previous_nav: Callable[[str | None, str], tuple[float, date] | None] = lambda lei, key: None

    def register_by_lei(self) -> dict[str, RegisterFund]:
        return {f.lei: f for f in self.register}

    def register_by_name(self) -> dict[str, RegisterFund]:
        return {fold(f.name): f for f in self.register}


class Validator:
    def __init__(self, template: Template, ctx: ValidationContext):
        self.t = template
        self.ctx = ctx
        self.findings: list[FindingData] = []

    # ------------------------------------------------------------------ helpers
    def _add(self, row: NormalizedRow, rule: dict, field_key: str, value: str, expected: str = "", **extra):
        f = self.t.field(field_key)
        message = rule["explain"].format(
            label=f.label,
            value=value,
            expected=expected,
            client=self.ctx.client_name,
            type_label=f.type_label,
        )
        self.findings.append(
            FindingData(
                row_index=row.index,
                fund_key=row.key,
                fund_label=row.label,
                field=field_key,
                rule_id=rule["id"],
                severity=rule["severity"],
                value_text=value,
                expected_text=expected,
                message=message,
                suggestion=extra.get("suggestion"),
            )
        )

    def _shown(self, row: NormalizedRow, field_key: str) -> str:
        return display_value(row.values.get(field_key), self.t.field(field_key))

    @staticmethod
    def _num(row: NormalizedRow, key: str) -> float | None:
        v = row.values.get(key)
        return float(v) if isinstance(v, int | float) else None

    @staticmethod
    def _fmt(v: float) -> str:
        return f"{v:,.2f}".rstrip("0").rstrip(".")

    # ------------------------------------------------------------------ run
    def run(self, rows: list[NormalizedRow]) -> list[FindingData]:
        for rule in self.t.rules:
            check = getattr(self, f"check_{rule['check']}")
            check(rule, rows)
        return self.findings

    # ------------------------------------------------------------------ checks
    def check_required(self, rule, rows):
        for row in rows:
            for f in self.t.fields:
                if f.required and row.values.get(f.key) is None and f.key not in row.format_errors:
                    self._add(row, rule, f.key, "", "a value")

    def check_format(self, rule, rows):
        for row in rows:
            for key, raw in row.format_errors.items():
                self._add(row, rule, key, raw, self.t.field(key).type_label)

    def check_lei(self, rule, rows):
        by_name = self.ctx.register_by_name()
        for row in rows:
            lei = row.values.get(rule["field"])
            if lei and not lei_is_valid(lei):
                registered = by_name.get(fold(row.label))
                suggestion = registered.lei if registered else None
                self._add(row, rule, rule["field"], lei, "a valid 20-character LEI", suggestion=suggestion)

    def check_in_register(self, rule, rows):
        known = self.ctx.register_by_lei()
        for row in rows:
            lei = row.values.get(rule["field"])
            if lei and lei_is_valid(lei) and lei not in known:
                self._add(row, rule, rule["field"], lei, "an LEI from the fund register")

    def check_unique(self, rule, rows):
        counts = Counter(row.values.get(rule["field"]) for row in rows if row.values.get(rule["field"]))
        for row in rows:
            v = row.values.get(rule["field"])
            if v and counts[v] > 1:
                self._add(row, rule, rule["field"], v, "one row per fund")

    def check_equals_period(self, rule, rows):
        expected = self.ctx.period_end
        for row in rows:
            v = row.values.get(rule["field"])
            if v and date.fromisoformat(v) != expected:
                self._add(row, rule, rule["field"], self._shown(row, rule["field"]), expected.strftime("%d %b %Y"))

    def check_currency(self, rule, rows):
        for row in rows:
            v = row.values.get(rule["field"])
            if v and v not in ISO_CURRENCIES:
                self._add(row, rule, rule["field"], v, "an ISO 4217 code")

    def check_enum(self, rule, rows):
        f = self.t.field(rule["field"])
        allowed = list(f.values)
        for row in rows:
            v = row.values.get(f.key)
            if v and v not in allowed:
                expected = ", ".join(a.replace("_", " ") for a in allowed)
                self._add(row, rule, f.key, v, expected)

    def _bounds(self, rule, rows, keys):
        lo, hi = rule.get("min"), rule.get("max")
        exclusive = rule.get("exclusive", False)
        for row in rows:
            for key in keys:
                v = self._num(row, key)
                if v is None:
                    continue
                too_low = lo is not None and (v <= lo if exclusive else v < lo)
                too_high = hi is not None and v > hi
                if too_low or too_high:
                    expected = (
                        f"between {self._fmt(lo)} and {self._fmt(hi)}"
                        if lo is not None and hi is not None
                        else (f"above {self._fmt(lo)}" if lo is not None else f"at most {self._fmt(hi)}")
                    )
                    self._add(row, rule, key, self._fmt(v), expected)

    def check_range(self, rule, rows):
        self._bounds(rule, rows, rule["fields"])

    def check_min(self, rule, rows):
        self._bounds(rule, rows, [rule["field"]])

    def check_max(self, rule, rows):
        self._bounds(rule, rows, [rule["field"]])

    def check_sum_equals(self, rule, rows):
        for row in rows:
            values = [self._num(row, k) for k in rule["fields"]]
            if any(v is None for v in values):
                continue  # missing buckets are reported by the mandatory-field rule
            total = sum(values)
            if abs(total - rule["target"]) > rule["tolerance"]:
                self._add(row, rule, rule["fields"][0], self._fmt(total), self._fmt(rule["target"]))

    def check_less_or_equal(self, rule, rows):
        for row in rows:
            a, b = self._num(row, rule["field"]), self._num(row, rule["other"])
            if a is not None and b is not None and a > b + 1e-9:
                self._add(row, rule, rule["field"], self._fmt(a), self._fmt(b))

    def check_change_vs_previous(self, rule, rows):
        for row in rows:
            current = self._num(row, rule["field"])
            if current is None:
                continue
            lei = row.values.get("fund_lei")
            previous = self.ctx.previous_nav(lei if lei and lei_is_valid(lei) else None, row.key)
            if not previous:
                continue
            prev_value, prev_period = previous
            if not prev_value:
                continue
            change = (current / prev_value - 1) * 100
            if abs(change) > rule["threshold_pct"]:
                f = self.t.field(rule["field"])
                self._add(
                    row,
                    rule,
                    rule["field"],
                    f"{change:+.1f}%",
                    f"{display_value(prev_value, f)} on {prev_period.strftime('%d %b %Y')}",
                )


def validate(rows: list[NormalizedRow], template: Template, ctx: ValidationContext) -> list[FindingData]:
    return Validator(template, ctx).run(rows)


__all__ = ["FindingData", "RegisterFund", "ValidationContext", "validate", "value_text"]
