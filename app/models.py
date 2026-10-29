"""Data model.

Reference data   : clients, funds (the client's fund register), users.
Filing workflow  : filings -> file versions -> findings, cell overrides, client queries.
Learning         : client_mappings (confirmed column mappings, reused next time).
Control          : audit_log (hash-chained, append-only).
"""

import json
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Role(StrEnum):
    MAKER = "maker"  # reporting analyst
    CHECKER = "checker"  # reviewer
    ADMIN = "admin"


class FilingStatus(StrEnum):
    MAPPING = "mapping"  # columns need confirmation
    IN_PROGRESS = "in_progress"  # checked, analyst resolving findings
    IN_REVIEW = "in_review"  # submitted for four-eyes review
    APPROVED = "approved"  # approved internally (four-eyes)
    RETURNED = "returned"  # back to the analyst (by the reviewer or at the client's request)
    SENT_TO_CLIENT = "sent_to_client"  # waiting for the client's sign-off
    CLIENT_APPROVED = "client_approved"  # signed off by the client, ready to file
    FILED = "filed"  # recorded as submitted to the regulator


# Statuses in which the content is final and the filing file can be downloaded.
RELEASED_STATUSES = {"approved", "sent_to_client", "client_approved", "filed"}


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class FindingState(StrEnum):
    OPEN = "open"
    QUERIED = "queried"  # sent to the client, waiting for corrected data
    JUSTIFIED = "justified"  # warning accepted with a written reason


# --------------------------------------------------------------------------- reference data
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    full_name: Mapped[str] = mapped_column(String(120))
    job_title: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(255))

    @property
    def initials(self) -> str:
        parts = self.full_name.split()
        return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    client_type: Mapped[str] = mapped_column(String(60), default="Alternative investment fund manager")
    contact_name: Mapped[str] = mapped_column(String(120), default="")
    contact_email: Mapped[str] = mapped_column(String(120), default="")
    language: Mapped[str] = mapped_column(String(5), default="en")

    funds: Mapped[list["Fund"]] = relationship(back_populates="client", order_by="Fund.name")
    mappings: Mapped[list["ClientMapping"]] = relationship(back_populates="client", order_by="ClientMapping.id")
    filings: Mapped[list["Filing"]] = relationship(back_populates="client", order_by="Filing.period_end.desc()")


class Fund(Base):
    """The client's fund register, used to cross-check filings."""

    __tablename__ = "funds"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    name: Mapped[str] = mapped_column(String(160))
    lei: Mapped[str] = mapped_column(String(20), unique=True)
    base_currency: Mapped[str] = mapped_column(String(3))
    strategy: Mapped[str] = mapped_column(String(40))
    # Last NAV reported before onboarding (imported from the previous provider),
    # used for the period-on-period plausibility check on the first filing.
    last_reported_nav: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_reported_period: Mapped[date | None] = mapped_column(Date, nullable=True)

    client: Mapped[Client] = relationship(back_populates="funds")


class ClientMapping(Base):
    """A confirmed mapping from a client's column header to a template field."""

    __tablename__ = "client_mappings"
    __table_args__ = (UniqueConstraint("client_id", "header_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    header_key: Mapped[str] = mapped_column(String(200))  # normalised header
    header: Mapped[str] = mapped_column(String(200))  # as the client writes it
    target_field: Mapped[str | None] = mapped_column(String(60), nullable=True)  # None = ignored column
    transform: Mapped[str] = mapped_column(String(30), default="none")
    confirmed_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    times_used: Mapped[int] = mapped_column(Integer, default=1)

    client: Mapped[Client] = relationship(back_populates="mappings")
    confirmed_by: Mapped[User] = relationship()


# --------------------------------------------------------------------------- filing workflow
class Filing(Base):
    __tablename__ = "filings"
    __table_args__ = (UniqueConstraint("client_id", "period_end", "template_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    template_id: Mapped[str] = mapped_column(String(40), default="fund_return_v1")
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default=FilingStatus.MAPPING)

    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Client sign-off (the asset manager stays responsible for its regulatory reports)
    sent_to_client_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_to_client_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    client_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)  # approved | changes
    client_decision_by: Mapped[str | None] = mapped_column(String(120), nullable=True)  # client contact name
    client_decision_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    client_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_decision_recorded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    # Submission to the regulator (recorded, not performed, by the application)
    filed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    filed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    filing_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)

    client: Mapped[Client] = relationship(back_populates="filings")
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])
    reviewed_by: Mapped[User | None] = relationship(foreign_keys=[reviewed_by_id])
    sent_to_client_by: Mapped[User | None] = relationship(foreign_keys=[sent_to_client_by_id])
    client_decision_recorded_by: Mapped[User | None] = relationship(foreign_keys=[client_decision_recorded_by_id])
    filed_by: Mapped[User | None] = relationship(foreign_keys=[filed_by_id])
    versions: Mapped[list["FileVersion"]] = relationship(
        back_populates="filing", order_by="FileVersion.number", cascade="all, delete-orphan"
    )
    overrides: Mapped[list["CellOverride"]] = relationship(
        back_populates="filing", order_by="CellOverride.id", cascade="all, delete-orphan"
    )
    queries: Mapped[list["ClientQuery"]] = relationship(
        back_populates="filing", order_by="ClientQuery.id", cascade="all, delete-orphan"
    )

    @property
    def current(self) -> "FileVersion | None":
        return self.versions[-1] if self.versions else None

    @property
    def title(self) -> str:
        return f"{self.client.name}, period ending {self.period_end.strftime('%d %b %Y')}"


class FileVersion(Base):
    __tablename__ = "file_versions"
    __table_args__ = (UniqueConstraint("filing_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id"))
    number: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    columns_json: Mapped[str] = mapped_column(Text)  # list of headers, in file order
    rows_json: Mapped[str] = mapped_column(Text)  # list of {header: raw value}
    mapping_json: Mapped[str] = mapped_column(Text, default="{}")  # header -> {field, transform, source, ...}
    mapping_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    mapping_method: Mapped[str] = mapped_column(String(20), default="rules")
    normalized_json: Mapped[str] = mapped_column(Text, default="[]")  # list of {field: value}
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Findings at the first check, before any correction: measures the client's data quality.
    initial_errors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    initial_warnings: Mapped[int | None] = mapped_column(Integer, nullable=True)

    filing: Mapped[Filing] = relationship(back_populates="versions")
    uploaded_by: Mapped[User] = relationship()
    findings: Mapped[list["Finding"]] = relationship(
        back_populates="version", order_by="Finding.id", cascade="all, delete-orphan"
    )

    # JSON helpers
    @property
    def columns(self) -> list[str]:
        return json.loads(self.columns_json)

    @property
    def rows(self) -> list[dict]:
        return json.loads(self.rows_json)

    @property
    def mapping(self) -> dict:
        return json.loads(self.mapping_json)

    @property
    def normalized(self) -> list[dict]:
        return json.loads(self.normalized_json)

    def counts(self) -> dict[str, int]:
        c = {"errors": 0, "warnings": 0, "open_errors": 0, "open_warnings": 0, "queried": 0, "justified": 0}
        for f in self.findings:
            if f.severity == Severity.ERROR:
                c["errors"] += 1
                if f.state == FindingState.OPEN:
                    c["open_errors"] += 1
            else:
                c["warnings"] += 1
                if f.state == FindingState.OPEN:
                    c["open_warnings"] += 1
            if f.state == FindingState.QUERIED:
                c["queried"] += 1
            if f.state == FindingState.JUSTIFIED:
                c["justified"] += 1
        c["total"] = len(self.findings)
        return c


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("file_versions.id"))
    row_index: Mapped[int] = mapped_column(Integer)
    fund_key: Mapped[str] = mapped_column(String(200))
    fund_label: Mapped[str] = mapped_column(String(200))
    field: Mapped[str] = mapped_column(String(60))
    rule_id: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10))
    value_text: Mapped[str] = mapped_column(String(200), default="")
    expected_text: Mapped[str] = mapped_column(String(200), default="")
    message: Mapped[str] = mapped_column(Text)
    suggestion: Mapped[str | None] = mapped_column(String(200), nullable=True)

    state: Mapped[str] = mapped_column(String(20), default=FindingState.OPEN)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    carried_over: Mapped[bool] = mapped_column(Boolean, default=False)

    version: Mapped[FileVersion] = relationship(back_populates="findings")
    actor: Mapped[User | None] = relationship()


class CellOverride(Base):
    """An analyst correction of a value, with the reason. Applied on top of the client's data."""

    __tablename__ = "cell_overrides"

    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id"))
    fund_key: Mapped[str] = mapped_column(String(200))
    field: Mapped[str] = mapped_column(String(60))
    original_value: Mapped[str] = mapped_column(String(200))
    new_value: Mapped[str] = mapped_column(String(200))
    note: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    filing: Mapped[Filing] = relationship(back_populates="overrides")
    user: Mapped[User] = relationship()


class ClientQuery(Base):
    __tablename__ = "client_queries"

    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    finding_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    filing: Mapped[Filing] = relationship(back_populates="queries")
    sent_by: Mapped[User] = relationship()


# --------------------------------------------------------------------------- audit
class AuditLog(Base):
    """Append-only, hash-chained log: each entry's hash covers the previous
    entry's hash, so editing or deleting a past row breaks the chain."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(50))
    filing_id: Mapped[int | None] = mapped_column(ForeignKey("filings.id"), nullable=True)
    details: Mapped[str] = mapped_column(Text, default="{}")
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64))

    user: Mapped[User | None] = relationship()
