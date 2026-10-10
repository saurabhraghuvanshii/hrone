"""Step 6: GET /analytics/employees/{emp_code}/monthly and /analytics/departments/summary.

Built from RAW docs: analytics must read STORED derived values (work_hours/late/overtime/half_day), never recompute.
July 2026: Jul 1 = Wed, 23 weekdays; Jul 4/5 and 11/12 are weekends. Feb 2026 has 20 weekdays.
"""
import pytest

from tests.conftest import bson_dt

MONTHLY = "/analytics/employees/{}/monthly"
SUMMARY = "/analytics/departments/summary"


@pytest.fixture
def L(make_log):
    def _l(emp, date, status="PRESENT", wh=None, late=0, ot=0, half=False, punch=True):
        doc = dict(emp_code=emp, date=date, status=status, work_hours=wh, late_minutes=late,
                   overtime_minutes=ot, half_day=half, history=[], punch_out=None)
        # punch times deliberately NOT consistent with the stored values
        doc["punch_in"] = bson_dt(f"{date}T09:30:00") if punch and status in ("PRESENT", "WFH", "ON_DUTY") else None
        return make_log(**doc)
    return _l


def monthly(client, emp, month="2026-07"):
    return client.get(MONTHLY.format(emp), params={"month": month} if month is not None else {})


# ---------------- monthly ----------------
def test_monthly_full_mix(client, make_employee, L):
    make_employee("E1", joined_on="2026-01-05")
    L("E1", "2026-07-01", "PRESENT", 9.0)            # Wed +1
    L("E1", "2026-07-02", "WFH", 3.0, half=True)     # Thu +0.5
    L("E1", "2026-07-03", "ON_DUTY", 8.0)            # Fri +1
    L("E1", "2026-07-06", "ABSENT")
    L("E1", "2026-07-07", "LEAVE")
    L("E1", "2026-07-09", "LEAVE")
    L("E1", "2026-07-08", "PRESENT", 9.0, late=15)   # +1, late 15
    L("E1", "2026-07-04", "PRESENT", 9.0, late=20, ot=45)  # Saturday: not in present_days, but in totals
    L("E1", "2026-06-30", "PRESENT", 9.0, late=99, ot=99)  # other months excluded
    L("E1", "2026-08-03", "LEAVE", late=99)
    r = monthly(client, "E1")
    assert r.status_code == 200
    # present = 1+0.5+1+1 = 3.5 ; working 23 ; pct = 3.5/23*100 = 15.2173 -> 15.22
    # leave = 2 ; late_count = 2 (Jul 8, Jul 4) ; late total 35 ; overtime 45
    assert r.json() == {"emp_code": "E1", "month": "2026-07", "working_days": 23, "present_days": 3.5,
                        "leave_days": 2, "late_count": 2, "total_late_minutes": 35,
                        "total_overtime_minutes": 45, "attendance_pct": 15.22}


def test_monthly_uses_stored_values_not_recomputed(client, make_employee, L):
    make_employee("E1")
    # punch_in 09:30 (on time) but stored late=30, half_day True although work_hours 9 -> stored wins
    L("E1", "2026-07-01", "PRESENT", 9.0, late=30, ot=60, half=True)
    j = monthly(client, "E1").json()
    assert j["present_days"] == 0.5 and j["late_count"] == 1 and j["total_late_minutes"] == 30
    assert j["total_overtime_minutes"] == 60


def test_monthly_mid_month_joiner(client, make_employee, L):
    make_employee("E2", joined_on="2026-07-15")
    L("E2", "2026-07-15", "PRESENT", 9.0)
    L("E2", "2026-07-16", "PRESENT", 9.0)
    j = monthly(client, "E2").json()
    # Jul 15-17 (3) + 20-24 (5) + 27-31 (5) = 13 ; 2/13*100 = 15.3846 -> 15.38
    assert j["working_days"] == 13 and j["present_days"] == 2 and j["attendance_pct"] == 15.38


def test_monthly_joined_on_weekend_starts_next_monday(client, make_employee):
    make_employee("E2", joined_on="2026-07-11")  # Saturday
    # Jul 13-17, 20-24, 27-31 = 15
    assert monthly(client, "E2").json()["working_days"] == 15


def test_monthly_joined_last_day(client, make_employee):
    make_employee("E2", joined_on="2026-07-31")  # Friday
    assert monthly(client, "E2").json()["working_days"] == 1


def test_monthly_joined_before_month_feb(client, make_employee):
    make_employee("E2", joined_on="2025-12-01")
    assert monthly(client, "E2", "2026-02").json()["working_days"] == 20


def test_monthly_joined_after_month_end(client, make_employee, L):
    make_employee("E3", joined_on="2026-08-01")
    L("E3", "2026-07-06", "PRESENT", 9.0)
    j = monthly(client, "E3").json()
    assert j["working_days"] == 0 and j["attendance_pct"] is None


def test_monthly_no_logs(client, make_employee):
    make_employee("E4")
    assert monthly(client, "E4").json() == {
        "emp_code": "E4", "month": "2026-07", "working_days": 23, "present_days": 0, "leave_days": 0,
        "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": 0.0}


def test_monthly_legacy_doc_missing_optional_fields(client, make_employee, make_log):
    make_employee("E5")
    make_log(emp_code="E5", date="2026-07-01", status="PRESENT", punch_in=bson_dt("2026-07-01T09:30:00"))
    make_log(emp_code="E5", date="2026-07-02", status="LEAVE")
    j = monthly(client, "E5").json()
    # present 1 (half_day missing = false), leave 1, no late/overtime; 1/23*100 = 4.3478 -> 4.35
    assert (j["present_days"], j["leave_days"], j["late_count"], j["total_late_minutes"],
            j["total_overtime_minutes"], j["attendance_pct"]) == (1, 1, 0, 0, 0, 4.35)


def test_monthly_pct_exact_half_up_3125(client, make_employee, L):
    make_employee("E6", joined_on="2026-07-10")  # Fri Jul 10 + 15 more weekdays = 16
    L("E6", "2026-07-10", "WFH", 3.0, half=True)
    j = monthly(client, "E6").json()
    # 0.5/16*100 = 3.125 exactly -> half-up 3.13
    assert j["working_days"] == 16 and j["present_days"] == 0.5 and j["attendance_pct"] == 3.13


def test_monthly_pct_one_eighth_and_two_thirds(client, make_employee, L):
    make_employee("E7", joined_on="2026-07-22")  # Jul 22-24 (3) + 27-31 (5) = 8
    L("E7", "2026-07-22", "PRESENT", 9.0)
    assert monthly(client, "E7").json()["attendance_pct"] == 12.5  # 1/8*100
    make_employee("E8", joined_on="2026-07-29")  # Jul 29-31 = 3
    L("E8", "2026-07-29", "PRESENT", 9.0)
    L("E8", "2026-07-30", "PRESENT", 9.0)
    assert monthly(client, "E8").json()["attendance_pct"] == 66.67  # 2/3*100 = 66.666..


def test_monthly_weekend_only_presence(client, make_employee, L):
    make_employee("E9")
    L("E9", "2026-07-11", "PRESENT", 9.0, late=5, ot=30)  # Sat
    L("E9", "2026-07-12", "WFH", 9.0, ot=40)              # Sun
    j = monthly(client, "E9").json()
    assert j["present_days"] == 0 and j["attendance_pct"] == 0.0
    assert j["late_count"] == 1 and j["total_late_minutes"] == 5 and j["total_overtime_minutes"] == 70


def test_monthly_unknown_employee_404(client):
    assert monthly(client, "NOPE").status_code == 404


@pytest.mark.parametrize("month", ["2026-13", "2026-7", "0000-01", "2026-00", "2026/07", "202607", "abcd-ef", "2026-07-01"])
def test_monthly_bad_month_422(client, make_employee, month):
    make_employee("E1")
    assert monthly(client, "E1", month).status_code == 422


def test_monthly_missing_month_422(client, make_employee):
    make_employee("E1")
    assert monthly(client, "E1", None).status_code == 422


# ---------------- department summary ----------------
@pytest.fixture
def seeded(make_employee, L):
    for c in ("A", "B", "C"):
        make_employee(c, department="Engineering")
    make_employee("D", department="Engineering", joined_on="2026-08-01")  # excluded from headcount
    make_employee("E", department="HR")
    make_employee("F", department="HR")
    make_employee("G", department="Sales")
    make_employee("H", department="engineering")  # case-sensitive, sorts after "Sales"
    make_employee("O", department="Ops", joined_on="2026-08-01")  # whole dept joins later -> omitted
    L("A", "2026-07-01", "PRESENT", 8.0, late=12)
    L("A", "2026-07-02", "PRESENT", 10.0)
    L("A", "2026-07-03", "PRESENT", None)            # open record: present, excluded from avg
    L("A", "2026-07-07", "ON_DUTY", 6.0)
    L("A", "2026-06-30", "PRESENT", 1.0, late=50)    # other months ignored
    L("A", "2026-08-03", "PRESENT", 1.0)
    L("B", "2026-07-01", "WFH", 3.0, half=True)
    L("B", "2026-07-04", "PRESENT", 5.0, late=8)     # Saturday: in avg and late, not present_days
    L("B", "2026-07-06", "LEAVE")
    L("B", "2026-07-08", "ABSENT", 99.0)             # absent never in avg even with stored hours
    L("GHOST", "2026-07-01", "PRESENT", 20.0, late=50)  # nonexistent employee ignored
    L("E", "2026-07-01", "PRESENT", 9.12)
    L("F", "2026-07-01", "PRESENT", 9.13)
    L("G", "2026-07-01", "LEAVE")
    L("G", "2026-07-02", "LEAVE")


def by_dept(j):
    return {i["department"]: i for i in j["items"]}


def test_summary_full(client, seeded):
    r = client.get(SUMMARY, params={"month": "2026-07"})
    assert r.status_code == 200
    j = r.json()
    assert j["month"] == "2026-07"
    assert [i["department"] for i in j["items"]] == ["Engineering", "HR", "Sales", "engineering"]
    d = by_dept(j)
    # Engineering: headcount A,B,C = 3 (D joins Aug). present: A 1+1+1(open)+1(on duty)=4, B 0.5 -> 4.5 (Sat excluded)
    # avg over records 8,10,6,3,5 = 32/5 = 6.4 (open, LEAVE, ABSENT excluded; per-employee-mean would differ)
    # late: A 12 + B Sat 8 -> 2 / 20 ; leave 1 ; on_duty 1
    assert d["Engineering"] == {"department": "Engineering", "headcount": 3, "present_days": 4.5,
                                "avg_work_hours": 6.4, "late_count": 2, "total_late_minutes": 20,
                                "leave_count": 1, "on_duty_count": 1}
    # HR: 2 present days; avg (9.12+9.13)/2 = 9.125 -> 9.13
    assert d["HR"] == {"department": "HR", "headcount": 2, "present_days": 2, "avg_work_hours": 9.13,
                       "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}
    # Sales: only LEAVE records -> avg null, leave 2
    assert d["Sales"] == {"department": "Sales", "headcount": 1, "present_days": 0, "avg_work_hours": None,
                          "late_count": 0, "total_late_minutes": 0, "leave_count": 2, "on_duty_count": 0}
    # lowercase dept: employee with zero logs still counted
    assert d["engineering"] == {"department": "engineering", "headcount": 1, "present_days": 0,
                                "avg_work_hours": None, "late_count": 0, "total_late_minutes": 0,
                                "leave_count": 0, "on_duty_count": 0}
    assert "Ops" not in d


def test_summary_avg_not_mean_of_employee_means(client, make_employee, L):
    make_employee("A"); make_employee("B")
    L("A", "2026-07-01", "PRESENT", 8.0); L("A", "2026-07-02", "PRESENT", 10.0)
    L("B", "2026-07-01", "PRESENT", 3.0)
    # (8+10+3)/3 = 7.0 ; mean of means (9+3)/2 = 6.0
    assert client.get(SUMMARY, params={"month": "2026-07"}).json()["items"][0]["avg_work_hours"] == 7.0


def test_summary_headcount_boundary_month_end(client, make_employee):
    make_employee("A", joined_on="2026-07-31")  # joined on last day counts
    make_employee("B", joined_on="2026-08-01")  # next day does not
    j = client.get(SUMMARY, params={"month": "2026-07"}).json()
    assert [(i["department"], i["headcount"]) for i in j["items"]] == [("Engineering", 1)]


def test_summary_department_filter(client, seeded):
    j = client.get(SUMMARY, params={"month": "2026-07", "department": "HR"}).json()
    assert [i["department"] for i in j["items"]] == ["HR"] and j["items"][0]["headcount"] == 2
    j = client.get(SUMMARY, params={"month": "2026-07", "department": "engineering"}).json()
    assert [i["department"] for i in j["items"]] == ["engineering"]  # case-sensitive


def test_summary_unknown_department_empty(client, seeded):
    r = client.get(SUMMARY, params={"month": "2026-07", "department": "Nope"})
    assert r.status_code == 200 and r.json()["items"] == []


def test_summary_department_only_joining_later_filtered_empty(client, seeded):
    assert client.get(SUMMARY, params={"month": "2026-07", "department": "Ops"}).json()["items"] == []


def test_summary_no_employees(client):
    r = client.get(SUMMARY, params={"month": "2026-07"})
    assert r.status_code == 200 and r.json() == {"month": "2026-07", "items": []}


def test_summary_legacy_docs(client, make_employee, make_log):
    make_employee("A")
    make_log(emp_code="A", date="2026-07-01", status="PRESENT", punch_in=bson_dt("2026-07-01T09:30:00"))
    i = client.get(SUMMARY, params={"month": "2026-07"}).json()["items"][0]
    # present 1 ; work_hours missing -> excluded from avg -> null ; no late
    assert i["present_days"] == 1 and i["avg_work_hours"] is None and i["late_count"] == 0


@pytest.mark.parametrize("month", ["2026-13", "2026-7", "0000-01", "bad"])
def test_summary_bad_month_422(client, month):
    assert client.get(SUMMARY, params={"month": month}).status_code == 422


def test_summary_missing_month_422(client):
    assert client.get(SUMMARY).status_code == 422
