import pytest
from sqlalchemy import select

from app.models import AuditLog, FilingStatus
from app.services import workflow as wf

from .conftest import login


def approve(db, users, filing):
    wf.approve_filing(db, users["reviewer"], filing, "OK")
    return filing


def test_seeded_history_is_filed(db):
    from app.models import Filing

    march = db.scalar(select(Filing).where(Filing.status == FilingStatus.FILED))
    assert march.client_decision_by == "Julien Moreau"
    assert march.filing_reference == "DEMO-2026-0331-RB"


def test_full_release_flow(db, users, riverbend_in_review):
    marc = users["analyst2"]
    filing = approve(db, users, riverbend_in_review)
    draft = wf.draft_signoff_request(filing, marc)
    assert "Dear Julien" in draft.body and "Riverbend Gestion remains responsible" in draft.body
    assert "file it with the CSSF" in draft.body

    with pytest.raises(wf.WorkflowError, match="after the client's sign-off"):
        wf.record_filed(db, marc, filing, "REF-1")
    wf.send_to_client(db, marc, filing, draft.subject, draft.body)
    assert filing.status == FilingStatus.SENT_TO_CLIENT
    with pytest.raises(wf.WorkflowError, match="name of the person"):
        wf.record_client_decision(db, marc, filing, True, " ")
    wf.record_client_decision(db, marc, filing, True, "Julien Moreau", "OK to file")
    assert filing.status == FilingStatus.CLIENT_APPROVED
    with pytest.raises(wf.WorkflowError, match="submission reference"):
        wf.record_filed(db, marc, filing, "")
    wf.record_filed(db, marc, filing, "DEMO-REF-42")
    assert filing.status == FilingStatus.FILED

    actions = [e.action for e in db.scalars(select(AuditLog).where(AuditLog.filing_id == filing.id))]
    assert actions[-4:] == ["filing.approved", "client.signoff_requested", "client.approved", "filing.filed"]


def test_cannot_send_before_internal_approval(db, users, riverbend_in_review):
    with pytest.raises(wf.WorkflowError, match="approved by a reviewer"):
        wf.send_to_client(db, users["analyst2"], riverbend_in_review)


def test_reviewers_do_not_run_the_release_steps(db, users, riverbend_in_review):
    filing = approve(db, users, riverbend_in_review)
    with pytest.raises(wf.WorkflowError) as exc:
        wf.send_to_client(db, users["reviewer"], filing)
    assert exc.value.status_code == 403


def test_client_change_request_reopens_and_needs_new_review(db, users, riverbend_in_review):
    marc = users["analyst2"]
    filing = approve(db, users, riverbend_in_review)
    wf.send_to_client(db, marc, filing)
    with pytest.raises(wf.WorkflowError, match="Describe the changes"):
        wf.record_client_decision(db, marc, filing, False, "Julien Moreau", "")
    wf.record_client_decision(db, marc, filing, False, "Julien Moreau", "Use the final NAV of 124.5m")
    assert filing.status == FilingStatus.RETURNED
    assert filing.reviewed_by is None
    # Back through the four-eyes review before going to the client again
    wf.submit_filing(db, marc, filing)
    assert filing.status == FilingStatus.IN_REVIEW
    with pytest.raises(wf.WorkflowError, match="approved by a reviewer"):
        wf.send_to_client(db, marc, filing)
    approve(db, users, filing)
    wf.send_to_client(db, marc, filing)
    assert filing.client_decision is None  # previous answer cleared for the new round


def test_released_filing_is_locked(db, users, riverbend_in_review):
    filing = approve(db, users, riverbend_in_review)
    wf.send_to_client(db, users["analyst2"], filing)
    with pytest.raises(wf.WorkflowError, match="locked"):
        wf.correct_value(db, users["analyst2"], filing, filing.current.normalized[0]["key"], "nav", "1", "x")


def test_web_release_flow(client):
    client.post("/login", data={"username": "reviewer", "password": "demo1234"})
    client.post("/filings/2/approve", data={"comment": "OK"})
    client.post("/logout")
    client.post("/login", data={"username": "analyst2", "password": "demo1234"})
    page = client.get("/filings/2").text
    assert "Send to client for sign-off" in page and "Dear Julien" in page
    assert (
        client.post("/filings/2/send-to-client", data={"subject": "s", "body": "b"}, follow_redirects=False).status_code
        == 303
    )
    assert "Record client answer" in client.get("/filings/2").text
    client.post("/filings/2/client-decision", data={"decision": "approved", "contact_name": "Julien Moreau"})
    page = client.get("/filings/2").text
    assert "Record CSSF submission" in page
    client.post("/filings/2/filed", data={"reference": "DEMO-REF-7"})
    page = client.get("/filings/2").text
    assert "Filed with the CSSF" in page and "DEMO-REF-7" in page
    assert client.get("/filings/2/export.csv").status_code == 200
    assert "DEMO-REF-7" in client.get("/filings/2/record").text


def test_api_release_flow(client):
    reviewer, marc = login(client, "reviewer"), login(client, "analyst2")
    client.post("/api/filings/2/approve", headers=reviewer, data={"comment": "OK"})
    assert client.post("/api/filings/2/send-to-client", headers=marc).json()["status"] == "sent_to_client"
    r = client.post(
        "/api/filings/2/client-decision", headers=marc, data={"decision": "approved", "contact_name": "J. Moreau"}
    )
    assert r.json()["status"] == "client_approved"
    assert client.post("/api/filings/2/filed", headers=marc, data={"reference": "R-1"}).json()["status"] == "filed"
