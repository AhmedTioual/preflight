"""Tamper-evident audit trail.

Every entry stores `prev_hash` (hash of the previous entry) and `entry_hash`
(SHA-256 over its own content + prev_hash). `verify_chain` recomputes the
chain; any edited, inserted or deleted row is detected.
"""

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, User, utcnow

GENESIS = "0" * 64


def _compute_hash(entry: AuditLog) -> str:
    payload = json.dumps(
        {
            "timestamp": entry.timestamp.isoformat(),
            "user_id": entry.user_id,
            "action": entry.action,
            "filing_id": entry.filing_id,
            "details": entry.details,
            "prev_hash": entry.prev_hash,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def log_action(
    db: Session,
    action: str,
    user: User | None = None,
    filing_id: int | None = None,
    **details,
) -> AuditLog:
    last = db.scalar(select(AuditLog).order_by(AuditLog.id.desc()).limit(1))
    entry = AuditLog(
        timestamp=utcnow(),
        user_id=user.id if user else None,
        action=action,
        filing_id=filing_id,
        details=json.dumps(details, default=str, sort_keys=True),
        prev_hash=last.entry_hash if last else GENESIS,
    )
    entry.entry_hash = _compute_hash(entry)
    db.add(entry)
    db.flush()
    return entry


@dataclass
class ChainStatus:
    valid: bool
    entries: int
    broken_at: int | None = None


def verify_chain(db: Session) -> ChainStatus:
    expected_prev = GENESIS
    entries = db.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    for entry in entries:
        if entry.prev_hash != expected_prev or _compute_hash(entry) != entry.entry_hash:
            return ChainStatus(valid=False, entries=len(entries), broken_at=entry.id)
        expected_prev = entry.entry_hash
    return ChainStatus(valid=True, entries=len(entries))
