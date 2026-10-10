import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from tests.conftest import IST, bson_dt, ms

D = "2026-07-06"  # a Monday


def no_ids(obj):
    if isinstance(obj, dict):
        assert "_id" not in obj and "id" not in obj
        for v in obj.values():
            no_ids(v)
    elif isinstance(obj, list):
        for v in obj:
            no_ids(v)


def emp_body(**over):
    b = {"emp_code": "EMP0001", "name": "Asha", "email": "asha@example.com", "department": "Eng",
         "joined_on": "2026-01-05"}
    b.update(over)
    return b


# ---------- health ----------
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# ---------- create employee ----------
def test_create_employee_ok(client):
    before = int(time.time() * 1000)
    r = client.post("/employees", json=emp_body())
    assert r.status_code == 201
    j = r.json()
    assert set(j) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert j["shift_start"] == "09:30" and j["shift_end"] == "18:30"  # spec defaults
    assert isinstance(j["created_at"], int) and not isinstance(j["created_at"], bool)
    assert before - 5000 <= j["created_at"] <= int(time.time() * 1000) + 5000
    no_ids(j)


def test_create_employee_duplicate_409(client):
    assert client.post("/employees", json=emp_body()).status_code == 201
    assert client.post("/employees", json=emp_body(name="Other")).status_code == 409


def test_create_employee_concurrent(client):
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda _: client.post("/employees", json=emp_body()).status_code, range(10)))
    assert codes.count(201) == 1 and codes.count(409) == 9


@pytest.mark.parametrize("over", [
    {"emp_code": "EMP12"}, {"emp_code": "emp0001"}, {"emp_code": "EMP1234567"},
    {"email": "not-an-email"},
    {"shift_start": "09:00", "shift_end": "09:00"},
    {"shift_start": "24:00"}, {"shift_end": "24:00"},
    {"joined_on": "2026-02-30"},
    {"name": ""},
])
def test_create_employee_422(client, over):
    assert client.post("/employees", json=emp_body(**over)).status_code == 422


def test_create_employee_missing_name_422(client):
    b = emp_body()
    del b["name"]
    assert client.post("/employees", json=b).status_code == 422


# ---------- list employees ----------
@pytest.fixture
def five(make_employee):
    for c, d in [("EMP0003", "Eng"), ("EMP0001", "Eng"), ("EMP0005", "HR"), ("EMP0002", "HR"), ("EMP0004", "Eng")]:
        make_employee(c, department=d)


def test_list_employees_sorted_and_pages(client, five):
    r = client.get("/employees", params={"page": 1, "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0002"]
    assert (r["total"], r["page"], r["page_size"]) == (5, 1, 2)
    r2 = client.get("/employees", params={"page": 2, "page_size": 2}).json()
    assert [e["emp_code"] for e in r2["items"]] == ["EMP0003", "EMP0004"]
    no_ids(r)
    assert isinstance(r["items"][0]["created_at"], int)


def test_list_employees_department_total(client, five):
    r = client.get("/employees", params={"department": "HR", "page_size": 1}).json()
    assert r["total"] == 2 and [e["emp_code"] for e in r["items"]] == ["EMP0002"]
    assert client.get("/employees", params={"department": "hr"}).json()["total"] == 0  # case-sensitive


def test_list_employees_beyond_end(client, five):
    r = client.get("/employees", params={"page": 9, "page_size": 2}).json()
    assert r["items"] == [] and r["total"] == 5


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}])
def test_list_employees_pagination_422(client, params):
    assert client.get("/employees", params=params).status_code == 422


# ---------- punch-in ----------
def punch(client, code="EMP0001", at=None, **extra):
    body = {"emp_code": code, **extra}
    if at is not None:
        body["punched_at"] = at
    return client.post("/attendance/punch-in", json=body)


def test_punch_in_unknown_employee_404(client):
    assert punch(client, "EMP9999", ms(f"{D}T09:30:00")).status_code == 404


@pytest.mark.parametrize("t,late", [
    ("09:30:00", 0), ("09:40:00", 0),   # exactly 10:00 after start -> not late
    ("09:40:01", 10),                   # >10:00 -> floor(10:01/60)=10 minutes since 09:30
    ("10:15:59", 45),                   # 45:59 -> 45
])
def test_punch_in_late_boundaries(client, make_employee, t, late):
    make_employee()
    r = punch(client, at=ms(f"{D}T{t}"))
    assert r.status_code == 201
    assert r.json()["late_minutes"] == late
    assert r.json()["date"] == D


def test_punch_in_truncates_millis(client, make_employee):
    make_employee()
    r = punch(client, at=ms(f"{D}T09:40:00") + 900)  # 09:40:00.900 -> 09:40:00
    assert r.status_code == 201
    assert r.json()["late_minutes"] == 0
    assert r.json()["punch_in"] == ms(f"{D}T09:40:00")


def test_punch_in_defaults(client, make_employee):
    make_employee()
    j = punch(client, at=ms(f"{D}T09:30:00")).json()
    assert set(j) == {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes",
                      "overtime_minutes", "half_day", "history"}
    assert j["status"] == "PRESENT" and j["punch_out"] is None and j["work_hours"] is None
    assert j["overtime_minutes"] == 0 and j["half_day"] is False and j["history"] == []
    assert j["punch_in"] == ms(f"{D}T09:30:00")
    no_ids(j)


def test_punch_in_date_is_ist(client, make_employee):
    make_employee()
    # 2026-07-05T19:30Z == 2026-07-06T01:00 IST
    r = punch(client, at=ms("2026-07-05T19:30:00+00:00"))
    assert r.status_code == 201 and r.json()["date"] == "2026-07-06"


def test_punch_in_overnight_before_start(client, make_employee):
    make_employee(shift_start="22:00", shift_end="06:00")
    r = punch(client, at=ms(f"{D}T21:55:00"))
    assert r.status_code == 201
    assert r.json()["date"] == D and r.json()["late_minutes"] == 0  # before shift start -> not late


def test_punch_in_overnight_after_midnight(client, make_employee):
    make_employee(shift_start="22:00", shift_end="06:00")
    r = punch(client, at=ms("2026-07-07T00:30:00"))
    assert r.status_code == 201
    # 00:30 < 06:00 -> belongs to previous day's shift; 22:00 -> 00:30 = 150 min
    assert r.json()["date"] == D and r.json()["late_minutes"] == 150


def test_punch_in_wfh(client, make_employee):
    make_employee()
    r = punch(client, at=ms(f"{D}T09:30:00"), status="WFH")
    assert r.status_code == 201 and r.json()["status"] == "WFH"


@pytest.mark.parametrize("status", ["LEAVE", "ABSENT", "present", "FOO"])
def test_punch_in_bad_status_422(client, make_employee, status):
    make_employee()
    assert punch(client, at=ms(f"{D}T09:30:00"), status=status).status_code == 422


@pytest.mark.parametrize("bad", [1783312500, 1783312500000.5, "1783312500000", True, None, 4102444800001])
def test_punch_in_bad_punched_at_422(client, make_employee, bad):
    make_employee()
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad})
    assert r.status_code == 422


def test_punch_in_default_now(client, make_employee):
    make_employee()
    before = datetime.now(IST).date().isoformat()
    r = punch(client)
    after = datetime.now(IST).date().isoformat()
    assert r.status_code == 201 and r.json()["date"] in (before, after)


def test_punch_in_duplicate_409(client, make_employee):
    make_employee()
    assert punch(client, at=ms(f"{D}T09:30:00")).status_code == 201
    assert punch(client, at=ms(f"{D}T11:00:00")).status_code == 409
    assert punch(client, at=ms("2026-07-07T09:30:00")).status_code == 201  # different date fine


def test_punch_in_concurrent(client, make_employee):
    make_employee()
    at = ms(f"{D}T09:30:00")
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda _: punch(client, at=at).status_code, range(10)))
    assert codes.count(201) == 1 and codes.count(409) == 9


# ---------- GET /attendance ----------
def raw(code, date, status="PRESENT", **kw):
    d = {"emp_code": code, "date": date, "status": status, "punch_in": bson_dt(f"{date}T09:30:00"),
         "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
         "history": []}
    d.update(kw)
    return d


@pytest.fixture
def logs(make_log):
    make_log(**raw("EMP0002", "2026-07-06"))
    make_log(**raw("EMP0001", "2026-07-06"))
    make_log(**raw("EMP0001", "2026-07-07", "WFH"))
    make_log(**raw("EMP0001", "2026-07-05", "LEAVE", punch_in=None))
    make_log(**raw("EMP0002", "2026-07-08", "WFH"))


def keys(r):
    return [(i["date"], i["emp_code"]) for i in r["items"]]


def test_attendance_sort(client, logs):
    r = client.get("/attendance").json()
    assert keys(r) == [("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001"),
                       ("2026-07-06", "EMP0002"), ("2026-07-05", "EMP0001")]
    assert r["total"] == 5
    no_ids(r)


def test_attendance_filters(client, logs):
    g = lambda **p: client.get("/attendance", params=p).json()
    assert g(emp_code="EMP0002")["total"] == 2
    assert g(status="WFH")["total"] == 2
    r = g(date_from="2026-07-06", date_to="2026-07-07")  # inclusive both ends
    assert r["total"] == 3 and keys(r) == [("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001"),
                                           ("2026-07-06", "EMP0002")]
    r = g(emp_code="EMP0001", status="WFH", date_from="2026-07-07")
    assert r["total"] == 1
    r = g(page=2, page_size=2)
    assert r["total"] == 5 and keys(r) == [("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]


@pytest.mark.parametrize("params", [
    {"date_from": "2026-07-08", "date_to": "2026-07-07"},
    {"date_from": "2026-13-01"}, {"date_to": "2026-13-01"},
    {"status": "FOO"}, {"page": 0}, {"page_size": 101},
])
def test_attendance_422(client, params):
    assert client.get("/attendance", params=params).status_code == 422


def test_attendance_legacy_doc(client, make_log):
    make_log(emp_code="EMP0001", date=D, status="PRESENT", punch_in=bson_dt(f"{D}T09:31:00"),
             punch_out=None, work_hours=None, late_minutes=1, overtime_minutes=0)
    it = client.get("/attendance").json()["items"][0]
    assert it["history"] == [] and it["half_day"] is False
    assert it["punch_in"] == ms(f"{D}T09:31:00") and isinstance(it["punch_in"], int)


def test_attendance_history_datetimes_as_ints(client, make_log):
    make_log(**raw("EMP0001", D, punch_in=bson_dt(f"{D}T09:30:00"), history=[{
        "at": bson_dt("2026-07-07T10:00:00"), "by": "hr", "reason": "fix time",
        "changes": {"punch_in": {"from": bson_dt(f"{D}T09:45:00"), "to": bson_dt(f"{D}T09:30:00")}}}]))
    h = client.get("/attendance").json()["items"][0]["history"][0]
    assert h["at"] == ms("2026-07-07T10:00:00") and isinstance(h["at"], int)
    assert h["changes"]["punch_in"]["from"] == ms(f"{D}T09:45:00")
    assert h["changes"]["punch_in"]["to"] == ms(f"{D}T09:30:00")
    assert isinstance(h["changes"]["punch_in"]["from"], int)
