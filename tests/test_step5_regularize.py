import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone

import pytest

from app.main import db
from tests.conftest import bson_dt, ms

D = "2026-07-07"  # a Tuesday
EMP = "EMP0001"
BY = "hr.admin"
REASON = "biometric glitch"

client_ref = [None]


@pytest.fixture(autouse=True)
def _bind(client):
    client_ref[0] = client


@pytest.fixture
def emp(make_employee):
    make_employee(EMP)  # 09:30-18:30
    return EMP


def pin(iso, emp=EMP):
    r = client_ref[0].post("/attendance/punch-in", json={"emp_code": emp, "punched_at": ms(iso)})
    assert r.status_code in (200, 201), r.text


def pout(iso, emp=EMP):
    return client_ref[0].post("/attendance/punch-out", json={"emp_code": emp, "punched_at": ms(iso)})


def closed(pi=f"{D}T10:05:00", po=f"{D}T18:30:00"):
    pin(pi)
    r = pout(po)
    assert r.status_code == 200, r.text


def patch(body, emp=EMP, date=D, by=BY, reason=REASON, **kw):
    b = {"reason": reason, "regularized_by": by}
    b.update(body)
    b.update(kw)
    return client_ref[0].patch(f"/attendance/{emp}/{date}", json=b)


def stored(emp=EMP, date=D):
    return db.attendance_logs.find_one({"emp_code": emp, "date": date})


def to_ms(v):
    if v is None or isinstance(v, int):
        return v
    return int(v.replace(tzinfo=v.tzinfo or timezone.utc).timestamp() * 1000)


def raw(**f):
    base = {"emp_code": EMP, "date": D, "status": "PRESENT", "punch_in": bson_dt(f"{D}T10:05:00"),
            "punch_out": bson_dt(f"{D}T18:30:00"), "work_hours": 8.42, "late_minutes": 35,
            "overtime_minutes": 0, "half_day": False, "history": []}
    base.update(f)
    return base


# ---------- 1, 2: sample #8 and append ----------
def test_sample_8_punch_in_correction(emp):
    closed()
    before = client_ref[0]
    assert stored()["work_hours"] == 8.42 and stored()["late_minutes"] == 35  # 8h25m=8.4167; 35 late
    t0 = int(time.time() * 1000)
    r = patch({"punch_in": ms(f"{D}T09:28:00")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["late_minutes"] == 0
    assert j["work_hours"] == 9.03  # 09:28 -> 18:30 = 9h02m = 9.0333
    assert j["punch_in"] == ms(f"{D}T09:28:00")
    assert j["overtime_minutes"] == 0 and j["half_day"] is False
    assert len(j["history"]) == 1
    h = j["history"][0]
    assert h["by"] == BY and h["reason"] == REASON
    assert isinstance(h["at"], int) and t0 - 2000 <= h["at"] <= int(time.time() * 1000) + 2000
    assert set(h["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    assert h["changes"]["punch_in"] == {"from": ms(f"{D}T10:05:00"), "to": ms(f"{D}T09:28:00")}
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert h["changes"]["work_hours"] == {"from": 8.42, "to": 9.03}
    s = stored()
    assert s["late_minutes"] == 0 and s["work_hours"] == 9.03 and len(s["history"]) == 1


def test_second_patch_appends(emp):
    closed()
    first = patch({"punch_in": ms(f"{D}T09:28:00")}).json()["history"][0]
    r = patch({"punch_out": ms(f"{D}T18:45:00")}, by="other", reason="second fix")
    assert r.status_code == 200
    hist = r.json()["history"]
    assert len(hist) == 2
    assert hist[0] == first
    assert hist[1]["by"] == "other" and hist[1]["reason"] == "second fix"
    assert set(hist[1]["changes"]) == {"punch_out", "work_hours"}  # 15 min OT < 30 -> overtime unchanged
    assert len(stored()["history"]) == 2


# ---------- 3: PRESENT -> ABSENT ----------
def test_present_to_absent_closed(emp):
    closed()
    r = patch({"status": "ABSENT"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "ABSENT"
    assert j["punch_in"] is None and j["punch_out"] is None and j["work_hours"] is None
    assert j["late_minutes"] == 0 and j["overtime_minutes"] == 0 and j["half_day"] is False
    ch = j["history"][0]["changes"]
    # overtime 0->0 and half_day False->False did not change
    assert set(ch) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}
    assert ch["status"] == {"from": "PRESENT", "to": "ABSENT"}
    assert ch["punch_in"] == {"from": ms(f"{D}T10:05:00"), "to": None}
    assert ch["punch_out"] == {"from": ms(f"{D}T18:30:00"), "to": None}
    assert ch["work_hours"] == {"from": 8.42, "to": None}
    assert ch["late_minutes"] == {"from": 35, "to": 0}


def test_to_leave_resets_overtime_and_half_day(emp):
    # in 09:28 out 13:45 -> 4h17m = 4.28 half day; separate: overtime case
    closed(f"{D}T09:28:00", f"{D}T19:10:00")  # OT 40
    r = patch({"status": "LEAVE"})
    assert r.status_code == 200
    ch = r.json()["history"][0]["changes"]
    assert ch["overtime_minutes"] == {"from": 40, "to": 0}
    assert "late_minutes" not in ch and "half_day" not in ch


# ---------- 4: LEAVE -> PRESENT ----------
def test_leave_to_present(emp):
    db.attendance_logs.insert_one(raw(status="LEAVE", punch_in=None, punch_out=None, work_hours=None,
                                      late_minutes=0))
    r = patch({"status": "PRESENT", "punch_in": ms(f"{D}T09:50:00"), "punch_out": ms(f"{D}T19:00:00")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "PRESENT"
    assert j["late_minutes"] == 20  # 09:50 is 20 min after 09:30
    assert j["work_hours"] == 9.17  # 9h10m = 9.1667
    assert j["overtime_minutes"] == 30  # 19:00 - 18:30
    assert j["half_day"] is False
    ch = j["history"][0]["changes"]
    assert set(ch) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes"}
    assert ch["work_hours"] == {"from": None, "to": 9.17}
    assert ch["punch_in"] == {"from": None, "to": ms(f"{D}T09:50:00")}
    assert ch["status"] == {"from": "LEAVE", "to": "PRESENT"}
    assert ch["overtime_minutes"] == {"from": 0, "to": 30}


def test_absent_to_present_with_punches(emp):
    db.attendance_logs.insert_one(raw(status="ABSENT", punch_in=None, punch_out=None, work_hours=None,
                                      late_minutes=0))
    r = patch({"status": "WFH", "punch_in": ms(f"{D}T09:30:00")})
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "WFH" and j["punch_out"] is None and j["work_hours"] is None
    assert j["late_minutes"] == 0


# ---------- 5: status only ----------
def test_status_only_change(emp):
    closed()
    r = patch({"status": "WFH"})
    assert r.status_code == 200
    j = r.json()
    assert j["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}
    assert j["punch_in"] == ms(f"{D}T10:05:00") and j["work_hours"] == 8.42 and j["late_minutes"] == 35


# ---------- 6: overtime / half-day ----------
def test_punch_out_correction_overtime(emp):
    closed(f"{D}T09:28:00", f"{D}T18:35:00")
    r = patch({"punch_out": ms(f"{D}T19:10:59")})
    j = r.json()
    assert r.status_code == 200
    assert j["overtime_minutes"] == 40  # 19:10:59 - 18:30 = 40m59s floor
    assert j["work_hours"] == 9.72  # 09:28 -> 19:10:59 = 9h42m59s = 9.7164
    ch = j["history"][0]["changes"]
    assert ch["overtime_minutes"] == {"from": 0, "to": 40}
    assert ch["punch_out"]["to"] == ms(f"{D}T19:10:59")


def test_overtime_boundary_29_59(emp):
    closed(f"{D}T09:28:00", f"{D}T18:35:00")
    j = patch({"punch_out": ms(f"{D}T18:59:59")}).json()
    assert j["overtime_minutes"] == 0  # 29m59s < 30


def test_half_day_correction(emp):
    closed(f"{D}T09:28:00", f"{D}T18:35:00")
    j = patch({"punch_out": ms(f"{D}T13:45:00")}).json()
    assert j["work_hours"] == 4.28  # 4h17m = 4.2833
    assert j["half_day"] is True
    assert j["overtime_minutes"] == 0
    assert j["history"][0]["changes"]["half_day"] == {"from": False, "to": True}


def test_half_day_boundary_4_50_is_not_half(emp):
    closed(f"{D}T09:28:00", f"{D}T18:35:00")
    j = patch({"punch_in": ms(f"{D}T09:30:00"), "punch_out": ms(f"{D}T14:00:00")}).json()
    assert j["work_hours"] == 4.5 and j["half_day"] is False
    j = patch({"punch_out": ms(f"{D}T13:59:00")}).json()  # 4h29m = 4.4833 -> 4.48
    assert j["work_hours"] == 4.48 and j["half_day"] is True


def test_overnight_punch_in_after_midnight_valid(emp, make_employee):
    db.employees.delete_many({})
    make_employee("EMP0009", shift_start="22:00", shift_end="06:00")
    pin("2026-07-07T22:00:00", emp="EMP0009")
    assert pout("2026-07-08T06:00:00", emp="EMP0009").status_code == 200
    r = patch({"punch_in": ms("2026-07-08T00:30:00")}, emp="EMP0009")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["date"] == D
    assert j["work_hours"] == 5.5  # 00:30 -> 06:00
    assert j["late_minutes"] == 150  # 00:30 is 150 min after 22:00 shift start


def test_overnight_punch_in_wrong_day_422(emp, make_employee):
    db.employees.delete_many({})
    make_employee("EMP0009", shift_start="22:00", shift_end="06:00")
    pin("2026-07-07T22:00:00", emp="EMP0009")
    assert pout("2026-07-08T06:00:00", emp="EMP0009").status_code == 200
    assert patch({"punch_in": ms("2026-07-08T07:00:00")}, emp="EMP0009").status_code == 422  # belongs to 07-08
    assert patch({"punch_in": ms("2026-07-06T23:00:00")}, emp="EMP0009").status_code == 422  # belongs to 07-06


# ---------- 7: legacy ----------
def test_legacy_doc_patchable(emp):
    db.attendance_logs.insert_one({"emp_code": EMP, "date": D, "status": "PRESENT",
                                   "punch_in": bson_dt(f"{D}T10:05:00"), "punch_out": bson_dt(f"{D}T18:30:00"),
                                   "work_hours": 8.42, "late_minutes": 35, "overtime_minutes": 0})
    r = patch({"punch_in": ms(f"{D}T09:28:00")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert len(j["history"]) == 1 and j["half_day"] is False
    assert "half_day" not in j["history"][0]["changes"]


def test_legacy_doc_missing_derived_fields(emp):
    db.attendance_logs.insert_one({"emp_code": EMP, "date": D, "status": "PRESENT",
                                   "punch_in": bson_dt(f"{D}T09:30:00")})
    r = patch({"status": "WFH"})
    assert r.status_code == 200
    j = r.json()
    assert j["late_minutes"] == 0 and j["half_day"] is False and len(j["history"]) == 1


# ---------- 8: 422 ----------
@pytest.mark.parametrize("body", [
    {"status": "ABSENT", "punch_in": "IN"},
    {"status": "LEAVE", "punch_out": "OUT"},
    {"status": "ABSENT", "punch_in": "IN", "punch_out": "OUT"},
    {"punch_out": "BEFORE"},                      # punch_out < punch_in
    {"punch_out": "EQUAL"},                       # punch_out == punch_in
    {"punch_out": "OVER24"},                      # > 24h after punch_in
    {"punch_in": "AFTER_OUT"},                    # moves punch_in past stored punch_out
    {"punch_in": "OTHER_DAY"},                    # R1 date mismatch
    {},                                           # nothing changes
    {"status": "PRESENT"},                        # same status
    {"punch_in": "SAME_IN"},                      # same value
    {"punch_in": "SAME_IN", "punch_out": "SAME_OUT", "status": "PRESENT"},
])
def test_422_on_closed_record(emp, body):
    closed()
    iso = {"IN": f"{D}T09:00:00", "OUT": f"{D}T17:00:00", "BEFORE": f"{D}T10:00:00", "EQUAL": f"{D}T10:05:00",
           "OVER24": "2026-07-08T10:05:01", "AFTER_OUT": f"{D}T19:00:00", "OTHER_DAY": "2026-07-08T09:00:00",
           "SAME_IN": f"{D}T10:05:00", "SAME_OUT": f"{D}T18:30:00"}
    b = {k: (ms(iso[v]) if isinstance(v, str) and v in iso else v) for k, v in body.items()}
    before = stored()
    r = patch(b)
    assert r.status_code == 422, r.text
    after = stored()
    assert after == before  # nothing written


def test_422_absent_stored_only_punch_in(emp):
    db.attendance_logs.insert_one(raw(status="ABSENT", punch_in=None, punch_out=None, work_hours=None,
                                      late_minutes=0))
    assert patch({"punch_in": ms(f"{D}T09:30:00")}).status_code == 422  # stays ABSENT + punch_in


def test_422_presence_status_without_punch_in(emp):
    db.attendance_logs.insert_one(raw(status="LEAVE", punch_in=None, punch_out=None, work_hours=None,
                                      late_minutes=0))
    assert patch({"status": "PRESENT"}).status_code == 422
    assert patch({"status": "WFH", "punch_out": ms(f"{D}T18:00:00")}).status_code == 422
    assert patch({"status": "ON_DUTY"}).status_code == 422


def test_422_punch_in_after_open_nothing_changed(emp):
    pin(f"{D}T09:30:00")
    assert patch({"punch_in": ms(f"{D}T09:30:00")}).status_code == 422


def test_422_punch_out_exactly_24h_plus(emp):
    closed()
    assert patch({"punch_out": ms(f"{D}T10:05:00") + 24 * 3600 * 1000 + 1000}).status_code == 422


@pytest.mark.parametrize("date", ["2026-02-30", "2026-13-01", "20260707", "abc"])
def test_422_invalid_path_date(emp, date):
    closed()
    assert patch({"status": "WFH"}, date=date).status_code == 422


@pytest.mark.parametrize("body", [
    {"status": None}, {"punch_in": None}, {"punch_out": None},
    {"reason": "abcd"}, {"reason": "x" * 201}, {"regularized_by": ""}, {"regularized_by": "x" * 51},
    {"status": "BOGUS"}, {"punch_in": "123"}, {"punch_in": 1.5}, {"punch_in": True},
])
def test_422_invalid_fields(emp, body):
    closed()
    b = {"status": "WFH"}
    b.update(body)
    r = patch(b)
    assert r.status_code == 422, r.text


def test_422_missing_required(emp):
    closed()
    c = client_ref[0]
    assert c.patch(f"/attendance/{EMP}/{D}", json={"status": "WFH", "reason": REASON}).status_code == 422
    assert c.patch(f"/attendance/{EMP}/{D}", json={"status": "WFH", "regularized_by": BY}).status_code == 422
    assert c.patch(f"/attendance/{EMP}/{D}", json={}).status_code == 422


def test_reason_length_boundaries(emp):
    closed()
    assert patch({"status": "WFH"}, reason="abcde").status_code == 200
    assert patch({"status": "ON_DUTY"}, reason="y" * 200, by="b" * 50).status_code == 200


# ---------- 404 ----------
def test_404_unknown_employee(emp):
    closed()
    assert patch({"status": "WFH"}, emp="EMP9999").status_code == 404


def test_404_no_record_for_date(emp):
    closed()
    assert patch({"status": "WFH"}, date="2026-07-08").status_code == 404


def test_404_employee_without_logs(emp):
    assert patch({"status": "WFH"}).status_code == 404


# ---------- 9: derived fields ignored ----------
def test_derived_fields_in_body_ignored(emp):
    closed()
    r = patch({"punch_in": ms(f"{D}T09:28:00"), "late_minutes": 999, "work_hours": 1.0,
               "overtime_minutes": 77, "half_day": True, "history": [], "emp_code": "EMP7777",
               "date": "2030-01-01"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["late_minutes"] == 0 and j["work_hours"] == 9.03 and j["overtime_minutes"] == 0
    assert j["half_day"] is False and j["emp_code"] == EMP and j["date"] == D
    assert len(j["history"]) == 1


def test_only_derived_fields_is_no_change_422(emp):
    closed()
    assert patch({"late_minutes": 999}).status_code == 422


# ---------- 10: concurrency ----------
def test_concurrent_patches_no_lost_history(emp):
    closed(f"{D}T09:28:00", f"{D}T18:35:00")
    outs = [f"{D}T18:{30 + i}:00" for i in range(6, 11)]  # 18:36..18:40, all differ from 18:35

    def go(o):
        return patch({"punch_out": ms(o)}).status_code

    with ThreadPoolExecutor(5) as ex:
        codes = list(ex.map(go, outs))
    assert all(c in (200, 409) for c in codes), codes
    assert codes.count(200) >= 1
    s = stored()
    assert len(s["history"]) == codes.count(200)
    # chain consistency: each entry's "from" equals the previous entry's "to"
    prev = ms(f"{D}T18:35:00")
    for h in s["history"]:
        pc = h["changes"]["punch_out"]
        assert to_ms(pc["from"]) == prev
        prev = to_ms(pc["to"])
    assert prev == to_ms(s["punch_out"])


# ---------- 11: punch-out vs patch ----------
def test_concurrent_punch_out_and_patch(emp):
    pin(f"{D}T09:50:00")
    new_in = ms(f"{D}T09:28:00")
    out_ms = ms(f"{D}T18:30:00")

    def do_patch():
        return patch({"punch_in": new_in}).status_code

    def do_out():
        return pout(f"{D}T18:30:00").status_code

    with ThreadPoolExecutor(2) as ex:
        fp, fo = ex.submit(do_patch), ex.submit(do_out)
        cp, co = fp.result(), fo.result()
    assert cp in (200, 404, 409, 422) and co in (200, 404, 409, 422)
    assert cp != 500 and co != 500
    s = stored()
    if co == 200:
        assert s["punch_out"] is not None
    if cp == 200:
        assert len(s["history"]) == 1
        assert s["punch_in"].replace(tzinfo=s["punch_in"].tzinfo or timezone.utc) == bson_dt(f"{D}T09:28:00")
    if cp == 200 and co == 200:
        # consistent: derived fields match final times (09:28 -> 18:30 = 9h02m = 9.03)
        assert s["work_hours"] == 9.03 and s["late_minutes"] == 0
