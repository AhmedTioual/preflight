import os
import tempfile
from pathlib import Path

# Configure the app for tests BEFORE importing it: isolated DB, no AI calls.
_TMP = Path(tempfile.mkdtemp(prefix="preflight-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["USE_LLM"] = "false"
os.environ["PASSWORD_HASH_ITERATIONS"] = "1000"
os.environ["SECRET_KEY"] = "test-secret-key-with-enough-length-for-hs256"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.demo_data import SAMPLES  # noqa: E402
from app.models import Client, Filing, User  # noqa: E402
from app.seed import SAMPLES_DIR, seed  # noqa: E402
from app.services.ingest import read_file  # noqa: E402
from app.services.mapping import RuleBasedMapper  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Every test starts from a freshly seeded database (with Riverbend history)."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed(db)
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def users(db):
    return {u.username: u for u in db.scalars(select(User))}


@pytest.fixture
def clients(db):
    return {c.code: c for c in db.scalars(select(Client))}


def sample(key: str):
    spec = SAMPLES[key]
    return spec, read_file(spec["file"], (SAMPLES_DIR / spec["file"]).read_bytes())


@pytest.fixture
def northstar_q2(db, users, clients):
    """Northstar June 2026 filing, version 1 uploaded and mapping confirmed as proposed."""
    from app.services import workflow as wf

    spec, parsed = sample("northstar_q2_v1")
    filing = wf.create_filing(db, users["analyst"], clients["NORTHSTAR"], spec["period"], parsed, RuleBasedMapper())
    choices = {c: (p["field"], p["transform"]) for c, p in filing.current.mapping.items()}
    wf.confirm_mapping(db, users["analyst"], filing, choices)
    return filing


@pytest.fixture
def riverbend_in_review(db) -> Filing:
    return db.scalar(select(Filing).where(Filing.status == "in_review"))


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def login(client: TestClient, username: str) -> dict:
    response = client.post("/api/token", data={"username": username, "password": "demo1234"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def by_rule(filing, rule_id):
    return [f for f in filing.current.findings if f.rule_id == rule_id]
