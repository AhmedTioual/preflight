"""View models for templates: turns a file version into the review grid."""

from dataclasses import dataclass, field

from app.models import FileVersion, Filing, Finding, FindingState, Severity
from app.services.normalize import display_value, value_text
from app.services.template import TRANSFORMS, Template
from app.services.workflow import version_diff

STATUS_LABELS = {
    "mapping": "Mapping",
    "in_progress": "In progress",
    "in_review": "In review",
    "approved": "Approved",
    "returned": "Returned",
    "sent_to_client": "With client",
    "client_approved": "Client approved",
    "filed": "Filed",
}

STATE_LABELS = {"open": "Open", "queried": "Asked client", "justified": "Justified"}

FIELD_GROUPS = {
    "fund_name": "Identification",
    "nav": "Size and leverage",
    "liq_1d": "Liquidity profile",
    "investor_count": "Investors",
}

SOURCE_LABELS = {"saved": "Saved", "ai": "AI proposal", "rules": "Matched", "analyst": "Set by analyst"}


@dataclass
class Cell:
    fund_key: str
    field: str
    value: str
    raw: str
    state: str = "ok"  # ok | error | warning | justified | queried | corrected | changed
    findings: list[Finding] = field(default_factory=list)
    related: list[Finding] = field(default_factory=list)  # findings on a rule spanning this cell
    corrected: dict | None = None
    changed_from: str | None = None
    edit_value: str = ""
    related_fields: list[str] = field(default_factory=list)
    missing: bool = False


@dataclass
class Grid:
    funds: list[dict]  # {key, label, index}
    rows: list[dict]  # {field: FieldDef, cells: [Cell]}
    findings_by_id: dict[int, Finding]


def cell_state(cell: Cell) -> str:
    def worst(items):
        if any(f.severity == Severity.ERROR and f.state == FindingState.QUERIED for f in items):
            return "queried"
        if any(f.severity == Severity.ERROR for f in items):
            return "error"
        if any(f.severity == Severity.WARNING and f.state != FindingState.JUSTIFIED for f in items):
            return "warning"
        if items:
            return "justified"
        return None

    return (
        worst(cell.findings)
        or worst(cell.related)
        or ("corrected" if cell.corrected else "changed" if cell.changed_from is not None else "ok")
    )


def build_grid(filing: Filing, version: FileVersion, template: Template) -> Grid:
    rows = version.normalized
    funds = [{"key": r["key"], "label": r["label"], "index": r["index"]} for r in rows]
    changes = {(c["fund_key"], c["field"]): c["old"] for c in version_diff(filing, version)}

    direct: dict[tuple, list[Finding]] = {}
    related: dict[tuple, list[Finding]] = {}
    for f in version.findings:
        direct.setdefault((f.fund_key, f.field), []).append(f)
        for other in template.rule_fields(f.rule_id, f.field):
            if other != f.field:
                related.setdefault((f.fund_key, other), []).append(f)

    grid_rows = []
    for fd in template.fields:
        cells = []
        for r in rows:
            key = (r["key"], fd.key)
            value = r["values"].get(fd.key)
            shown = display_value(value, fd) if value is not None else r["format_errors"].get(fd.key, "")
            cell = Cell(
                fund_key=r["key"],
                field=fd.key,
                value=shown,
                raw=r["raw"].get(fd.key, ""),
                findings=direct.get(key, []),
                related=related.get(key, []),
                corrected=r["overridden"].get(fd.key),
                changed_from=changes.get(key),
                edit_value=r["format_errors"].get(fd.key, value_text(value)),
                missing=value is None and fd.key not in r["format_errors"],
            )
            rel = []
            for f in cell.findings + cell.related:
                rel += [x for x in template.rule_fields(f.rule_id, f.field) if x != fd.key]
            cell.related_fields = sorted(set(rel))
            cell.state = cell_state(cell)
            cells.append(cell)
        grid_rows.append({"field": fd, "cells": cells})
    return Grid(funds, grid_rows, {f.id: f for f in version.findings})


def mapping_rows(version: FileVersion, template: Template) -> list[dict]:
    rows = []
    for column in version.columns:
        p = version.mapping.get(column, {})
        rows.append(
            {
                "column": column,
                "field": p.get("field"),
                "transform": p.get("transform", "none"),
                "source": p.get("source", "rules"),
                "confidence": p.get("confidence", 0) or 0,
                "reason": p.get("reason", ""),
                "samples": [str(s) for s in (p.get("samples") or [])],
            }
        )
    return rows


__all__ = ["FIELD_GROUPS", "STATE_LABELS", "STATUS_LABELS", "SOURCE_LABELS", "TRANSFORMS", "build_grid", "mapping_rows"]
