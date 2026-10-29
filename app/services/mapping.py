"""Column mapping: which client column feeds which template field.

Order of resolution for each column of an uploaded file:
  1. Saved mapping   - this client's column was confirmed before: reuse it.
  2. AI proposal     - Claude reads the header and sample values and proposes a
                       field, a unit transform, a confidence and a reason.
  3. Offline matcher - aliases + fuzzy matching, used when no API key is set or
                       the AI call fails.
Proposals are never applied silently: an analyst confirms them, and only then
are they saved for next time.
"""

import contextlib
import difflib
import logging
import re
from dataclasses import asdict, dataclass

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ClientMapping
from app.services.template import TRANSFORMS, Template, fold, header_key

log = logging.getLogger(__name__)

THOUSANDS_RE = re.compile(
    r"\b(k|keur|k€|kusd|000s?|'000|thousands?|milliers?|en milliers)\b|\(\s*[a-z€$£]{0,3}\s*k\s*\)|k€", re.I
)
MILLIONS_RE = re.compile(r"\b(m|mn|mio|meur|m€|musd|millions?)\b|\(\s*[a-z€$£]{0,3}\s*m\s*\)", re.I)


@dataclass
class Proposal:
    field: str | None
    transform: str = "none"
    source: str = "rules"  # saved | ai | rules | analyst
    confidence: float = 0.0
    reason: str = ""
    samples: list | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- helpers
def _samples(rows: list[dict], header: str, n: int = 3) -> list:
    return [r.get(header) for r in rows if r.get(header) is not None][:n]


def _numeric_samples(rows: list[dict], header: str) -> list[float]:
    out = []
    for r in rows:
        v = r.get(header)
        if isinstance(v, int | float) and not isinstance(v, bool):
            out.append(float(v))
        elif isinstance(v, str):
            with contextlib.suppress(ValueError):
                out.append(float(v.replace(",", ".").replace("%", "").strip()))
    return out


def detect_transform(header: str, field_type: str, rows: list[dict]) -> tuple[str, str]:
    """Infer a unit transform from the header text and sample values."""
    if field_type == "amount":
        if THOUSANDS_RE.search(header):
            return "thousands", "the header says the amounts are in thousands"
        if MILLIONS_RE.search(header):
            return "millions", "the header says the amounts are in millions"
    if field_type == "percent":
        values = _numeric_samples(rows, header)
        if values and all(0 <= v <= 1 for v in values) and any(v > 0 for v in values):
            return "fraction_to_percent", "the values are fractions (all between 0 and 1)"
    return "none", ""


def _clean_header(header: str) -> str:
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", str(header))
    text = fold(text)
    text = re.sub(r"\b(eur|usd|gbp|in|en|k|m|mn|keur|meur|000)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- offline matcher
class RuleBasedMapper:
    method = "rules"

    def propose(self, columns: list[str], rows: list[dict], template: Template) -> dict[str, Proposal]:
        candidates = []
        for header in columns:
            cleaned = _clean_header(header)
            if not cleaned:
                continue
            for f in template.fields:
                names = {fold(a) for a in f.aliases} | {fold(f.label), fold(f.key)}
                best, why = 0.0, ""
                for alias in names:
                    if not alias:
                        continue
                    if cleaned == alias:
                        score, reason = 0.97, f'matches the known column name "{alias}"'
                    elif alias in cleaned or cleaned in alias:
                        shorter, longer = sorted([alias, cleaned], key=len)
                        score = 0.6 + 0.3 * len(shorter) / len(longer)
                        reason = f'close to the known column name "{alias}"'
                    else:
                        ratio = difflib.SequenceMatcher(None, cleaned, alias).ratio()
                        score, reason = (ratio * 0.85, f'similar to "{alias}"') if ratio >= 0.75 else (0, "")
                    if score > best:
                        best, why = score, reason
                if best:
                    candidates.append((best, header, f, why))

        proposals: dict[str, Proposal] = {}
        used_fields: set[str] = set()
        for score, header, f, why in sorted(candidates, key=lambda c: -c[0]):
            if header in proposals or f.key in used_fields or score < 0.6:
                continue
            transform, t_reason = detect_transform(header, f.type, rows)
            reason = f"Header {why}" + (f"; {t_reason}" if t_reason else "") + "."
            proposals[header] = Proposal(f.key, transform, "rules", round(score, 2), reason, _samples(rows, header))
            used_fields.add(f.key)

        for header in columns:
            if header not in proposals:
                proposals[header] = Proposal(
                    None, "none", "rules", 0.0, "No template field matches this column.", _samples(rows, header)
                )
        return proposals


# --------------------------------------------------------------------------- AI mapper
MAPPING_TOOL = {
    "name": "propose_mapping",
    "description": "Propose how each client column maps to the reporting template.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mappings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "column": {"type": "string", "description": "Client column header, copied exactly"},
                        "field": {"type": ["string", "null"], "description": "Template field key, or null if the column is not needed"},
                        "transform": {"type": "string", "enum": list(TRANSFORMS)},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string", "description": "One short sentence a reviewer can check"},
                    },
                    "required": ["column", "field", "transform", "confidence", "reason"],
                },
            }
        },
        "required": ["mappings"],
    },
}  # fmt: skip

MAPPING_SYSTEM = """You map columns of a client's spreadsheet to the fields of a regulatory reporting template.
Rules:
- Use only the template field keys provided. Map each field at most once. Use null for columns that are not needed.
- Choose a transform only when the header or the sample values justify it:
  thousands / millions for amounts expressed in thousands or millions; fraction_to_percent when a
  percentage field contains fractions between 0 and 1.
- Headers can be in English or French. Be conservative: lower the confidence when unsure.
- Give a one-sentence reason that a reviewer can verify from the header and samples."""


class _AIMapping(BaseModel):
    column: str
    field: str | None
    transform: str = "none"
    confidence: float = Field(0.5, ge=0, le=1)
    reason: str = ""


class LLMMapper:
    method = "ai"

    def __init__(self, client=None, model: str | None = None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.client = client
        self.model = model or settings.anthropic_model

    def propose(self, columns: list[str], rows: list[dict], template: Template) -> dict[str, Proposal]:
        fields_desc = "\n".join(f"- {f.key} ({f.type}): {f.label}. {f.description}" for f in template.fields)
        cols_desc = "\n".join(f"- {c!r}: samples {_samples(rows, c)!r}" for c in columns)
        response = self.client.messages.create(
            model=self.model,
            max_tokens=3000,
            system=MAPPING_SYSTEM,
            tools=[MAPPING_TOOL],
            tool_choice={"type": "tool", "name": "propose_mapping"},
            messages=[
                {
                    "role": "user",
                    "content": f"<template_fields>\n{fields_desc}\n</template_fields>\n"
                    f"<client_columns>\n{cols_desc}\n</client_columns>",
                }
            ],
        )
        tool_use = next(b for b in response.content if b.type == "tool_use")
        valid_fields = set(template.by_key)
        proposals: dict[str, Proposal] = {}
        used: set[str] = set()
        for raw in tool_use.input.get("mappings", []):
            try:
                m = _AIMapping.model_validate(raw)
            except ValidationError:
                continue
            if m.column not in columns or m.column in proposals:
                continue
            field_key = m.field if m.field in valid_fields and m.field not in used else None
            transform = m.transform if m.transform in TRANSFORMS else "none"
            if field_key:
                used.add(field_key)
            proposals[m.column] = Proposal(
                field_key, transform, "ai", round(m.confidence, 2), m.reason, _samples(rows, m.column)
            )
        for c in columns:
            proposals.setdefault(c, Proposal(None, "none", "ai", 0.0, "Not mapped by the AI.", _samples(rows, c)))
        return proposals


# --------------------------------------------------------------------------- orchestration
def saved_mappings(db: Session, client_id: int) -> dict[str, ClientMapping]:
    return {m.header_key: m for m in db.scalars(select(ClientMapping).where(ClientMapping.client_id == client_id))}


def propose_mapping(
    db: Session, client_id: int, columns: list[str], rows: list[dict], template: Template, mapper=None
) -> tuple[dict[str, Proposal], str]:
    """Return (proposals per column, method used for the non-saved columns)."""
    saved = saved_mappings(db, client_id)
    result: dict[str, Proposal] = {}
    unknown = []
    for c in columns:
        m = saved.get(header_key(c))
        if m:
            when = m.confirmed_at.strftime("%d %b %Y")
            result[c] = Proposal(
                m.target_field,
                m.transform,
                "saved",
                1.0,
                f"Confirmed by {m.confirmed_by.full_name} on {when}; used {m.times_used} time(s).",
                _samples(rows, c),
            )
        else:
            unknown.append(c)

    method = "saved"
    if unknown:
        taken = {p.field for p in result.values() if p.field}
        proposals, method = _propose_unknown(unknown, rows, template, mapper)
        for c in unknown:
            p = proposals[c]
            if p.field in taken:  # a saved column already feeds this field
                p = Proposal(None, "none", p.source, 0.0, "Field already fed by another column.", p.samples)
            if p.field:
                taken.add(p.field)
            result[c] = p
    return result, method


def _propose_unknown(columns, rows, template, mapper):
    if mapper is not None:
        return mapper.propose(columns, rows, template), mapper.method
    if settings.llm_enabled:
        try:
            ai = LLMMapper()
            return ai.propose(columns, rows, template), ai.method
        except Exception as exc:  # network, quota, malformed output: never block intake
            log.warning("AI mapping failed, using offline matcher: %s", exc)
    rules = RuleBasedMapper()
    return rules.propose(columns, rows, template), rules.method
