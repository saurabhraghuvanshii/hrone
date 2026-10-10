import os
from datetime import datetime, timedelta, timezone

os.environ["MONGO_DB"] = os.environ.get("TEST_MONGO_DB", "hrone_test")  # set before app import; never the real DB

import pytest
from fastapi.testclient import TestClient

from app.main import app, db

IST = timezone(timedelta(hours=5, minutes=30))


def ms(iso: str) -> int:
    """'2026-07-06T09:40:01+05:30' -> epoch ms. Naive strings are treated as IST."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return int(dt.timestamp() * 1000)


def bson_dt(iso: str) -> datetime:
    """For inserting raw documents: IST (or explicit offset) ISO string -> aware datetime."""
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:  # runs lifespan -> creates indexes
        yield c


@pytest.fixture(autouse=True)
def clean_db(client):
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield


@pytest.fixture
def make_employee():
    """Insert an employee document directly (like the grader's seed). Returns the document."""

    def _make(emp_code="EMP0001", department="Engineering", shift_start="09:30", shift_end="18:30",
              joined_on="2026-01-05", **extra):
        doc = {
            "emp_code": emp_code,
            "name": extra.pop("name", f"Name {emp_code}"),
            "email": extra.pop("email", f"{emp_code.lower()}@example.com"),
            "department": department,
            "shift_start": shift_start,
            "shift_end": shift_end,
            "joined_on": joined_on,
            "created_at": bson_dt("2026-01-05T10:00:00"),
            **extra,
        }
        db.employees.insert_one(dict(doc))
        return doc

    return _make


@pytest.fixture
def make_log():
    """Insert a raw attendance_logs document. Pass any fields; datetimes as aware datetimes (see bson_dt)."""

    def _make(**fields):
        db.attendance_logs.insert_one(dict(fields))
        return fields

    return _make
