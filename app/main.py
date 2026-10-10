"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Contract: openapi.yaml (paths, status codes, business rules R1-R10). Storage: DATA_MODEL.md.
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: real environment variables take priority over .env

# tz_aware: datetimes come back as aware UTC, so arithmetic with IST values never mixes naive and aware.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=5000
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
GRACE = timedelta(minutes=10)
OVERTIME_MIN_MINUTES = 30
HALF_DAY_HOURS = 4.5
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when an identical index already exists."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Aware datetime -> epoch milliseconds (integer arithmetic, no float rounding)."""
    return None if dt is None else (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]  # "HH:MM" strings compare chronologically


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; an overnight-shift punch before shift_end belongs to the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_at(att_date: str, emp: dict) -> datetime:
    return datetime.combine(date.fromisoformat(att_date), parse_hhmm(emp["shift_start"]), IST)


def shift_end_at(att_date: str, emp: dict) -> datetime:
    day = date.fromisoformat(att_date) + timedelta(days=1 if is_overnight(emp) else 0)
    return datetime.combine(day, parse_hhmm(emp["shift_end"]), IST)


def compute_late_minutes(punch_in: datetime, att_date: str, emp: dict) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then floor minutes since shift start."""
    delta = punch_in - shift_start_at(att_date, emp)
    return delta // timedelta(minutes=1) if delta > GRACE else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded to 2 decimals half-up (Decimal, not float/banker's rounding)."""
    seconds = (punch_out - punch_in) // timedelta(seconds=1)
    return float((Decimal(seconds) / 3600).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, att_date: str, emp: dict) -> int:
    """R3: floor minutes after shift end (next day for overnight shifts), counted only when >= 30."""
    minutes = (punch_out - shift_end_at(att_date, emp)) // timedelta(minutes=1)
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                   att_date: str, emp: dict) -> dict:
    """The API-owned fields (R2-R5) for a record in the given final state."""
    fields = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE_STATUSES:
        return fields
    fields["late_minutes"] = compute_late_minutes(punch_in, att_date, emp)
    if punch_out is not None:
        work_hours = compute_work_hours(punch_in, punch_out)
        fields.update(
            work_hours=work_hours,
            overtime_minutes=compute_overtime(punch_out, att_date, emp),
            half_day=work_hours < HALF_DAY_HOURS,  # R5 uses the rounded value
        )
    return fields


def _api_value(value):
    return to_ms(value) if isinstance(value, datetime) else value


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def serialize_record(doc: dict) -> dict:
    """Mongo document -> API shape: no _id, instants as epoch ms (also inside history), legacy gaps defaulted."""
    history = [
        {
            "at": to_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {
                field: {"from": _api_value(change.get("from")), "to": _api_value(change.get("to"))}
                for field, change in (entry.get("changes") or {}).items()
            },
        }
        for entry in doc.get("history") or []
    ]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": history,
    }


def validation_error(loc: tuple, msg: str, value=None):
    """422 with FastAPI's default validation body, for checks that span several parameters."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


def paginate(collection, query: dict, sort: list, page: int, page_size: int) -> dict:
    """R10: total reflects the filter; a page past the end returns no items without querying."""
    total = collection.count_documents(query)
    skip = (page - 1) * page_size
    docs = [] if skip >= total else list(collection.find(query).sort(sort).skip(skip).limit(page_size))
    return {"docs": docs, "total": total}


# --------------------------------------------------------------------------- #
# Models and validated types
# --------------------------------------------------------------------------- #
def _valid_date(value: str) -> str:
    date.fromisoformat(value)  # ValueError on e.g. 2026-02-30 -> 422
    return value


# [0-9] rather than \d: the pydantic regex engine treats \d as any Unicode digit.
DateStr = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_valid_date)]
HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class RequestModel(BaseModel):
    """Request bodies: optional fields may be omitted but never sent as null. Unknown fields are ignored."""

    @field_validator("*", mode="before")
    @classmethod
    def _reject_null(cls, value):
        if value is None:
            raise ValueError("must not be null")
        return value


class EmployeeIn(RequestModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_not_empty(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(RequestModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(RequestModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


# --------------------------------------------------------------------------- #
# Query builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    if date_from and date_to and date_from > date_to:
        raise validation_error(("query", "date_from"), "date_from must be on or before date_to", date_from)
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)  # unique index on emp_code decides concurrent creates
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {} if department is None else {"department": department}
    result = paginate(db.employees, q, [("emp_code", ASCENDING)], page, page_size)
    return {
        "items": [serialize_employee(d) for d in result["docs"]],
        "total": result["total"],
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_fields(body.status, ts, None, att_date, emp),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index: exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    # Latest punch-in at or before ts, so an overnight shift is found across midnight ((emp_code, punch_in) index).
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= record["punch_in"] or ts - record["punch_in"] > timedelta(hours=24):
        raise validation_error(("body", "punched_at"), "punch-out must be after punch-in and within 24 hours",
                               body.punched_at)
    derived = derived_fields(record["status"], record["punch_in"], ts, record["date"], emp)
    # Conditional update: of two concurrent punch-outs only one still sees punch_out == null.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": derived["work_hours"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    result = paginate(db.attendance_logs, q, ATTENDANCE_SORT, page, page_size)
    return {
        "items": [serialize_record(d) for d in result["docs"]],
        "total": result["total"],
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
