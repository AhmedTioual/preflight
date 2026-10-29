"""Preflight - FastAPI application.

Web UI (server-rendered) for reporting analysts and reviewers, plus a JSON API
under /api for automation. Interactive API docs: /docs
"""

import json
import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, get_db, init_db
from app.demo_data import SAMPLES
from app.models import RELEASED_STATUSES, AuditLog, Client, Filing, FilingStatus, Finding, Role, Severity, User
from app.security import COOKIE_NAME, authenticate, create_token, get_current_user, get_optional_user, require_roles
from app.seed import SAMPLES_DIR, seed
from app.services import workflow as wf
from app.services.audit import log_action, verify_chain
from app.services.ingest import IngestError, ParsedFile, read_file
from app.services.normalize import display_value
from app.services.template import TRANSFORMS, load_template
from app.views import FIELD_GROUPS, SOURCE_LABELS, STATE_LABELS, STATUS_LABELS, build_grid, mapping_rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as db:
        seed(db)  # idempotent: only seeds an empty database
    yield


app = FastAPI(
    title="Preflight",
    description="Pre-submission checks for regulatory fund filings: AI-assisted column mapping, "
    "rule-based validation and four-eyes sign-off.",
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["fromjson"] = json.loads
templates.env.globals.update(
    STATUS_LABELS=STATUS_LABELS,
    STATE_LABELS=STATE_LABELS,
    SOURCE_LABELS=SOURCE_LABELS,
    FIELD_GROUPS=FIELD_GROUPS,
    TRANSFORMS=TRANSFORMS,
    display_value=display_value,
    first_time_right=wf.first_time_right,
    filing_regulator=lambda f: load_template(f.template_id).regulator,
)


# --------------------------------------------------------------------------- errors
def _strip_flash(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in {"error", "msg"}]
    return urlunsplit(parts._replace(query=urlencode(query)))


@app.exception_handler(wf.WorkflowError)
async def workflow_error_handler(request: Request, exc: wf.WorkflowError):
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)
    target = _strip_flash(request.headers.get("referer") or "/overview")
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}error={quote(exc.message)}", status_code=303)


@app.exception_handler(IngestError)
async def ingest_error_handler(request: Request, exc: IngestError):
    return await workflow_error_handler(request, wf.WorkflowError(str(exc)))


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401 and not request.url.path.startswith("/api"):
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


# --------------------------------------------------------------------------- helpers
def render(request: Request, name: str, user: User | None, **context) -> HTMLResponse:
    context.update(
        user=user,
        error=request.query_params.get("error"),
        message=request.query_params.get("msg"),
        llm_enabled=settings.llm_enabled,
    )
    return templates.TemplateResponse(request, name, context)


def redirect(url: str, msg: str | None = None) -> RedirectResponse:
    if msg:
        url = f"{url}{'&' if '?' in url else '?'}msg={quote(msg)}"
    return RedirectResponse(url, status_code=303)


def get_filing_or_404(db: Session, filing_id: int) -> Filing:
    filing = db.get(Filing, filing_id)
    if filing is None:
        raise HTTPException(404, "Filing not found")
    return filing


def get_client_or_404(db: Session, code: str) -> Client:
    client = db.scalar(select(Client).where(Client.code == code.upper()))
    if client is None:
        raise HTTPException(404, "Client not found")
    return client


def load_sample(key: str) -> tuple[dict, ParsedFile]:
    spec = SAMPLES.get(key)
    if spec is None:
        raise wf.WorkflowError("Choose one of the sample files.")
    return spec, read_file(spec["file"], (SAMPLES_DIR / spec["file"]).read_bytes())


async def parsed_upload(file: UploadFile | None) -> ParsedFile:
    if file is None or not file.filename:
        raise wf.WorkflowError("Choose a file to upload, or pick a sample.")
    return read_file(file.filename, await file.read())


def checks_message(filing: Filing) -> str:
    v = filing.current
    if filing.status == FilingStatus.MAPPING:
        return "File received. Review how its columns map to the template."
    c = v.counts()
    prefix = "Columns recognised from earlier files. " if v.mapping_method == "saved" else ""
    if not c["total"]:
        return f"{prefix}Version {v.number} checked: no findings."
    return (
        f"{prefix}Version {v.number} checked: {c['errors']} error{'s' if c['errors'] != 1 else ''}, "
        f"{c['warnings']} warning{'s' if c['warnings'] != 1 else ''}."
    )


def filing_url(filing: Filing) -> str:
    return f"/filings/{filing.id}/mapping" if filing.status == FilingStatus.MAPPING else f"/filings/{filing.id}"


def client_quality(db: Session) -> list[dict]:
    out = []
    for client in db.scalars(select(Client).order_by(Client.name)):
        filings = [f for f in client.filings if f.versions]
        ftr = [wf.first_time_right(f) for f in filings]
        ftr = [x for x in ftr if x is not None]
        rules: dict[str, int] = {}
        for f in filings:
            for finding in f.versions[0].findings:
                rules[finding.rule_id] = rules.get(finding.rule_id, 0) + 1
        top = max(rules.items(), key=lambda kv: kv[1]) if rules else None
        out.append(
            {
                "client": client,
                "filings": len(filings),
                "first_time_right": (sum(ftr), len(ftr)),
                "top_issue": load_template().rule(top[0])["name"] if top else None,
                "mappings": len(client.mappings),
            }
        )
    return out


# --------------------------------------------------------------------------- auth pages
@app.get("/", include_in_schema=False)
def index(user: User | None = Depends(get_optional_user)):
    return RedirectResponse("/overview" if user else "/login", status_code=303)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, user: User | None = Depends(get_optional_user)):
    if user:
        return RedirectResponse("/overview", status_code=303)
    return render(request, "login.html", None)


@app.post("/login", include_in_schema=False)
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = authenticate(db, username.strip(), password)
    if user is None:
        log_action(db, "auth.login_failed", None, username=username.strip())
        db.commit()
        return render(request, "login.html", None, login_error="Wrong username or password.", username=username)
    log_action(db, "auth.login", user)
    db.commit()
    response = RedirectResponse("/overview", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        create_token(user),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.token_ttl_minutes * 60,
    )
    return response


@app.post("/logout", include_in_schema=False)
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# --------------------------------------------------------------------------- overview
@app.get("/overview", response_class=HTMLResponse, include_in_schema=False)
def overview(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    filings = db.scalars(select(Filing).order_by(Filing.created_at.desc())).all()
    is_reviewer = user.role in {Role.CHECKER, Role.ADMIN}
    attention = []
    for f in filings:
        if (
            is_reviewer
            and f.status == FilingStatus.IN_REVIEW
            and user.id not in wf.preparers(db, f)
            or user.role in {Role.MAKER, Role.ADMIN}
            and f.status
            in {
                FilingStatus.MAPPING,
                FilingStatus.IN_PROGRESS,
                FilingStatus.RETURNED,
                FilingStatus.APPROVED,
                FilingStatus.SENT_TO_CLIENT,
                FilingStatus.CLIENT_APPROVED,
            }
        ):
            attention.append(f)
    first_files = [f.versions[0] for f in filings if f.versions and f.versions[0].initial_errors is not None]
    stats = {
        "open": sum(
            f.status in {FilingStatus.MAPPING, FilingStatus.IN_PROGRESS, FilingStatus.RETURNED} for f in filings
        ),
        "in_review": sum(f.status == FilingStatus.IN_REVIEW for f in filings),
        "caught": sum((v.initial_errors or 0) + (v.initial_warnings or 0) for v in first_files),
        "ftr": (sum(v.initial_errors == 0 for v in first_files), len(first_files)),
    }
    return render(
        request,
        "overview.html",
        user,
        active="overview",
        filings=filings,
        attention=attention,
        stats=stats,
        quality=client_quality(db),
        is_reviewer=is_reviewer,
    )


# --------------------------------------------------------------------------- new filing
@app.get("/filings/new", response_class=HTMLResponse, include_in_schema=False)
def new_filing_page(request: Request, user: User = Depends(require_roles(Role.MAKER)), db: Session = Depends(get_db)):
    clients = db.scalars(select(Client).order_by(Client.name)).all()
    taken = {(f.client.code, f.period_end) for f in db.scalars(select(Filing))}
    samples = {k: {**v, "period": v["period"].isoformat(), "taken": (v["client"], v["period"]) in taken}
               for k, v in SAMPLES.items() if not k.endswith("_v2")}  # fmt: skip
    return render(request, "filing_new.html", user, active="new", clients=clients, samples=samples)


@app.post("/filings", include_in_schema=False)
async def create_filing(
    client_code: str = Form(""),
    period_end: str = Form(""),
    source: str = Form("file"),
    sample: str = Form(""),
    file: UploadFile | None = File(None),
    user: User = Depends(require_roles(Role.MAKER)),
    db: Session = Depends(get_db),
):
    if source == "sample":
        spec, parsed = load_sample(sample)
        client, period = get_client_or_404(db, spec["client"]), spec["period"]
    else:
        if not client_code or not period_end:
            raise wf.WorkflowError("Choose the client and the reporting period.")
        client = get_client_or_404(db, client_code)
        try:
            period = date.fromisoformat(period_end)
        except ValueError as exc:
            raise wf.WorkflowError("Enter a valid period end date.") from exc
        parsed = await parsed_upload(file)
    filing = wf.create_filing(db, user, client, period, parsed)
    return redirect(filing_url(filing), checks_message(filing))


# --------------------------------------------------------------------------- mapping
@app.get("/filings/{filing_id}/mapping", response_class=HTMLResponse, include_in_schema=False)
def mapping_page(
    filing_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    filing = get_filing_or_404(db, filing_id)
    version = filing.current
    template = load_template(filing.template_id)
    rows = mapping_rows(version, template)
    mapped = {r["field"] for r in rows if r["field"]}
    missing = [f for f in template.fields if f.required and f.key not in mapped]
    return render(
        request,
        "mapping.html",
        user,
        active="filing",
        filing=filing,
        version=version,
        template=template,
        rows=rows,
        missing=missing,
        editable=not version.mapping_confirmed and user.role in {Role.MAKER, Role.ADMIN},
    )


@app.post("/filings/{filing_id}/mapping", include_in_schema=False)
async def confirm_mapping(
    filing_id: int, request: Request, user: User = Depends(require_roles(Role.MAKER)), db: Session = Depends(get_db)
):
    filing = get_filing_or_404(db, filing_id)
    form = await request.form()
    choices = {}
    for i, column in enumerate(filing.current.columns):
        field_key = form.get(f"field_{i}") or None
        choices[column] = (field_key, form.get(f"transform_{i}") or "none")
    wf.confirm_mapping(db, user, filing, choices)
    return redirect(f"/filings/{filing.id}", "Mapping saved for this client. " + checks_message(filing))


# --------------------------------------------------------------------------- workspace
@app.get("/filings/{filing_id}", response_class=HTMLResponse, include_in_schema=False)
def filing_page(
    filing_id: int,
    request: Request,
    v: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    filing = get_filing_or_404(db, filing_id)
    if filing.status == FilingStatus.MAPPING and v is None:
        return RedirectResponse(f"/filings/{filing.id}/mapping", status_code=303)
    version = next((x for x in filing.versions if x.number == v), None) if v else filing.current
    if version is None or not version.mapping_confirmed:
        raise HTTPException(404, "Version not found")
    template = load_template(filing.template_id)
    is_current = version.id == filing.current.id
    is_maker = user.role in {Role.MAKER, Role.ADMIN}
    can_edit = is_current and is_maker and filing.status in {FilingStatus.IN_PROGRESS, FilingStatus.RETURNED}
    review_blocker = wf.review_blocker(db, user, filing) if filing.status == FilingStatus.IN_REVIEW else None
    can_review = is_current and filing.status == FilingStatus.IN_REVIEW and review_blocker is None
    history = db.scalars(select(AuditLog).where(AuditLog.filing_id == filing.id).order_by(AuditLog.id)).all()
    return render(
        request,
        "filing.html",
        user,
        active="filing",
        filing=filing,
        version=version,
        template=template,
        grid=build_grid(filing, version, template),
        diff=wf.version_diff(filing, version),
        counts=version.counts(),
        blockers=wf.submission_blockers(filing),
        can_edit=can_edit,
        can_review=can_review,
        review_blocker=review_blocker,
        is_current=is_current,
        query=wf.draft_client_query(filing, user) if can_edit else None,
        signoff=wf.draft_signoff_request(filing, user) if is_maker and filing.status == FilingStatus.APPROVED else None,
        can_release=is_current and is_maker,
        released=filing.status in RELEASED_STATUSES,
        history=history,
        samples={k: v for k, v in SAMPLES.items() if v["client"] == filing.client.code},
    )


@app.post("/filings/{filing_id}/versions", include_in_schema=False)
async def upload_version(
    filing_id: int,
    source: str = Form("file"),
    sample: str = Form(""),
    file: UploadFile | None = File(None),
    user: User = Depends(require_roles(Role.MAKER)),
    db: Session = Depends(get_db),
):
    filing = get_filing_or_404(db, filing_id)
    if source == "sample":
        spec, parsed = load_sample(sample)
        if spec["client"] != filing.client.code or spec["period"] != filing.period_end:
            raise wf.WorkflowError("That sample belongs to another client or period.")
    else:
        parsed = await parsed_upload(file)
    wf.add_version(db, user, filing, parsed)
    return redirect(filing_url(filing), checks_message(filing))


@app.post("/filings/{filing_id}/corrections", include_in_schema=False)
def correct(
    filing_id: int,
    fund_key: str = Form(...),
    field: str = Form(...),
    value: str = Form(""),
    note: str = Form(""),
    user: User = Depends(require_roles(Role.MAKER)),
    db: Session = Depends(get_db),
):
    filing = get_filing_or_404(db, filing_id)
    if not value.strip():
        raise wf.WorkflowError("Enter the corrected value.")
    wf.correct_value(db, user, filing, fund_key, field, value, note)
    return redirect(
        f"/filings/{filing.id}?cell={quote(fund_key)}:{field}", "Value corrected. " + checks_message(filing)
    )


@app.post("/findings/{finding_id}/justify", include_in_schema=False)
def justify(
    finding_id: int,
    note: str = Form(""),
    user: User = Depends(require_roles(Role.MAKER)),
    db: Session = Depends(get_db),
):
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(404, "Finding not found")
    wf.justify_warning(db, user, finding, note)
    filing = finding.version.filing
    return redirect(f"/filings/{filing.id}?cell={quote(finding.fund_key)}:{finding.field}", "Warning justified.")


@app.post("/filings/{filing_id}/query", include_in_schema=False)
def send_query(
    filing_id: int,
    subject: str = Form(""),
    body: str = Form(""),
    user: User = Depends(require_roles(Role.MAKER)),
    db: Session = Depends(get_db),
):
    filing = get_filing_or_404(db, filing_id)
    query = wf.send_client_query(db, user, filing, subject, body)
    return redirect(
        f"/filings/{filing.id}",
        f"Query recorded as sent to {filing.client.contact_name or filing.client.name} ({query.finding_count} points).",
    )


@app.post("/filings/{filing_id}/submit", include_in_schema=False)
def submit(filing_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    filing = get_filing_or_404(db, filing_id)
    wf.submit_filing(db, user, filing)
    return redirect(f"/filings/{filing.id}", "Submitted for review. A second person must approve it.")


@app.post("/filings/{filing_id}/approve", include_in_schema=False)
def approve(
    filing_id: int, comment: str = Form(""), user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    filing = get_filing_or_404(db, filing_id)
    wf.approve_filing(db, user, filing, comment)
    return redirect(f"/filings/{filing.id}", "Filing approved. Next: the analyst sends it to the client for sign-off.")


@app.post("/filings/{filing_id}/return", include_in_schema=False)
def return_(
    filing_id: int, comment: str = Form(""), user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    filing = get_filing_or_404(db, filing_id)
    wf.return_filing(db, user, filing, comment)
    return redirect(f"/filings/{filing.id}", "Filing returned to the analyst.")


@app.post("/filings/{filing_id}/send-to-client", include_in_schema=False)
def send_to_client(
    filing_id: int,
    subject: str = Form(""),
    body: str = Form(""),
    user: User = Depends(require_roles(Role.MAKER)),
    db: Session = Depends(get_db),
):
    filing = get_filing_or_404(db, filing_id)
    wf.send_to_client(db, user, filing, subject, body)
    return redirect(f"/filings/{filing.id}", f"Recorded as sent to {filing.client.contact_name} for sign-off.")


@app.post("/filings/{filing_id}/client-decision", include_in_schema=False)
def client_decision(
    filing_id: int,
    decision: str = Form(...),
    contact_name: str = Form(""),
    comment: str = Form(""),
    user: User = Depends(require_roles(Role.MAKER)),
    db: Session = Depends(get_db),
):
    filing = get_filing_or_404(db, filing_id)
    approved = decision == "approved"
    wf.record_client_decision(db, user, filing, approved, contact_name, comment)
    if approved:
        msg = f"Client sign-off recorded. Next: file it with the {load_template(filing.template_id).regulator}."
    else:
        msg = "Change request recorded. The filing is back with the analyst and will need a new review."
    return redirect(f"/filings/{filing.id}", msg)


@app.post("/filings/{filing_id}/filed", include_in_schema=False)
def filed(
    filing_id: int,
    reference: str = Form(""),
    user: User = Depends(require_roles(Role.MAKER)),
    db: Session = Depends(get_db),
):
    filing = get_filing_or_404(db, filing_id)
    wf.record_filed(db, user, filing, reference)
    regulator = load_template(filing.template_id).regulator
    return redirect(f"/filings/{filing.id}", f"Recorded as filed with the {regulator}. The filing is complete.")


@app.get("/filings/{filing_id}/export.csv", include_in_schema=False)
def export(filing_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    filing = get_filing_or_404(db, filing_id)
    if filing.status not in RELEASED_STATUSES:
        raise wf.WorkflowError("The filing file is available once the filing is approved.")
    log_action(db, "filing.exported", user, filing.id, version=filing.current.number)
    db.commit()
    name = f"{filing.client.code.lower()}_fund_return_{filing.period_end.isoformat()}.csv"
    return PlainTextResponse(
        wf.export_csv(filing),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/filings/{filing_id}/record", response_class=HTMLResponse, include_in_schema=False)
def record(filing_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    filing = get_filing_or_404(db, filing_id)
    history = db.scalars(select(AuditLog).where(AuditLog.filing_id == filing.id).order_by(AuditLog.id)).all()
    return render(
        request,
        "record.html",
        user,
        filing=filing,
        template=load_template(filing.template_id),
        history=history,
        chain=verify_chain(db),
    )


# --------------------------------------------------------------------------- clients & audit
@app.get("/clients", response_class=HTMLResponse, include_in_schema=False)
def clients_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return render(request, "clients.html", user, active="clients", quality=client_quality(db))


@app.get("/clients/{code}", response_class=HTMLResponse, include_in_schema=False)
def client_page(code: str, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    client = get_client_or_404(db, code)
    return render(request, "client.html", user, active="clients", client=client, template=load_template())


@app.get("/audit", response_class=HTMLResponse, include_in_schema=False)
def audit_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    entries = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(300)).all()
    return render(request, "audit.html", user, active="audit", entries=entries, chain=verify_chain(db))


# --------------------------------------------------------------------------- API
def filing_json(filing: Filing, detail: bool = False) -> dict:
    v = filing.current
    data = {
        "id": filing.id,
        "client": filing.client.code,
        "period_end": filing.period_end.isoformat(),
        "status": filing.status,
        "version": v.number if v else None,
        "counts": v.counts() if v else None,
        "submission_blockers": wf.submission_blockers(filing),
    }
    if detail and v:
        data["mapping"] = v.mapping
        data["findings"] = [
            {
                "id": f.id,
                "fund": f.fund_label,
                "field": f.field,
                "rule": f.rule_id,
                "severity": f.severity,
                "state": f.state,
                "value": f.value_text,
                "expected": f.expected_text,
                "message": f.message,
                "note": f.note,
            }
            for f in v.findings
        ]
        data["data"] = [{"fund": r["label"], **r["values"]} for r in v.normalized]
    return data


class MappingIn(BaseModel):
    columns: dict[str, tuple[str | None, str]]


@app.get("/health", tags=["system"])
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok", "llm_enabled": settings.llm_enabled}


@app.post("/api/token", tags=["auth"])
def api_token(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = authenticate(db, form.username, form.password)
    if user is None:
        raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
    log_action(db, "auth.api_token", user)
    db.commit()
    return {"access_token": create_token(user), "token_type": "bearer"}


@app.get("/api/filings", tags=["filings"])
def api_filings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return [filing_json(f) for f in db.scalars(select(Filing).order_by(Filing.created_at.desc()))]


@app.post("/api/filings", tags=["filings"], status_code=201)
async def api_create_filing(
    client_code: str = Form(...),
    period_end: date = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(require_roles(Role.MAKER)),
    db: Session = Depends(get_db),
):
    filing = wf.create_filing(db, user, get_client_or_404(db, client_code), period_end, await parsed_upload(file))
    return filing_json(filing, detail=True)


@app.get("/api/filings/{filing_id}", tags=["filings"])
def api_filing(filing_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return filing_json(get_filing_or_404(db, filing_id), detail=True)


@app.post("/api/filings/{filing_id}/mapping", tags=["filings"])
def api_confirm_mapping(
    filing_id: int, body: MappingIn, user: User = Depends(require_roles(Role.MAKER)), db: Session = Depends(get_db)
):
    filing = get_filing_or_404(db, filing_id)
    wf.confirm_mapping(db, user, filing, body.columns)
    return filing_json(filing, detail=True)


@app.post("/api/filings/{filing_id}/submit", tags=["filings"])
def api_submit(filing_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return filing_json(wf.submit_filing(db, user, get_filing_or_404(db, filing_id)))


@app.post("/api/filings/{filing_id}/approve", tags=["filings"])
def api_approve(
    filing_id: int, comment: str = Form(""), user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return filing_json(wf.approve_filing(db, user, get_filing_or_404(db, filing_id), comment))


@app.post("/api/filings/{filing_id}/return", tags=["filings"])
def api_return(
    filing_id: int, comment: str = Form(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return filing_json(wf.return_filing(db, user, get_filing_or_404(db, filing_id), comment))


@app.post("/api/filings/{filing_id}/send-to-client", tags=["client sign-off"])
def api_send_to_client(filing_id: int, user: User = Depends(require_roles(Role.MAKER)), db: Session = Depends(get_db)):
    return filing_json(wf.send_to_client(db, user, get_filing_or_404(db, filing_id)))


@app.post("/api/filings/{filing_id}/client-decision", tags=["client sign-off"])
def api_client_decision(
    filing_id: int,
    decision: str = Form(...),
    contact_name: str = Form(...),
    comment: str = Form(""),
    user: User = Depends(require_roles(Role.MAKER)),
    db: Session = Depends(get_db),
):
    filing = get_filing_or_404(db, filing_id)
    return filing_json(wf.record_client_decision(db, user, filing, decision == "approved", contact_name, comment))


@app.post("/api/filings/{filing_id}/filed", tags=["client sign-off"])
def api_filed(
    filing_id: int,
    reference: str = Form(...),
    user: User = Depends(require_roles(Role.MAKER)),
    db: Session = Depends(get_db),
):
    return filing_json(wf.record_filed(db, user, get_filing_or_404(db, filing_id), reference))


@app.get("/api/audit/verify", tags=["audit"])
def api_audit_verify(user: User = Depends(require_roles(Role.CHECKER)), db: Session = Depends(get_db)):
    status = verify_chain(db)
    return {"valid": status.valid, "entries": status.entries, "broken_at": status.broken_at}


__all__ = ["Severity", "app"]
