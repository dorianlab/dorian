import uuid
from pathlib import Path
from typing import List, Optional

import pendulum
from fastapi import APIRouter, File, Form, Query, UploadFile

from backend.envs import expdb
from backend.events import Event, aemit
from backend.rate_limit import http_rate_limit

router = APIRouter()

_DATA_DIR = Path("/app/data/feedback")


async def _insert(doc: dict) -> None:
    await expdb["contact_submissions"].insert_one(doc)


@router.post("/contact/bug")
async def submit_bug_report(
    uid: str = Form(...),
    name: str = Form(""),
    title: str = Form(...),
    description: str = Form(...),
    steps: str = Form(""),
    expected: str = Form(""),
    device: str = Form(""),
    severity: str = Form("low"),
    files: List[UploadFile] = File(default=[]),
    _rl=http_rate_limit("contact"),
):
    submission_id = str(uuid.uuid4())
    attachment_paths: List[str] = []
    if files:
        attach_dir = _DATA_DIR / "bug" / uid / submission_id
        attach_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.filename:
                dest = attach_dir / f.filename
                dest.write_bytes(await f.read())
                attachment_paths.append(f"data/feedback/bug/{uid}/{submission_id}/{f.filename}")

    doc = {
        "_id": submission_id,
        "type": "bug",
        "uid": uid,
        "name": name,
        "submitted_at": str(pendulum.now()),
        "title": title,
        "description": description,
        "steps": steps,
        "expected": expected,
        "device": device,
        "severity": severity,
        "attachments": attachment_paths,
    }
    await _insert(doc)
    await aemit(Event("ContactFormSubmitted", data=doc))
    return {"status": "ok", "submission_id": submission_id}


@router.post("/contact/feedback")
async def submit_feedback(
    uid: str = Form(...),
    name: str = Form(""),
    feedback_type: str = Form(...),
    subject: str = Form(...),
    details: str = Form(...),
    rating: str = Form("5"),
    _rl=http_rate_limit("contact"),
):
    submission_id = str(uuid.uuid4())
    doc = {
        "_id": submission_id,
        "type": "feedback",
        "uid": uid,
        "name": name,
        "submitted_at": str(pendulum.now()),
        "feedback_type": feedback_type,
        "subject": subject,
        "details": details,
        "rating": rating,
    }
    await _insert(doc)
    await aemit(Event("ContactFormSubmitted", data=doc))
    return {"status": "ok", "submission_id": submission_id}


@router.post("/contact/us")
async def submit_contact_us(
    uid: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    subject: str = Form(...),
    message: str = Form(...),
    _rl=http_rate_limit("contact"),
):
    submission_id = str(uuid.uuid4())
    doc = {
        "_id": submission_id,
        "type": "contact",
        "uid": uid,
        "submitted_at": str(pendulum.now()),
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "subject": subject,
        "message": message,
    }
    await _insert(doc)
    await aemit(Event("ContactFormSubmitted", data=doc))
    return {"status": "ok", "submission_id": submission_id}


@router.get("/contact/submissions")
async def list_submissions(
    uid: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    _rl=http_rate_limit("observability"),
):
    query: dict = {}
    if uid:
        query["uid"] = uid
    if type:
        query["type"] = type

    col = expdb["contact_submissions"]
    cursor = col.find(query).sort("submitted_at", -1).limit(limit)
    return [doc async for doc in cursor]
