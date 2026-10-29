"""Seed demo users, clients, fund registers and some filing history.

History is created through the real workflow (upload, mapping, checks,
justification, four-eyes approval), so the audit trail is genuine:
  * Riverbend Gestion, March 2026: approved.
  * Riverbend Gestion, June 2026: submitted, waiting in the reviewer's queue.
Northstar Capital is a new client with no history: its first file is the live demo.

Usage:  python -m app.seed            (idempotent)
        python -m app.seed --reset    (drop and rebuild)
"""

import sys
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import demo_data as d
from app.db import Base, SessionLocal, engine, init_db
from app.models import Client, Fund, Role, Severity, User
from app.security import hash_password
from app.services import workflow as wf
from app.services.audit import log_action
from app.services.ingest import read_file
from app.services.mapping import RuleBasedMapper

DEMO_PASSWORD = "demo1234"
SAMPLES_DIR = Path(__file__).resolve().parents[1] / "sample_files"

USERS = [
    ("analyst", "Alice Martin", "Reporting analyst", Role.MAKER),
    ("analyst2", "Marc Weber", "Reporting analyst", Role.MAKER),
    ("reviewer", "Claire Dubois", "Senior reviewer, regulatory reporting", Role.CHECKER),
    ("admin", "Sam Keller", "Platform administrator", Role.ADMIN),
]


def _upload(db: Session, user: User, client: Client, filename: str, period, filing=None):
    path = SAMPLES_DIR / filename
    parsed = read_file(filename, path.read_bytes())
    if filing is None:
        return wf.create_filing(db, user, client, period, parsed, mapper=RuleBasedMapper())
    wf.add_version(db, user, filing, parsed, mapper=RuleBasedMapper())
    return filing


def _confirm_as_proposed(db: Session, user: User, filing) -> None:
    if filing.current.mapping_confirmed:
        return
    choices = {c: (p.get("field"), p.get("transform", "none")) for c, p in filing.current.mapping.items()}
    wf.confirm_mapping(db, user, filing, choices)


def _justify_all(db: Session, user: User, filing, note: str) -> None:
    for f in list(filing.current.findings):
        if f.severity == Severity.WARNING and f.state != "justified":
            wf.justify_warning(db, user, f, note)


def seed(db: Session) -> None:
    if db.scalar(select(User).limit(1)):
        print("Database already seeded - nothing to do (use --reset to rebuild).")
        return

    users = {}
    for username, name, title, role in USERS:
        users[username] = User(
            username=username, full_name=name, job_title=title, role=role, password_hash=hash_password(DEMO_PASSWORD)
        )
        db.add(users[username])

    clients = {}
    for spec in d.CLIENTS:
        client = Client(**{k: v for k, v in spec.items() if k != "funds"})
        db.add(client)
        for name, lei, ccy, strategy, nav, period in spec["funds"]:
            db.add(
                Fund(
                    client=client,
                    name=name,
                    lei=lei,
                    base_currency=ccy,
                    strategy=strategy,
                    last_reported_nav=nav,
                    last_reported_period=period,
                )
            )
        clients[spec["code"]] = client
    db.flush()
    log_action(db, "system.seeded", clients=list(clients), users=list(users))
    db.commit()

    # Riverbend history, prepared by Marc and reviewed by Claire.
    marc, claire = users["analyst2"], users["reviewer"]
    rb = clients["RIVERBEND"]
    q1 = _upload(db, marc, rb, "riverbend_declaration_2026-03-31.csv", date(2026, 3, 31))
    _confirm_as_proposed(db, marc, q1)
    _justify_all(db, marc, q1, "Reviewed with the client.")
    wf.submit_filing(db, marc, q1)
    wf.approve_filing(db, claire, q1, "Figures consistent with the custodian statement.")
    wf.send_to_client(db, marc, q1, "For your approval: fund return 31 Mar 2026")
    wf.record_client_decision(db, marc, q1, True, "Julien Moreau", "Approved by email.")
    wf.record_filed(db, marc, q1, "DEMO-2026-0331-RB")

    q2 = _upload(db, marc, rb, "riverbend_declaration_2026-06-30.csv", date(2026, 6, 30))
    _confirm_as_proposed(db, marc, q2)
    _justify_all(
        db,
        marc,
        q2,
        "Confirmed by Julien Moreau (Riverbend): EUR 26m subscription from an institutional investor on 12 June.",
    )
    wf.submit_filing(db, marc, q2)
    print(f"Seeded {len(users)} users, {len(clients)} clients and 2 Riverbend filings (password: {DEMO_PASSWORD}).")


def main() -> None:
    if "--reset" in sys.argv:
        from app import models  # noqa: F401

        Base.metadata.drop_all(engine)
    init_db()
    with SessionLocal() as db:
        seed(db)


if __name__ == "__main__":
    main()
