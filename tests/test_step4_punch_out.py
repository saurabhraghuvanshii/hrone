from concurrent.futures import ThreadPoolExecutor
from datetime import timezone

import pytest

from app.main import db
from tests.conftest import bson_dt, ms

D = "2026-07-06"  # a Monday


def pin(emp, iso, **kw):
    r = client_ref[0].post("/attendance/punch-in", json={"emp_code": emp, "punched_at": ms(iso), **kw})
    assert r.status_code in (200, 201), r.text
    return r.json()


def pout(emp, iso=None, **kw):
    body = {"emp_code": emp}
    if iso is not None:
        body["punched_at"] = ms(iso)
    body.update(kw)
    return client_ref[0].post("/attendance/punch-out", json=body)


client_ref = [None]


@pytest.fixture(autouse=True)
def _bind(client):
    client_ref[0] = client


@pytest.fixture
def emp(make_employee):
    make_employee("EMP0001")  # 09:30-18:30
    return "EMP0001"


def stored(emp_code="EMP0001", date=D):
    return db.attendance_logs.find_one({"emp_code": emp_code, "date": date})


# ---------- case 1 ----------
def test_normal_day(emp):
    pin(emp, f"{D}T09:28:00")
    r = pout(emp, f"{D}T18:35:00")
    assert r.status_code == 200
    j = r.json()
    # 09:28 -> 18:35 = 9h07m = 9.1167h -> 9.12; 5 min after shift end < 30 -> 0
    assert j["work_hours"] == 9.12
    assert j["overtime_minutes"] == 0
    assert j["half_day"] is False
    assert j["emp_code"] == emp and j["date"] == D
    assert j["punch_in"] == ms(f"{D}T09:28:00")
    assert j["punch_out"] == ms(f"{D}T18:35:00")
    assert j["late_minutes"] == 0
    assert j["history"] == []
    assert set(j) >= {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes",
                      "overtime_minutes", "half_day", "history"}
    assert "_id" not in j


# ---------- case 2 ----------
@pytest.mark.parametrize("out,ot", [
    (f"{D}T18:30:00", 0),   # exactly shift end
    (f"{D}T18:59:59", 0),   # 29m59s -> below 30
    (f"{D}T19:00:00", 30),  # exactly 30
    (f"{D}T19:10:59", 40),  # 40m59s floor -> 40
])
def test_overtime_boundaries(emp, out, ot):
    pin(emp, f"{D}T09:28:00")
    r = pout(emp, out)
    assert r.status_code == 200
    assert r.json()["overtime_minutes"] == ot
    assert stored()["overtime_minutes"] == ot


# ---------- case 3 ----------
def test_work_hours_half_up_9_125(emp):
    pin(emp, f"{D}T09:00:00")
    j = pout(emp, f"{D}T18:07:30").json()
    # 9h07m30s = 32850s / 3600 = 9.125 exactly -> half-up 9.13
    assert j["work_hours"] == 9.13


def test_work_hours_half_up_0_005(emp):
    pin(emp, f"{D}T09:00:00")
    j = pout(emp, f"{D}T09:00:18").json()
    # 18s / 3600 = 0.005 -> half-up 0.01
    assert j["work_hours"] == 0.01


def test_punch_out_millis_truncated(emp):
    pin(emp, f"{D}T09:00:00")
    t = ms(f"{D}T18:07:30")
    r = client_ref[0].post("/attendance/punch-out", json={"emp_code": emp, "punched_at": t + 999})
    assert r.status_code == 200
    j = r.json()
    assert j["punch_out"] == t  # truncated to the second
    assert j["work_hours"] == 9.13
    assert j["punch_out"] % 1000 == 0


# ---------- case 4 ----------
@pytest.mark.parametrize("out,wh,half", [
    (f"{D}T13:00:00", 3.5, True),      # 3.5h
    (f"{D}T13:59:42", 4.5, False),     # 4h29m42s = 16182s = 4.495 -> 4.50 -> not half
    (f"{D}T13:59:41", 4.49, True),     # 4h29m41s = 16181s = 4.4947 -> 4.49 -> half
])
def test_half_day(emp, out, wh, half):
    pin(emp, f"{D}T09:30:00")
    j = pout(emp, out).json()
    assert j["work_hours"] == wh
    assert j["half_day"] is half
    assert stored()["half_day"] is half


def test_half_day_inputs_sanity():
    # guard for the hand-computed seconds above
    assert 4 * 3600 + 29 * 60 + 42 == 16182 and 4 * 3600 + 29 * 60 + 41 == 16181


# ---------- case 5 ----------
def test_overnight_shift(make_employee):
    make_employee("EMP0002", shift_start="22:00", shift_end="06:00")
    pin("EMP0002", "2026-07-06T21:55:00")
    r = pout("EMP0002", "2026-07-07T06:40:00")
    assert r.status_code == 200
    j = r.json()
    assert j["date"] == "2026-07-06"
    # 21:55 -> 06:40 = 8h45m = 8.75h; shift end 06:00 next day -> 40 min overtime
    assert j["work_hours"] == 8.75
    assert j["overtime_minutes"] == 40
    assert j["half_day"] is False
    assert db.attendance_logs.count_documents({"emp_code": "EMP0002"}) == 1


# ---------- case 6 ----------
def test_picks_most_recent_record(emp):
    pin(emp, f"{D}T09:30:00")
    pin(emp, "2026-07-07T09:30:00")
    r = pout(emp, "2026-07-07T18:30:00")
    assert r.status_code == 200 and r.json()["date"] == "2026-07-07"
    assert stored(date="2026-07-07")["punch_out"] is not None
    assert stored(date=D).get("punch_out") is None


def test_punched_at_in_between_closes_earlier_day(emp):
    pin(emp, f"{D}T09:30:00")
    pin(emp, "2026-07-07T09:30:00")
    r = pout(emp, f"{D}T20:00:00")  # after day1 in, before day2 in
    assert r.status_code == 200
    j = r.json()
    assert j["date"] == D
    assert j["work_hours"] == 10.5  # 09:30 -> 20:00
    assert j["overtime_minutes"] == 90  # 18:30 -> 20:00
    assert stored(date="2026-07-07").get("punch_out") is None


# ---------- case 7 ----------
def test_unknown_employee_404(client):
    assert pout("NOPE", f"{D}T18:00:00").status_code == 404


def test_employee_without_records_404(emp):
    assert pout(emp, f"{D}T18:00:00").status_code == 404


def test_punched_at_before_any_punch_in_404(emp):
    pin(emp, f"{D}T09:30:00")
    assert pout(emp, f"{D}T09:29:59").status_code == 404
    assert stored().get("punch_out") is None


def test_second_punch_out_409(emp):
    pin(emp, f"{D}T09:30:00")
    assert pout(emp, f"{D}T18:30:00").status_code == 200
    first = stored()["punch_out"]
    assert pout(emp, f"{D}T19:30:00").status_code == 409
    assert stored()["punch_out"] == first


def test_punched_at_equal_punch_in_422(emp):
    pin(emp, f"{D}T09:30:00")
    assert pout(emp, f"{D}T09:30:00").status_code == 422
    assert stored().get("punch_out") is None


def test_punched_at_same_second_as_punch_in_422(emp):
    pin(emp, f"{D}T09:30:00")
    t = ms(f"{D}T09:30:00")
    r = client_ref[0].post("/attendance/punch-out", json={"emp_code": emp, "punched_at": t + 500})
    assert r.status_code == 422  # truncates to punch_in


def test_exactly_24h_allowed(emp):
    pin(emp, f"{D}T09:30:00")
    r = pout(emp, "2026-07-07T09:30:00")
    assert r.status_code == 200
    assert r.json()["work_hours"] == 24.0


def test_over_24h_422(emp):
    pin(emp, f"{D}T09:30:00")
    assert pout(emp, "2026-07-07T09:30:01").status_code == 422
    assert stored().get("punch_out") is None


@pytest.mark.parametrize("bad", [1783312500, 1783312500.5, "1783312500000", None, True, [], {}])
def test_invalid_punched_at_422(emp, bad):
    pin(emp, f"{D}T09:30:00")
    r = client_ref[0].post("/attendance/punch-out", json={"emp_code": emp, "punched_at": bad})
    assert r.status_code == 422, r.text
    assert stored().get("punch_out") is None


def test_missing_emp_code_422(client):
    assert client.post("/attendance/punch-out", json={}).status_code == 422


# ---------- case 8 ----------
@pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
def test_absent_leave_not_picked(emp, make_log, status):
    make_log(emp_code=emp, date=D, status=status, punch_in=None, punch_out=None, work_hours=None,
             late_minutes=0, overtime_minutes=0, half_day=False, history=[])
    assert pout(emp, f"{D}T18:00:00").status_code == 404


def test_absent_raw_missing_punch_in_field_404(emp, make_log):
    make_log(emp_code=emp, date=D, status="ABSENT")
    assert pout(emp, f"{D}T18:00:00").status_code == 404


def test_absent_ignored_but_earlier_present_used(emp, make_log):
    pin(emp, f"{D}T09:30:00")
    make_log(emp_code=emp, date="2026-07-07", status="LEAVE", punch_in=None, punch_out=None)
    r = pout(emp, "2026-07-07T09:00:00")  # 23.5h after punch_in
    assert r.status_code == 200 and r.json()["date"] == D


# ---------- case 9 ----------
def test_legacy_doc_punch_out(emp, make_log):
    make_log(emp_code=emp, date=D, status="PRESENT", punch_in=bson_dt(f"{D}T09:30:00"))
    r = pout(emp, f"{D}T19:00:00")
    assert r.status_code == 200
    j = r.json()
    assert j["history"] == []
    assert j["late_minutes"] == 0
    assert j["work_hours"] == 9.5
    assert j["overtime_minutes"] == 30
    assert j["half_day"] is False
    assert j["punch_out"] == ms(f"{D}T19:00:00")
    assert pout(emp, f"{D}T20:00:00").status_code == 409


# ---------- case 10 ----------
def test_concurrent_punch_outs(emp):
    pin(emp, f"{D}T09:30:00")
    t = ms(f"{D}T18:30:00")

    def go(i):
        return client_ref[0].post("/attendance/punch-out", json={"emp_code": emp, "punched_at": t + i * 1000})

    with ThreadPoolExecutor(10) as ex:
        res = list(ex.map(go, range(10)))
    codes = sorted(r.status_code for r in res)
    assert codes == [200] + [409] * 9, codes
    winner = next(r for r in res if r.status_code == 200).json()
    doc = stored()
    po = doc["punch_out"]
    if po.tzinfo is None:
        po = po.replace(tzinfo=timezone.utc)
    assert int(po.timestamp() * 1000) == winner["punch_out"]


# ---------- case 11 ----------
def test_late_minutes_preserved_and_history_empty(emp):
    pi = pin(emp, f"{D}T10:15:59")
    assert pi["late_minutes"] == 45
    j = pout(emp, f"{D}T18:30:00").json()
    assert j["late_minutes"] == 45
    assert j["history"] == []
    assert stored()["late_minutes"] == 45
    assert not stored().get("history")
