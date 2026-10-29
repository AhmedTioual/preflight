import pytest
from sqlalchemy import select

from app.models import AuditLog, ClientMapping, FilingStatus, FindingState
from app.services import workflow as wf
from app.services.audit import verify_chain
from app.services.mapping import RuleBasedMapper

from .conftest import by_rule, sample

NOTE = "Confirmed by the client in writing."


def fix_lei(db, user, filing):
    f = by_rule(filing, "LEI-CHECKSUM")[0]
    wf.correct_value(db, user, filing, f.fund_key, "fund_lei", f.suggestion, "LEI from the fund register.")


def justify_all(db, user, filing):
    for f in list(filing.current.findings):
        if f.severity == "warning" and f.state != FindingState.JUSTIFIED:
            wf.justify_warning(db, user, f, NOTE)


def test_first_file_needs_mapping_confirmation(db, users, clients):
    spec, parsed = sample("northstar_q2_v1")
    filing = wf.create_filing(db, users["analyst"], clients["NORTHSTAR"], spec["period"], parsed, RuleBasedMapper())
    assert filing.status == FilingStatus.MAPPING
    assert not filing.current.findings
    assert db.scalar(select(ClientMapping).where(ClientMapping.client_id == clients["NORTHSTAR"].id)) is None


def test_confirmed_mapping_is_saved_and_reused_next_quarter(db, users, clients, northstar_q2):
    saved = db.scalars(select(ClientMapping).where(ClientMapping.client_id == clients["NORTHSTAR"].id)).all()
    assert len(saved) == 17
    spec, parsed = sample("northstar_q3")
    q3 = wf.create_filing(db, users["analyst"], clients["NORTHSTAR"], spec["period"], parsed)
    assert q3.status == FilingStatus.IN_PROGRESS  # no mapping step
    assert q3.current.mapping_method == "saved"
    # June is not approved yet, so September NAVs are compared with March: one plausible warning.
    assert q3.current.counts()["errors"] == 0


def test_mapping_rejects_a_field_fed_twice(db, users, clients):
    spec, parsed = sample("northstar_q2_v1")
    filing = wf.create_filing(db, users["analyst"], clients["NORTHSTAR"], spec["period"], parsed, RuleBasedMapper())
    choices = {c: (p["field"], p["transform"]) for c, p in filing.current.mapping.items()}
    choices["Internal comment"] = ("nav", "none")
    with pytest.raises(wf.WorkflowError, match="mapped twice"):
        wf.confirm_mapping(db, users["analyst"], filing, choices)


def test_duplicate_filing_for_same_period_is_refused(db, users, clients, northstar_q2):
    spec, parsed = sample("northstar_q2_v1")
    with pytest.raises(wf.WorkflowError, match="already exists"):
        wf.create_filing(db, users["analyst"], clients["NORTHSTAR"], spec["period"], parsed)


def test_correction_requires_reason_and_rechecks(db, users, northstar_q2):
    f = by_rule(northstar_q2, "LEI-CHECKSUM")[0]
    with pytest.raises(wf.WorkflowError, match="Explain"):
        wf.correct_value(db, users["analyst"], northstar_q2, f.fund_key, "fund_lei", f.suggestion, " ")
    with pytest.raises(wf.WorkflowError, match="not a valid"):
        wf.correct_value(db, users["analyst"], northstar_q2, f.fund_key, "investor_count", "many", NOTE)
    fix_lei(db, users["analyst"], northstar_q2)
    assert not by_rule(northstar_q2, "LEI-CHECKSUM")
    assert northstar_q2.current.counts()["errors"] == 2


def test_justifications_survive_a_recheck_and_errors_cannot_be_justified(db, users, northstar_q2):
    nav = by_rule(northstar_q2, "NAV-JUMP")[0]
    wf.justify_warning(db, users["analyst"], nav, NOTE)
    fix_lei(db, users["analyst"], northstar_q2)  # re-runs every check
    assert by_rule(northstar_q2, "NAV-JUMP")[0].state == FindingState.JUSTIFIED
    with pytest.raises(wf.WorkflowError, match="cannot be justified"):
        wf.justify_warning(db, users["analyst"], by_rule(northstar_q2, "LIQ-SUM")[0], NOTE)


def test_client_query_then_corrected_version(db, users, northstar_q2):
    alice = users["analyst"]
    fix_lei(db, alice, northstar_q2)
    wf.justify_warning(db, alice, by_rule(northstar_q2, "NAV-JUMP")[0], NOTE)
    draft = wf.draft_client_query(northstar_q2, alice)
    assert "Dear Laura" in draft.body and "98% instead of 100%" in draft.body
    assert len(draft.finding_ids) == 3  # 2 errors + 1 unjustified warning
    wf.send_client_query(db, alice, northstar_q2, draft.subject, draft.body)
    assert northstar_q2.current.counts()["queried"] == 3

    _, v2 = sample("northstar_q2_v2")
    wf.add_version(db, alice, northstar_q2, v2)
    current = northstar_q2.current
    assert current.number == 2 and current.mapping_method == "saved"
    assert current.counts()["errors"] == 0  # client fixed two, LEI correction carried over
    assert by_rule(northstar_q2, "NAV-JUMP")[0].carried_over  # same value, justification kept
    changed = {(c["fund"], c["field"]) for c in wf.version_diff(northstar_q2, current)}
    assert changed == {
        ("Northstar European Credit Fund", "liq_over_90d"),
        ("Northstar Real Assets Fund", "largest_investor_pct"),
    }


def test_identical_file_is_refused(db, users, northstar_q2):
    _, same = sample("northstar_q2_v1")
    with pytest.raises(wf.WorkflowError, match="identical"):
        wf.add_version(db, users["analyst"], northstar_q2, same)


def test_submission_blocked_until_clean(db, users, northstar_q2):
    alice = users["analyst"]
    with pytest.raises(wf.WorkflowError, match="3 errors to correct; 2 warnings to justify"):
        wf.submit_filing(db, alice, northstar_q2)
    fix_lei(db, alice, northstar_q2)
    _, v2 = sample("northstar_q2_v2")
    wf.add_version(db, alice, northstar_q2, v2)
    justify_all(db, alice, northstar_q2)
    assert wf.submission_blockers(northstar_q2) == []
    wf.submit_filing(db, alice, northstar_q2)
    assert northstar_q2.status == FilingStatus.IN_REVIEW
    with pytest.raises(wf.WorkflowError, match="locked"):
        wf.correct_value(db, alice, northstar_q2, "northstar global equity fund", "nav", "1", NOTE)


def test_four_eyes_rules(db, users, riverbend_in_review):
    # Marc prepared it: he cannot review. Alice is a maker: she cannot review.
    with pytest.raises(wf.WorkflowError, match="Only reviewers"):
        wf.approve_filing(db, users["analyst"], riverbend_in_review)
    with pytest.raises(wf.WorkflowError, match="Only reviewers"):
        wf.approve_filing(db, users["analyst2"], riverbend_in_review)
    with pytest.raises(wf.WorkflowError, match="Tell the analyst"):
        wf.return_filing(db, users["reviewer"], riverbend_in_review, "")
    wf.approve_filing(db, users["reviewer"], riverbend_in_review, "OK")
    assert riverbend_in_review.status == FilingStatus.APPROVED


def test_admin_who_prepared_cannot_approve(db, users, clients):
    admin = users["admin"]
    spec, parsed = sample("northstar_q3")
    # Admin uploads the Q3 file itself (the mapping step is needed: Northstar has no saved columns yet).
    filing = wf.create_filing(db, admin, clients["NORTHSTAR"], spec["period"], parsed, RuleBasedMapper())
    wf.confirm_mapping(db, admin, filing, {c: (p["field"], p["transform"]) for c, p in filing.current.mapping.items()})
    justify_all(db, admin, filing)
    wf.submit_filing(db, admin, filing)
    with pytest.raises(wf.WorkflowError, match="Four-eyes"):
        wf.approve_filing(db, admin, filing)


def test_returned_filing_can_be_resubmitted(db, users, riverbend_in_review):
    wf.return_filing(db, users["reviewer"], riverbend_in_review, "Attach the client's confirmation.")
    assert riverbend_in_review.status == FilingStatus.RETURNED
    wf.submit_filing(db, users["analyst2"], riverbend_in_review)
    assert riverbend_in_review.status == FilingStatus.IN_REVIEW


def test_export_uses_template_order_and_corrected_values(db, users, northstar_q2):
    alice = users["analyst"]
    fix_lei(db, alice, northstar_q2)
    csv_text = wf.export_csv(northstar_q2).splitlines()
    assert csv_text[0].startswith("fund_name,fund_lei,period_end,base_currency,strategy,nav")
    assert "999900NSGLOBEQTY0151" in csv_text[1] and "431250000.0" in csv_text[1]


def test_every_step_is_audited_and_chain_holds(db, users, northstar_q2):
    fix_lei(db, users["analyst"], northstar_q2)
    actions = [e.action for e in db.scalars(select(AuditLog).where(AuditLog.filing_id == northstar_q2.id))]
    assert actions == ["filing.created", "version.uploaded", "mapping.confirmed", "value.corrected"]
    assert verify_chain(db).valid
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "value.corrected"))
    entry.details = '{"note": "edited later"}'
    db.commit()
    assert not verify_chain(db).valid


def test_first_time_right_is_measured_on_the_first_check(db, users, northstar_q2, riverbend_in_review):
    fix_lei(db, users["analyst"], northstar_q2)
    assert northstar_q2.current.initial_errors == 3  # unchanged by the correction
    assert wf.first_time_right(northstar_q2) is False
    assert wf.first_time_right(riverbend_in_review) is True
