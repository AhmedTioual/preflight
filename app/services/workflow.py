"""Filing lifecycle and controls.

    upload v1 ──> MAPPING ──confirm──> IN_PROGRESS ──submit──> IN_REVIEW ──approve──> APPROVED
                     ▲                     │  ▲                    │
                     └── new version ──────┘  └──── RETURNED <─────┘ return

Controls enforced here, so the web UI and the API behave the same:
  * only analysts (makers) upload, map, correct and submit; only reviewers approve or return;
  * a reviewer who took part in preparing the filing cannot approve or return it (four-eyes);
  * a filing cannot be submitted while any error remains;
  * every warning must be justified in writing before submission;
  * every action is written to the hash-chained audit log.
"""

import csv
import io
import json
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    CellOverride,
    Client,
    ClientMapping,
    ClientQuery,
    FileVersion,
    Filing,
    FilingStatus,
    Finding,
    FindingState,
    Fund,
    Role,
    Severity,
    User,
    utcnow,
)
from app.services.audit import log_action
from app.services.ingest import ParsedFile
from app.services.mapping import propose_mapping
from app.services.normalize import NormalizedRow, ParseError, display_value, normalize_rows, parse_value, value_text
from app.services.template import TRANSFORMS, Template, fold, header_key, lei_is_valid, load_template
from app.services.validation import RegisterFund, ValidationContext, validate

PREPARATION_ACTIONS = {
    "filing.created",
    "version.uploaded",
    "mapping.confirmed",
    "value.corrected",
    "warning.justified",
    "query.sent",
    "filing.submitted",
}


class WorkflowError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _require_maker(user: User) -> None:
    if user.role not in {Role.MAKER, Role.ADMIN}:
        raise WorkflowError("Only reporting analysts can prepare filings.", 403)


def _editable(filing: Filing) -> None:
    if filing.status not in {FilingStatus.MAPPING, FilingStatus.IN_PROGRESS, FilingStatus.RETURNED}:
        raise WorkflowError("This filing is locked while it is in review or after approval.")


def template_for(filing: Filing) -> Template:
    return load_template(filing.template_id)


# --------------------------------------------------------------------------- intake
def create_filing(db: Session, user: User, client: Client, period_end: date, parsed: ParsedFile, mapper=None) -> Filing:
    _require_maker(user)
    existing = db.scalar(select(Filing).where(Filing.client_id == client.id, Filing.period_end == period_end))
    if existing:
        raise WorkflowError(
            f"A filing for {client.name} and this period already exists (filing {existing.id}). "
            "Open it to upload a corrected file."
        )
    filing = Filing(client=client, period_end=period_end, created_by=user, status=FilingStatus.MAPPING)
    db.add(filing)
    db.flush()
    log_action(db, "filing.created", user, filing.id, client=client.code, period_end=period_end)
    _add_version(db, user, filing, parsed, mapper)
    db.commit()
    return filing


def add_version(db: Session, user: User, filing: Filing, parsed: ParsedFile, mapper=None) -> FileVersion:
    _require_maker(user)
    _editable(filing)
    if filing.current and filing.current.sha256 == parsed.sha256:
        raise WorkflowError("This file is identical to the current version.")
    version = _add_version(db, user, filing, parsed, mapper)
    db.commit()
    return version


def _add_version(db: Session, user: User, filing: Filing, parsed: ParsedFile, mapper) -> FileVersion:
    template = template_for(filing)
    number = len(filing.versions) + 1
    proposals, method = propose_mapping(db, filing.client_id, parsed.columns, parsed.rows, template, mapper)
    version = FileVersion(
        filing=filing,
        number=number,
        filename=parsed.filename,
        sha256=parsed.sha256,
        columns_json=json.dumps(parsed.columns),
        rows_json=json.dumps(parsed.rows, default=str),
        mapping_json=json.dumps({c: p.to_dict() for c, p in proposals.items()}, default=str),
        mapping_method=method,
        uploaded_by=user,
    )
    db.add(version)
    db.flush()
    log_action(
        db,
        "version.uploaded",
        user,
        filing.id,
        version=number,
        filename=parsed.filename,
        sha256=parsed.sha256,
        rows=len(parsed.rows),
    )
    all_saved = all(p.source == "saved" for p in proposals.values())
    if all_saved:
        # Every column is known from earlier confirmed files: no need to ask again.
        version.mapping_confirmed = True
        log_action(db, "mapping.reused", user, filing.id, version=number, columns=len(parsed.columns))
        _bump_usage(db, filing.client_id, parsed.columns)
        run_checks(db, filing, version)
        if filing.status in {FilingStatus.MAPPING, FilingStatus.RETURNED}:
            filing.status = FilingStatus.IN_PROGRESS
    else:
        filing.status = FilingStatus.MAPPING
    return version


def _bump_usage(db: Session, client_id: int, columns: list[str]) -> None:
    keys = {header_key(c) for c in columns}
    for m in db.scalars(select(ClientMapping).where(ClientMapping.client_id == client_id)):
        if m.header_key in keys:
            m.times_used += 1


def confirm_mapping(db: Session, user: User, filing: Filing, choices: dict[str, tuple[str | None, str]]) -> None:
    """`choices` is {column: (field key or None, transform)} as reviewed by the analyst."""
    _require_maker(user)
    _editable(filing)
    version = filing.current
    if version is None or version.mapping_confirmed:
        raise WorkflowError("There is no mapping waiting for confirmation.")
    template = template_for(filing)
    valid = set(template.by_key)
    seen: dict[str, str] = {}
    for column, (field_key, transform) in choices.items():
        if field_key and field_key not in valid:
            raise WorkflowError(f'Unknown template field for column "{column}".')
        if transform not in TRANSFORMS:
            raise WorkflowError(f'Unknown unit conversion for column "{column}".')
        if field_key:
            if field_key in seen:
                label = template.field(field_key).label
                raise WorkflowError(f'"{label}" is mapped twice: from "{seen[field_key]}" and "{column}".')
            seen[field_key] = column

    mapping = version.mapping
    changed = 0
    for column in version.columns:
        field_key, transform = choices.get(column, (None, "none"))
        proposal = mapping.get(column, {})
        if (proposal.get("field"), proposal.get("transform", "none")) != (field_key, transform):
            changed += 1
            proposal.update(source="analyst", confidence=1.0, reason=f"Set by {user.full_name}.")
        proposal.update(field=field_key, transform=transform)
        mapping[column] = proposal
        _save_mapping(db, filing.client_id, column, field_key, transform, user)
    version.mapping_json = json.dumps(mapping, default=str)
    version.mapping_confirmed = True
    missing = [template.field(f).label for f in valid - set(seen) if template.field(f).required]
    log_action(
        db,
        "mapping.confirmed",
        user,
        filing.id,
        version=version.number,
        columns=len(version.columns),
        changed_by_analyst=changed,
        unmapped_required=missing,
    )
    run_checks(db, filing, version)
    filing.status = FilingStatus.IN_PROGRESS
    db.commit()


def _save_mapping(db: Session, client_id: int, column: str, field_key, transform, user: User) -> None:
    key = header_key(column)
    existing = db.scalar(
        select(ClientMapping).where(ClientMapping.client_id == client_id, ClientMapping.header_key == key)
    )
    if existing:
        existing.target_field, existing.transform = field_key, transform
        existing.confirmed_by, existing.confirmed_at = user, utcnow()
        existing.times_used += 1
    else:
        db.add(
            ClientMapping(
                client_id=client_id,
                header_key=key,
                header=column,
                target_field=field_key,
                transform=transform,
                confirmed_by=user,
            )
        )


# --------------------------------------------------------------------------- checks
def _context(db: Session, filing: Filing) -> ValidationContext:
    register = [
        RegisterFund(f.name, f.lei, f.last_reported_nav, f.last_reported_period)
        for f in db.scalars(select(Fund).where(Fund.client_id == filing.client_id))
    ]
    previous = db.scalar(
        select(Filing)
        .where(
            Filing.client_id == filing.client_id,
            Filing.period_end < filing.period_end,
            Filing.status == FilingStatus.APPROVED,
        )
        .order_by(Filing.period_end.desc())
        .limit(1)
    )
    prev_rows = [NormalizedRow.from_dict(r) for r in previous.current.normalized] if previous else []
    by_lei = {f.lei: f for f in register}
    by_name = {fold(f.name): f for f in register}

    def previous_nav(lei, key):
        for r in prev_rows:
            if ((lei and r.values.get("fund_lei") == lei) or r.key == key) and r.values.get("nav"):
                return r.values["nav"], previous.period_end
        fund = by_lei.get(lei) if lei else None
        fund = fund or next((f for n, f in by_name.items() if n == key), None)
        if fund and fund.last_nav and fund.last_period and fund.last_period < filing.period_end:
            return fund.last_nav, fund.last_period
        return None

    return ValidationContext(filing.period_end, filing.client.name, register, previous_nav)


def _finding_key(f) -> tuple:
    return (f.fund_key, f.rule_id, f.field, f.value_text)


def run_checks(db: Session, filing: Filing, version: FileVersion) -> list[Finding]:
    """Normalise, apply corrections and validate. Keeps reviewer decisions that still apply."""
    template = template_for(filing)
    rows = normalize_rows(version.rows, version.mapping, template, filing.overrides)
    version.normalized_json = json.dumps([r.to_dict() for r in rows], default=str)
    results = validate(rows, template, _context(db, filing))

    # Decisions to keep: from this version (re-run after a correction) or, for justified
    # warnings, from the previous version when the same value is still reported.
    same_version = {_finding_key(f): f for f in version.findings}
    previous_version = next((v for v in filing.versions if v.number == version.number - 1), None)
    carried = {
        _finding_key(f): f
        for f in (previous_version.findings if previous_version else [])
        if f.state == FindingState.JUSTIFIED
    }

    version.findings.clear()
    db.flush()
    for r in results:
        finding = Finding(
            row_index=r.row_index,
            fund_key=r.fund_key,
            fund_label=r.fund_label,
            field=r.field,
            rule_id=r.rule_id,
            severity=r.severity,
            value_text=r.value_text,
            expected_text=r.expected_text,
            message=r.message,
            suggestion=r.suggestion,
        )
        key = _finding_key(r)
        if key in same_version:
            old = same_version[key]
            finding.state, finding.note, finding.actor_id = old.state, old.note, old.actor_id
            finding.updated_at, finding.carried_over = old.updated_at, old.carried_over
        elif key in carried and r.severity == Severity.WARNING:
            old = carried[key]
            finding.state, finding.note, finding.actor_id = FindingState.JUSTIFIED, old.note, old.actor_id
            finding.updated_at, finding.carried_over = old.updated_at, True
        version.findings.append(finding)
    if version.checked_at is None:
        version.initial_errors = sum(1 for f in version.findings if f.severity == Severity.ERROR)
        version.initial_warnings = sum(1 for f in version.findings if f.severity == Severity.WARNING)
    version.checked_at = utcnow()
    db.flush()
    return version.findings


# --------------------------------------------------------------------------- analyst actions
def correct_value(
    db: Session, user: User, filing: Filing, fund_key: str, field_key: str, new_value: str, note: str
) -> CellOverride:
    _require_maker(user)
    _editable(filing)
    if filing.status == FilingStatus.MAPPING:
        raise WorkflowError("Confirm the column mapping first.")
    if not note.strip():
        raise WorkflowError("Explain the correction: it is shown to the reviewer and kept in the audit trail.")
    template = template_for(filing)
    if field_key not in template.by_key:
        raise WorkflowError("Unknown field.")
    f = template.field(field_key)
    try:
        parse_value(new_value, f)
    except ParseError as exc:
        raise WorkflowError(f'"{new_value}" is not a valid {f.type_label}.') from exc

    version = filing.current
    row = next((NormalizedRow.from_dict(r) for r in version.normalized if r["key"] == fund_key), None)
    if row is None:
        raise WorkflowError("Fund not found in this file.")
    if field_key in row.overridden:
        original = row.overridden[field_key]["from"]
        filing.overrides[:] = [o for o in filing.overrides if not (o.fund_key == fund_key and o.field == field_key)]
        db.flush()
    else:
        original = row.format_errors.get(field_key, value_text(row.values.get(field_key)))
    override = CellOverride(
        filing=filing,
        fund_key=fund_key,
        field=field_key,
        original_value=original,
        new_value=new_value.strip(),
        note=note.strip(),
        user=user,
    )
    db.add(override)
    db.flush()
    db.refresh(filing)
    log_action(
        db,
        "value.corrected",
        user,
        filing.id,
        fund=row.label,
        field=f.label,
        old=original,
        new=new_value.strip(),
        note=note.strip(),
    )
    run_checks(db, filing, version)
    db.commit()
    return override


def justify_warning(db: Session, user: User, finding: Finding, note: str) -> Finding:
    _require_maker(user)
    filing = finding.version.filing
    _editable(filing)
    if finding.version.id != filing.current.id:
        raise WorkflowError("This finding belongs to an older version.")
    if finding.severity != Severity.WARNING:
        raise WorkflowError("Errors cannot be justified: correct the value or ask the client for corrected data.")
    if not note.strip():
        raise WorkflowError("Write the justification: it is shown to the reviewer.")
    finding.state, finding.note, finding.actor, finding.updated_at = (
        FindingState.JUSTIFIED,
        note.strip(),
        user,
        utcnow(),
    )
    finding.carried_over = False
    log_action(
        db,
        "warning.justified",
        user,
        filing.id,
        fund=finding.fund_label,
        rule=finding.rule_id,
        value=finding.value_text,
        note=note.strip(),
    )
    db.commit()
    return finding


# --------------------------------------------------------------------------- client query
@dataclass
class QueryDraft:
    subject: str
    body: str
    finding_ids: list[int]


def draft_client_query(filing: Filing, sender: User) -> QueryDraft:
    """Group every open point into one clear message to the client."""
    version = filing.current
    template = template_for(filing)
    points = [f for f in version.findings if f.state == FindingState.OPEN]
    by_fund: dict[str, list[Finding]] = {}
    for f in sorted(points, key=lambda f: (f.row_index, f.severity != Severity.ERROR)):
        by_fund.setdefault(f.fund_label, []).append(f)

    period = filing.period_end.strftime("%d %B %Y")
    first_name = (filing.client.contact_name or "").split(" ")[0] or "team"
    lines = [
        f"Dear {first_name},",
        "",
        f'Thank you for the data for the period ending {period} (file "{version.filename}"). '
        f"Before we can file, we need your help on {len(points)} point{'s' if len(points) != 1 else ''}:",
        "",
    ]
    for fund, items in by_fund.items():
        lines.append(fund)
        for f in items:
            rule = template.rule(f.rule_id)
            question = rule.get("ask", rule["explain"]).format(
                label=template.field(f.field).label,
                value=f.value_text,
                expected=f.expected_text,
                client=filing.client.name,
                type_label=template.field(f.field).type_label,
            )
            lines.append(f"  - {question}")
        lines.append("")
    lines += [
        "The simplest way is to send back the corrected file in the same format; we will re-run all checks on it.",
        "",
        "Kind regards,",
        sender.full_name,
    ]
    count = len(points)
    subject = (
        f"Fund return {filing.period_end.strftime('%d %b %Y')}: {count} point{'s' if count != 1 else ''} to review"
    )
    return QueryDraft(subject, "\n".join(lines), [f.id for f in points])


def send_client_query(db: Session, user: User, filing: Filing, subject: str, body: str) -> ClientQuery:
    _require_maker(user)
    _editable(filing)
    version = filing.current
    open_points = [f for f in version.findings if f.state == FindingState.OPEN]
    if not open_points:
        raise WorkflowError("There are no open points to send to the client.")
    if not body.strip():
        raise WorkflowError("The message is empty.")
    for f in open_points:
        f.state, f.actor, f.updated_at = FindingState.QUERIED, user, utcnow()
    query = ClientQuery(
        filing=filing,
        version_number=version.number,
        subject=subject.strip() or "Questions on your fund return",
        body=body.strip(),
        finding_count=len(open_points),
        sent_by=user,
    )
    db.add(query)
    log_action(db, "query.sent", user, filing.id, version=version.number, points=len(open_points))
    db.commit()
    return query


# --------------------------------------------------------------------------- four-eyes
def submission_blockers(filing: Filing) -> list[str]:
    version = filing.current
    if filing.status == FilingStatus.MAPPING or version is None or not version.mapping_confirmed:
        return ["Confirm the column mapping."]
    c = version.counts()
    blockers = []
    if c["errors"]:
        blockers.append(f"{c['errors']} error{'s' if c['errors'] > 1 else ''} to correct")
    unjustified = c["warnings"] - c["justified"]
    if unjustified:
        blockers.append(f"{unjustified} warning{'s' if unjustified > 1 else ''} to justify")
    return blockers


def submit_filing(db: Session, user: User, filing: Filing) -> Filing:
    _require_maker(user)
    if filing.status not in {FilingStatus.IN_PROGRESS, FilingStatus.RETURNED}:
        raise WorkflowError("Only filings in progress can be submitted.")
    blockers = submission_blockers(filing)
    if blockers:
        raise WorkflowError("Not ready for review: " + "; ".join(blockers) + ".")
    filing.status = FilingStatus.IN_REVIEW
    filing.submitted_at = utcnow()
    filing.reviewed_by = None
    filing.reviewed_at = None
    log_action(db, "filing.submitted", user, filing.id, version=filing.current.number, **filing.current.counts())
    db.commit()
    return filing


def preparers(db: Session, filing: Filing) -> set[int]:
    """Everyone who took part in preparing this filing."""
    ids = set(
        db.scalars(
            select(AuditLog.user_id).where(AuditLog.filing_id == filing.id, AuditLog.action.in_(PREPARATION_ACTIONS))
        )
    )
    ids.add(filing.created_by_id)
    return {i for i in ids if i is not None}


def review_blocker(db: Session, user: User, filing: Filing) -> str | None:
    if user.role not in {Role.CHECKER, Role.ADMIN}:
        return "Only reviewers can approve or return filings."
    if user.id in preparers(db, filing):
        return "Four-eyes principle: you took part in preparing this filing, so someone else must review it."
    if filing.status != FilingStatus.IN_REVIEW:
        return "This filing is not waiting for review."
    return None


def approve_filing(db: Session, user: User, filing: Filing, comment: str = "") -> Filing:
    if blocker := review_blocker(db, user, filing):
        raise WorkflowError(blocker, 403 if "four-eyes" in blocker.lower() or "Only" in blocker else 400)
    filing.status = FilingStatus.APPROVED
    filing.reviewed_by, filing.reviewed_at = user, utcnow()
    filing.review_comment = comment.strip() or None
    log_action(db, "filing.approved", user, filing.id, version=filing.current.number, comment=comment.strip())
    db.commit()
    return filing


# --------------------------------------------------------------------------- client sign-off and filing
def draft_signoff_request(filing: Filing, sender: User) -> QueryDraft:
    """Cover message sending the approved return to the client for sign-off."""
    template = template_for(filing)
    version = filing.current
    c = version.counts()
    first_name = (filing.client.contact_name or "").split(" ")[0] or "team"
    period = filing.period_end.strftime("%d %B %Y")
    lines = [
        f"Dear {first_name},",
        "",
        f"Please find attached the fund return for the period ending {period}, prepared from your file "
        f'"{version.filename}" and checked against {len(template.rules)} validation rules.',
        "",
        "Summary:",
        f"  - Funds reported: {len(version.rows)}",
        f"  - File versions received: {len(filing.versions)}",
        f"  - Values corrected by us: {len(filing.overrides)} (details in the validation record)",
        f"  - Warnings confirmed with you: {c['justified']}",
        "",
        f"As the reporting entity, {filing.client.name} remains responsible for the content of this return. "
        f"Please confirm that we can file it with the {template.regulator}, or tell us what to change.",
        "",
        "Kind regards,",
        sender.full_name,
    ]
    subject = f"For your approval: fund return {filing.period_end.strftime('%d %b %Y')}"
    return QueryDraft(subject, "\n".join(lines), [])


def send_to_client(db: Session, user: User, filing: Filing, subject: str = "", body: str = "") -> Filing:
    _require_maker(user)
    if filing.status != FilingStatus.APPROVED:
        raise WorkflowError("Only filings approved by a reviewer can be sent to the client.")
    filing.status = FilingStatus.SENT_TO_CLIENT
    filing.sent_to_client_by, filing.sent_to_client_at = user, utcnow()
    filing.client_decision = filing.client_decision_by = filing.client_comment = None
    filing.client_decision_at = None
    log_action(
        db,
        "client.signoff_requested",
        user,
        filing.id,
        version=filing.current.number,
        to=filing.client.contact_email,
        subject=subject.strip(),
    )
    db.commit()
    return filing


def record_client_decision(
    db: Session, user: User, filing: Filing, approved: bool, contact_name: str, comment: str = ""
) -> Filing:
    """Record the client's answer. A change request sends the filing back to the analyst,
    and it must then go through the four-eyes review again."""
    _require_maker(user)
    if filing.status != FilingStatus.SENT_TO_CLIENT:
        raise WorkflowError("This filing is not waiting for the client's sign-off.")
    if not contact_name.strip():
        raise WorkflowError("Enter the name of the person at the client who answered.")
    if not approved and not comment.strip():
        raise WorkflowError("Describe the changes the client asked for.")
    filing.client_decision = "approved" if approved else "changes"
    filing.client_decision_by = contact_name.strip()
    filing.client_decision_at = utcnow()
    filing.client_comment = comment.strip() or None
    filing.client_decision_recorded_by = user
    if approved:
        filing.status = FilingStatus.CLIENT_APPROVED
        log_action(db, "client.approved", user, filing.id, contact=contact_name.strip(), comment=comment.strip())
    else:
        filing.status = FilingStatus.RETURNED
        filing.reviewed_by, filing.reviewed_at = None, None
        filing.review_comment = comment.strip()
        log_action(
            db, "client.changes_requested", user, filing.id, contact=contact_name.strip(), comment=comment.strip()
        )
    db.commit()
    return filing


def record_filed(db: Session, user: User, filing: Filing, reference: str) -> Filing:
    """Record that the return was submitted to the regulator through the official channel."""
    _require_maker(user)
    if filing.status != FilingStatus.CLIENT_APPROVED:
        raise WorkflowError("A filing can be recorded as filed only after the client's sign-off.")
    if not reference.strip():
        raise WorkflowError("Enter the submission reference given by the regulator's channel.")
    filing.status = FilingStatus.FILED
    filing.filed_by, filing.filed_at = user, utcnow()
    filing.filing_reference = reference.strip()
    log_action(
        db, "filing.filed", user, filing.id, reference=reference.strip(), regulator=template_for(filing).regulator
    )
    db.commit()
    return filing


def return_filing(db: Session, user: User, filing: Filing, comment: str) -> Filing:
    if blocker := review_blocker(db, user, filing):
        raise WorkflowError(blocker, 403 if "four-eyes" in blocker.lower() or "Only" in blocker else 400)
    if not comment.strip():
        raise WorkflowError("Tell the analyst what to change.")
    filing.status = FilingStatus.RETURNED
    filing.reviewed_by, filing.reviewed_at = user, utcnow()
    filing.review_comment = comment.strip()
    log_action(db, "filing.returned", user, filing.id, version=filing.current.number, comment=comment.strip())
    db.commit()
    return filing


# --------------------------------------------------------------------------- outputs
def version_diff(filing: Filing, version: FileVersion) -> list[dict]:
    """Values that changed compared with the previous version."""
    previous = next((v for v in filing.versions if v.number == version.number - 1), None)
    if previous is None or not previous.mapping_confirmed or not version.mapping_confirmed:
        return []
    template = template_for(filing)
    before = {r["key"]: r for r in previous.normalized}
    changes = []
    for row in version.normalized:
        old = before.get(row["key"])
        for f in template.fields:
            new_v = row["values"].get(f.key)
            if old is None:
                continue
            old_v = old["values"].get(f.key)
            if value_text(old_v) != value_text(new_v):
                changes.append(
                    {
                        "fund_key": row["key"],
                        "fund": row["label"],
                        "field": f.key,
                        "label": f.label,
                        "old": display_value(old_v, f) or "empty",
                        "new": display_value(new_v, f) or "empty",
                    }
                )
    return changes


def export_csv(filing: Filing) -> str:
    """Filing-ready file: one row per fund, template field order, normalised values."""
    template = template_for(filing)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([f.key for f in template.fields])
    for row in filing.current.normalized:
        writer.writerow(["" if row["values"].get(f.key) is None else row["values"][f.key] for f in template.fields])
    return buffer.getvalue()


def first_time_right(filing: Filing) -> bool | None:
    """Did the client's first file pass with no errors?"""
    first = filing.versions[0] if filing.versions else None
    if first is None or first.initial_errors is None:
        return None
    return first.initial_errors == 0


__all__ = ["WorkflowError", "lei_is_valid"]
