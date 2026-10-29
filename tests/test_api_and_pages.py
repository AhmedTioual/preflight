from pathlib import Path

from app.seed import SAMPLES_DIR

from .conftest import login

NS_V1 = SAMPLES_DIR / "northstar_fund_return_2026-06-30.xlsx"


def test_health_and_auth(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/api/filings").status_code == 401
    assert client.post("/api/token", data={"username": "analyst", "password": "nope"}).status_code == 401


def test_api_end_to_end(client):
    maker, reviewer = login(client, "analyst"), login(client, "reviewer")
    with open(NS_V1, "rb") as fh:
        created = client.post(
            "/api/filings",
            headers=maker,
            data={"client_code": "NORTHSTAR", "period_end": "2026-06-30"},
            files={"file": (NS_V1.name, fh)},
        )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "mapping"
    columns = {c: (m["field"], m["transform"]) for c, m in body["mapping"].items()}
    checked = client.post(f"/api/filings/{body['id']}/mapping", headers=maker, json={"columns": columns}).json()
    assert checked["counts"]["errors"] == 3
    assert checked["data"][0]["nav"] == 431_250_000

    submit = client.post(f"/api/filings/{body['id']}/submit", headers=maker)
    assert submit.status_code == 400 and "Not ready" in submit.json()["detail"]
    assert client.post(f"/api/filings/{body['id']}/approve", headers=reviewer).status_code == 400
    assert client.get("/api/audit/verify", headers=reviewer).json()["valid"] is True
    assert client.get("/api/audit/verify", headers=maker).status_code == 403


def test_checker_cannot_upload(client):
    reviewer = login(client, "reviewer")
    with open(NS_V1, "rb") as fh:
        r = client.post(
            "/api/filings",
            headers=reviewer,
            data={"client_code": "NORTHSTAR", "period_end": "2026-06-30"},
            files={"file": (NS_V1.name, fh)},
        )
    assert r.status_code == 403


def test_unsupported_file_is_rejected_with_a_message(client):
    client.post("/login", data={"username": "analyst", "password": "demo1234"})
    r = client.post(
        "/filings",
        data={"client_code": "NORTHSTAR", "period_end": "2026-06-30", "source": "file"},
        files={"file": ("report.pdf", b"%PDF-1.7")},
        headers={"referer": "http://testserver/filings/new"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error=Upload%20an%20Excel" in r.headers["location"]


def test_web_demo_flow_and_pages_render(client):
    client.post("/login", data={"username": "analyst", "password": "demo1234"})
    r = client.post("/filings", data={"source": "sample", "sample": "northstar_q2_v1"}, follow_redirects=False)
    mapping_url = r.headers["location"].split("?")[0]
    assert mapping_url.endswith("/mapping")
    page = client.get(mapping_url).text
    assert "Net Asset Val (EUR k)" in page and "Multiply by 1,000" in page

    filing_id = int(mapping_url.split("/")[2])
    form = {}
    for i in range(17):
        form[f"field_{i}"] = (
            page.split(f'name="field_{i}"')[1].split("selected")[0].rsplit('value="', 1)[1].split('"')[0]
        )
        form[f"transform_{i}"] = "thousands" if i == 5 else "none"
    form["field_16"] = ""  # "Internal comment" is not needed
    r = client.post(f"/filings/{filing_id}/mapping", data=form, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/filings/{filing_id}").text
    assert page.count("rcell t-") == 16 * 3
    assert "3 errors left" in page and "Ask the client" in page

    for url in [
        "/overview",
        "/filings/new",
        "/clients",
        "/clients/NORTHSTAR",
        "/audit",
        f"/filings/{filing_id}/record",
    ]:
        assert client.get(url).status_code == 200, url
    assert client.get(f"/filings/{filing_id}/export.csv", follow_redirects=False).status_code == 303  # not approved


def test_reviewer_sees_decision_bar_and_approves(client):
    client.post("/login", data={"username": "reviewer", "password": "demo1234"})
    page = client.get("/filings/2").text  # Riverbend June, prepared by Marc
    assert "Approve filing" in page
    r = client.post("/filings/2/approve", data={"comment": "Checked."}, follow_redirects=False)
    assert r.status_code == 303
    csv = client.get("/filings/2/export.csv")
    assert csv.status_code == 200 and csv.text.startswith("fund_name,")


def test_preparer_sees_no_decision_bar(client):
    client.post("/login", data={"username": "analyst2", "password": "demo1234"})
    page = client.get("/filings/2").text
    assert "Approve filing" not in page and "Waiting for a second reviewer" in page


def test_sample_files_exist():
    assert {p.name for p in Path(SAMPLES_DIR).iterdir()} >= {
        "northstar_fund_return_2026-06-30.xlsx",
        "northstar_fund_return_2026-06-30_v2.xlsx",
        "northstar_fund_return_2026-09-30.xlsx",
        "riverbend_declaration_2026-03-31.csv",
        "riverbend_declaration_2026-06-30.csv",
    }
