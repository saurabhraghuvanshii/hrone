"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Contract: openapi.yaml (paths, status codes, business rules R1-R10). Storage: DATA_MODEL.md.
"""
import calendar
import json
import os
import threading
import time as clock
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

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


indexes_ready = threading.Event()


def _ensure_indexes_until_reachable() -> None:
    while not indexes_ready.is_set():
        try:
            ensure_indexes()
            indexes_ready.set()
        except ConnectionFailure:
            clock.sleep(1)  # each attempt already waits serverSelectionTimeoutMS


@asynccontextmanager
async def lifespan(_: FastAPI):
    # If MongoDB is unreachable at boot, start anyway and keep retrying in the background (/health stays 503 until the
    # indexes exist) instead of crashing. Any other error (e.g. duplicates blocking a unique index) fails startup.
    try:
        ensure_indexes()
        indexes_ready.set()
    except ConnectionFailure:
        threading.Thread(target=_ensure_indexes_until_reachable, daemon=True).start()
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


TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def record_state(doc: dict) -> dict:
    """The audited fields of a stored record, with legacy gaps defaulted (missing punch/derived values)."""
    return {
        "status": doc["status"],
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
    }


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


class RegularizeIn(RequestModel):
    """Only these fields can be corrected; derived fields sent by a client are ignored."""
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


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
    if not indexes_ready.is_set():
        raise HTTPException(503, "starting: indexes not created yet")
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
    for _ in range(3):
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
        # Compare-and-set on everything the computation read: of two concurrent punch-outs only one still sees
        # punch_out == null, and a correction that moved punch_in in between makes us re-read instead of writing
        # derived values computed from the old punch_in.
        updated = db.attendance_logs.find_one_and_update(
            {"_id": record["_id"], "punch_out": None, "punch_in": record["punch_in"], "status": record["status"]},
            {"$set": {
                "punch_out": ts,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return serialize_record(updated)
    raise HTTPException(409, "record was modified concurrently; retry")


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no attendance record for this date")

    def invalid(field: str, msg: str):
        return validation_error(("body", field), msg, getattr(body, field, None))

    status = body.status or record["status"]
    if status in PRESENCE_STATUSES:
        punch_in = from_ms(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        punch_out = from_ms(body.punch_out) if body.punch_out is not None else record.get("punch_out")
        if punch_in is None:
            raise invalid("punch_in", f"status {status} requires a punch_in")
        if body.punch_in is not None and attendance_date(punch_in, emp) != date:
            raise invalid("punch_in", "punch_in must stay on the record's attendance date")
        if punch_out is not None and not (punch_in < punch_out <= punch_in + timedelta(hours=24)):
            raise invalid("punch_out", "punch_out must be after punch_in and within 24 hours")
    else:
        if body.punch_in is not None or body.punch_out is not None:
            raise invalid("status", f"status {status} clears punch times; do not send punch_in/punch_out")
        punch_in = punch_out = None

    before = record_state(record)
    after = {"status": status, "punch_in": punch_in, "punch_out": punch_out,
             **derived_fields(status, punch_in, punch_out, date, emp)}
    if all(after[f] == before[f] for f in ("status", "punch_in", "punch_out")):
        raise invalid("status", "the request changes nothing")

    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {f: {"from": before[f], "to": after[f]} for f in TRACKED_FIELDS if after[f] != before[f]},
    }
    # Optimistic lock: the write only applies if the record is still exactly as read (same editable fields and
    # history length). A concurrent punch-out or correction makes this match nothing -> 409, never a lost entry.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(record.get("history") or [])]},
        },
        {"$set": after, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
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
# Analytics: pipeline building blocks
# --------------------------------------------------------------------------- #
def _valid_month(value: str) -> str:
    date(int(value[:4]), int(value[5:7]), 1)  # rejects year 0000
    return value


Month = Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(_valid_month)]
Limit = Annotated[int, Query(ge=1, le=50)]
MAX_TREND_DAYS = 92
MAX_INT64 = 2**63 - 1

IS_PRESENT = {"$in": ["$status", list(PRESENCE_STATUSES)]}
PRESENT_VALUE = {"$cond": [IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}  # R5, R6
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
WEEKDAY_PRESENT_VALUE = {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}  # R7: present_days counts Mon-Fri only
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MINUTES, 0]}, 1, 0]}


def count_if(condition) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def round_half_up_expr(expr, places: int) -> dict:
    """R8 inside a pipeline. $round is half-to-even, so use floor(x * 10^n + 0.5) / 10^n on Decimal128
    (exact; all reported values are non-negative). Null stays null."""
    scale = 10 ** places
    return {"$divide": [
        {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, scale]}, Decimal128("0.5")]}}, scale]}


def round_half_up(value, places: int):
    """R8 for the few scalars computed in Python."""
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def to_number(value):
    return float(value.to_decimal()) if isinstance(value, Decimal128) else value


def month_bounds(month: str) -> tuple:
    year, mon = int(month[:4]), int(month[5:7])
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def working_days(start: date, end: date) -> int:
    """R7: Mon-Fri between start and end inclusive (0 if start > end)."""
    days = (end - start).days + 1
    if days <= 0:
        return 0
    full_weeks, extra = divmod(days, 7)
    return full_weeks * 5 + sum(1 for i in range(extra) if (start.weekday() + i) % 7 < 5)


def build_employee_monthly(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": WEEKDAY_PRESENT_VALUE},
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": {"$sum": IS_LATE},
            "total_late_minutes": {"$sum": LATE_MINUTES},  # weekend records count too (R7)
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def build_department_summary(month: str, department: Optional[str]) -> list:
    """Starts from employees so that people with no logs still count (R9); logs are joined per employee."""
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    has_work_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [  # emp_code equality + date range -> (emp_code, date) index
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": WEEKDAY_PRESENT_VALUE},
                    "work_hours_sum": {"$sum": {"$cond": [has_work_hours, {"$toDecimal": "$work_hours"}, 0]}},
                    "work_hours_count": count_if(has_work_hours),
                    "late_count": {"$sum": IS_LATE},
                    "total_late_minutes": {"$sum": LATE_MINUTES},
                    "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
                    "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
                }},
            ],
            "as": "stats",
        }},
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$stats.present_days"},
            "work_hours_sum": {"$sum": "$stats.work_hours_sum"},
            "work_hours_count": {"$sum": "$stats.work_hours_count"},
            "late_count": {"$sum": "$stats.late_count"},
            "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
            "leave_count": {"$sum": "$stats.leave_count"},
            "on_duty_count": {"$sum": "$stats.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            # Mean over records (not of per-employee means): total hours / total records.
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                round_half_up_expr({"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2),
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
    ]


def build_late_leaderboard(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    match = {"date": {"$gte": first, "$lte": last}}
    if department is not None:  # rank inside the department only
        match["emp_code"] = {"$in": db.employees.distinct("emp_code", {"department": department})}
    return [
        {"$match": match},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": LATE_MINUTES}, "late_count": {"$sum": IS_LATE}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},  # logs whose employee does not exist are dropped before ranking
        # Standard competition ranking (1, 2, 2, 4); limit applies to the rank, so ties at the cutoff all stay.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]


def build_department_trend(department: str, date_from: str, date_to: str) -> list:
    if date_to < date_from:
        raise validation_error(("query", "to"), "to must be on or after from", date_to)
    if (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1 > MAX_TREND_DAYS:
        raise validation_error(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days", date_to)
    employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(404, "department not found")
    joined = [e.get("joined_on") or "" for e in employees]
    day_seed = {"present_count": 0, "late_count": 0}
    return [
        {"$match": {"emp_code": {"$in": [e["emp_code"] for e in employees]},
                    "date": {"$gte": date_from, "$lte": date_to}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": PRESENT_VALUE}, "late_count": {"$sum": IS_LATE}}},
        # Seed both ends of the range so $densify produces every day even when the range has no records.
        {"$unionWith": {"pipeline": [{"$documents": [{"_id": date_from, **day_seed}, {"_id": date_to, **day_seed}]}]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": "full"}}},
        {"$set": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        {"$set": {"headcount": {"$size": {"$filter": {  # R9 per day: joined_on <= that day
            "input": {"$literal": joined}, "cond": {"$lte": ["$$this", "$date"]}}}}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            round_half_up_expr({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
            None,
        ]}}},
        # Window = current row + up to 6 preceding rows of this range; $avg skips nulls, all-null -> null.
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": round_half_up_expr("$moving_avg_7d", 4),
        }},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    start = max(first, emp.get("joined_on") or first)  # R7: mid-month joiners count from joined_on
    work_days = working_days(date.fromisoformat(start), date.fromisoformat(last))
    stats = next(db.attendance_logs.aggregate(build_employee_monthly(emp_code, month)), {})
    present_days = stats.get("present_days", 0)
    pct = None if work_days == 0 else Decimal(str(present_days)) * 100 / work_days
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": work_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": round_half_up(pct, 2),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    rows = db.employees.aggregate(build_department_summary(month, department))
    items = [
        {
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": to_number(r["avg_work_hours"]),
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Limit = 10, department: Optional[str] = None):
    rows = db.attendance_logs.aggregate(build_late_leaderboard(month, limit, department))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r.get("name"),
            "department": r.get("department"),
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Annotated[DateStr, Query(alias="from")],
    date_to: Annotated[DateStr, Query(alias="to")],
):
    rows = db.attendance_logs.aggregate(build_department_trend(department, date_from, date_to))
    items = [
        {
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": to_number(r["attendance_rate"]),
            "moving_avg_7d": to_number(r["moving_avg_7d"]),
        }
        for r in rows
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                        "department_trend"]


def require(**params) -> None:
    """422 (FastAPI's validation body) for parameters the explained endpoint requires."""
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise RequestValidationError([
            {"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None} for name in missing
        ])


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[Month] = None,
    department: Optional[str] = None,
    limit: Limit = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    trend_from: Annotated[Optional[DateStr], Query(alias="from")] = None,
    trend_to: Annotated[Optional[DateStr], Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    """Explains the endpoint's main query, built by the same builder the endpoint uses."""
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": build_attendance_query(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": min((page - 1) * page_size, MAX_INT64),  # a page far past the end must not overflow BSON
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", build_employee_monthly(emp_code, month)
        elif endpoint == "department_summary":
            require(month=month)
            collection, pipeline = "employees", build_department_summary(month, department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            collection, pipeline = "attendance_logs", build_late_leaderboard(month, limit, department)
        else:
            require(department=department, **{"from": trend_from, "to": trend_to})
            collection, pipeline = "attendance_logs", build_department_trend(department, trend_from, trend_to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    # Relaxed Extended JSON: Int64, Decimal128, dates and timestamps become plain JSON.
    explain = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
