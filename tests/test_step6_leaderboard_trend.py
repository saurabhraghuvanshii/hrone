import pytest


# ---------------------------------------------------------------- helpers
def log(make_log, code, date, status="PRESENT", late=None, half=None):
    f = {"emp_code": code, "date": date, "status": status}
    if late is not None:
        f["late_minutes"] = late
    if half is not None:
        f["half_day"] = half
    make_log(**f)


def board(client, month="2026-07", **params):
    r = client.get("/analytics/leaderboard/late", params={"month": month, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def trend(client, dept, frm, to):
    r = client.get(f"/analytics/departments/{dept}/trend", params={"from": frm, "to": to})
    return r


def setup_board(make_employee, make_log, totals, dept="Eng"):
    """totals: {code: minutes}; one July log per employee carrying all minutes (inserted in given order)."""
    for code, mins in totals.items():
        make_employee(emp_code=code, department=dept)
        log(make_log, code, "2026-07-06", late=mins)


# ---------------------------------------------------------------- leaderboard
def test_lb_response_shape_and_fields(client, make_employee, make_log):
    make_employee(emp_code="E1", department="Eng", name="Alice")
    log(make_log, "E1", "2026-07-06", late=20)
    log(make_log, "E1", "2026-07-07", late=15)
    log(make_log, "E1", "2026-07-08", late=0)  # not late
    log(make_log, "E1", "2026-07-09")  # missing late_minutes = 0
    r = client.get("/analytics/leaderboard/late", params={"month": "2026-07"})
    assert r.status_code == 200
    assert r.json()["month"] == "2026-07"
    # total 35, late_count 2 (records with late_minutes > 0)
    assert r.json()["items"] == [
        {"rank": 1, "emp_code": "E1", "name": "Alice", "department": "Eng",
         "total_late_minutes": 35, "late_count": 2}
    ]


def test_lb_competition_ranking_excludes_zero(client, make_employee, make_log):
    setup_board(make_employee, make_log, {"E1": 50, "E2": 40, "E3": 40, "E4": 30, "E5": 0})
    items = board(client)
    assert [(i["rank"], i["emp_code"]) for i in items] == [(1, "E1"), (2, "E2"), (2, "E3"), (4, "E4")]


def test_lb_limit_applies_after_ranking(client, make_employee, make_log):
    setup_board(make_employee, make_log, {"E1": 50, "E2": 40, "E3": 40, "E4": 30})
    # limit=2 -> ranks <= 2: E1, E2, E3 (tie at 2)
    assert [(i["rank"], i["emp_code"]) for i in board(client, limit=2)] == [(1, "E1"), (2, "E2"), (2, "E3")]
    # limit=3 -> rank 4 > 3, so same three rows
    assert [i["emp_code"] for i in board(client, limit=3)] == ["E1", "E2", "E3"]
    # limit=4 -> all four
    assert [i["rank"] for i in board(client, limit=4)] == [1, 2, 2, 4]
    assert [i["emp_code"] for i in board(client, limit=1)] == ["E1"]


def test_lb_tie_order_by_emp_code(client, make_employee, make_log):
    setup_board(make_employee, make_log, {"E9": 40, "E5": 40, "E2": 40, "E7": 90})  # reverse code order
    items = board(client)
    assert [(i["rank"], i["emp_code"]) for i in items] == [(1, "E7"), (2, "E2"), (2, "E5"), (2, "E9")]


def test_lb_unknown_employee_logs_ignored(client, make_employee, make_log):
    setup_board(make_employee, make_log, {"E1": 20, "E2": 10})
    log(make_log, "GHOST", "2026-07-06", late=500)
    items = board(client)
    assert [(i["rank"], i["emp_code"]) for i in items] == [(1, "E1"), (2, "E2")]  # ghost takes no rank


def test_lb_department_filter_reranks(client, make_employee, make_log):
    setup_board(make_employee, make_log, {"A1": 100, "A2": 90, "A3": 80}, dept="A")
    setup_board(make_employee, make_log, {"B1": 10, "B2": 5}, dept="B")
    overall = board(client)
    assert [(i["rank"], i["emp_code"]) for i in overall][3:] == [(4, "B1"), (5, "B2")]
    in_b = board(client, department="B")
    assert [(i["rank"], i["emp_code"], i["department"]) for i in in_b] == [(1, "B1", "B"), (2, "B2", "B")]


def test_lb_unknown_department_empty(client, make_employee, make_log):
    setup_board(make_employee, make_log, {"E1": 20})
    assert board(client, department="Nope") == []


def test_lb_weekend_counts_and_outside_month_ignored(client, make_employee, make_log):
    make_employee(emp_code="E1")
    log(make_log, "E1", "2026-07-04", late=30)  # Saturday, counts
    log(make_log, "E1", "2026-06-30", late=100)  # outside
    log(make_log, "E1", "2026-08-01", late=100)  # outside
    log(make_log, "E1", "2026-07-31", late=5)  # last day, counts
    items = board(client)
    assert items[0]["total_late_minutes"] == 35  # 30 + 5
    assert items[0]["late_count"] == 2


def test_lb_month_with_no_data_empty(client, make_employee, make_log):
    setup_board(make_employee, make_log, {"E1": 20})
    assert board(client, month="2026-05") == []


def test_lb_default_limit_10(client, make_employee, make_log):
    setup_board(make_employee, make_log, {f"E{i:02d}": 100 + i for i in range(12)})  # 12 distinct totals
    items = board(client)
    assert len(items) == 10
    assert [i["rank"] for i in items] == list(range(1, 11))
    assert items[0]["emp_code"] == "E11"  # 111 highest


def test_lb_all_tied_exceeds_limit(client, make_employee, make_log):
    setup_board(make_employee, make_log, {f"E{i:02d}": 30 for i in range(12)})
    items = board(client, limit=10)
    assert len(items) == 12 and all(i["rank"] == 1 for i in items)
    assert [i["emp_code"] for i in items] == [f"E{i:02d}" for i in range(12)]


@pytest.mark.parametrize("params", [
    {"month": "2026-07", "limit": 0}, {"month": "2026-07", "limit": 51}, {"month": "2026-07", "limit": "x"},
    {"month": "2026-13"}, {"month": "2026-00"}, {"month": "2026-7"}, {"month": "July"}, {},
])
def test_lb_invalid_params_422(client, params):
    assert client.get("/analytics/leaderboard/late", params=params).status_code == 422


def test_lb_limit_bounds_ok(client, make_employee, make_log):
    setup_board(make_employee, make_log, {"E1": 20})
    assert len(board(client, limit=1)) == 1
    assert len(board(client, limit=50)) == 1


# ---------------------------------------------------------------- trend
def seed_trend(make_employee, make_log):
    make_employee(emp_code="A", department="Eng", joined_on="2026-01-01")
    make_employee(emp_code="B", department="Eng", joined_on="2026-01-01")
    make_employee(emp_code="C", department="Eng", joined_on="2026-07-03")
    make_employee(emp_code="X", department="Ops", joined_on="2026-01-01")
    L = lambda *a, **k: log(make_log, *a, **k)  # noqa: E731
    L("A", "2026-06-30"); L("A", "2026-07-11")  # outside range, ignored
    # d1 Wed 07-01: A (late), B -> 2
    L("A", "2026-07-01", late=10); L("B", "2026-07-01", status="WFH")
    # d2 Thu 07-02: no records
    # d3 Fri 07-03: A, B half day, C -> 2.5
    L("A", "2026-07-03"); L("B", "2026-07-03", half=True); L("C", "2026-07-03", status="ON_DUTY")
    # d4 Sat 07-04: A present on weekend, late 20
    L("A", "2026-07-04", late=20)
    # d6 Mon: A -> 1
    L("A", "2026-07-06")
    # d7 Tue: A, B -> 2
    L("A", "2026-07-07"); L("B", "2026-07-07")
    # d8 Wed: A, B, C -> 3
    for c in "ABC":
        L(c, "2026-07-08")
    # d9 Thu: A half, B -> 1.5
    L("A", "2026-07-09", half=True); L("B", "2026-07-09")
    # d10 Fri: ABSENT/LEAVE -> 0
    L("A", "2026-07-10", status="ABSENT"); L("B", "2026-07-10", status="LEAVE")
    # other dept record: ignored
    L("X", "2026-07-01", late=60); L("X", "2026-07-02")


def test_trend_full_scenario(client, make_employee, make_log):
    seed_trend(make_employee, make_log)
    r = trend(client, "Eng", "2026-07-01", "2026-07-10")
    assert r.status_code == 200
    body = r.json()
    assert body["department"] == "Eng"
    items = body["items"]
    assert [i["date"] for i in items] == [f"2026-07-{d:02d}" for d in range(1, 11)]
    assert [i["is_working_day"] for i in items] == [True, True, True, False, False, True, True, True, True, True]
    # headcount: 2 until C joins on 07-03, then 3
    assert [i["headcount"] for i in items] == [2, 2, 3, 3, 3, 3, 3, 3, 3, 3]
    # present: 2, 0, 2.5, 1 (Sat), 0, 1, 2, 3, 1.5, 0 (ABSENT/LEAVE not present)
    assert [i["present_count"] for i in items] == [2, 0, 2.5, 1, 0, 1, 2, 3, 1.5, 0]
    # late: 07-01 A; 07-04 A (weekend counts); other dept's late ignored
    assert [i["late_count"] for i in items] == [1, 0, 0, 1, 0, 0, 0, 0, 0, 0]
    # rate (working days, 4dp): 2/2=1.0, 0/2=0.0 (no records), 2.5/3=.8333, Sat/Sun null,
    # 1/3=.3333, 2/3=.6667, 3/3=1.0, 1.5/3=.5, 0/3=0.0
    assert [i["attendance_rate"] for i in items] == [1.0, 0.0, 0.8333, None, None, 0.3333, 0.6667, 1.0, 0.5, 0.0]
    # moving avg over non-null rounded rates in window of up to 7 rows:
    # d1 1.0 | d2 (1+0)/2=.5 | d3 1.8333/3=.6111 | d4,d5 same .6111 | d6 2.1666/4=.54165->.5417
    # d7 2.8333/5=.56666->.5667 | d8 rows d2..d8: 2.8333/5=.5667 (d1 dropped)
    # d9 rows d3..d9: 3.3333/5=.66666->.6667 | d10 rows d4..d10: 2.5/5=.5
    assert [i["moving_avg_7d"] for i in items] == [1.0, 0.5, 0.6111, 0.6111, 0.6111, 0.5417, 0.5667, 0.5667, 0.6667, 0.5]


def test_trend_item_keys(client, make_employee, make_log):
    make_employee(emp_code="A", department="Eng")
    item = trend(client, "Eng", "2026-07-01", "2026-07-01").json()["items"][0]
    assert set(item) == {"date", "is_working_day", "headcount", "present_count", "late_count",
                         "attendance_rate", "moving_avg_7d"}


def test_trend_single_day(client, make_employee, make_log):
    make_employee(emp_code="A", department="Eng")
    log(make_log, "A", "2026-07-01")
    items = trend(client, "Eng", "2026-07-01", "2026-07-01").json()["items"]
    assert len(items) == 1
    assert items[0]["attendance_rate"] == 1.0 and items[0]["moving_avg_7d"] == 1.0


def test_trend_single_weekend_day_all_null(client, make_employee, make_log):
    make_employee(emp_code="A", department="Eng")
    item = trend(client, "Eng", "2026-07-04", "2026-07-04").json()["items"][0]
    assert item["is_working_day"] is False
    assert item["attendance_rate"] is None and item["moving_avg_7d"] is None


def test_trend_no_records_zero_rates_on_weekdays(client, make_employee):
    make_employee(emp_code="A", department="Eng")
    items = trend(client, "Eng", "2026-07-01", "2026-07-07").json()["items"]  # Wed..Tue
    assert len(items) == 7
    assert [i["attendance_rate"] for i in items] == [0.0, 0.0, 0.0, None, None, 0.0, 0.0]
    assert all(i["moving_avg_7d"] == 0.0 for i in items)
    assert all(i["present_count"] == 0 and i["late_count"] == 0 for i in items)


def test_trend_headcount_zero_before_anyone_joined(client, make_employee):
    make_employee(emp_code="A", department="Eng", joined_on="2026-07-03")
    items = trend(client, "Eng", "2026-07-01", "2026-07-03").json()["items"]
    assert [i["headcount"] for i in items] == [0, 0, 1]
    # Wed/Thu headcount 0 -> null; Fri hc 1, no record -> 0.0
    assert [i["attendance_rate"] for i in items] == [None, None, 0.0]
    assert [i["moving_avg_7d"] for i in items] == [None, None, 0.0]


def test_trend_rounding_thirds(client, make_employee, make_log):
    for c in "ABC":
        make_employee(emp_code=c, department="Eng")
    log(make_log, "A", "2026-07-01")  # 1/3 -> .3333
    log(make_log, "A", "2026-07-02"); log(make_log, "B", "2026-07-02")  # 2/3 -> .6667
    items = trend(client, "Eng", "2026-07-01", "2026-07-02").json()["items"]
    assert [i["attendance_rate"] for i in items] == [0.3333, 0.6667]
    # average of rounded rates: (.3333+.6667)/2 = .5
    assert items[1]["moving_avg_7d"] == 0.5


def test_trend_moving_avg_uses_only_range_rows(client, make_employee, make_log):
    """Heavy attendance just before `from` must not leak into the first row's window."""
    make_employee(emp_code="A", department="Eng")
    log(make_log, "A", "2026-06-30")  # present before range
    items = trend(client, "Eng", "2026-07-01", "2026-07-02").json()["items"]
    assert [i["moving_avg_7d"] for i in items] == [0.0, 0.0]


def test_trend_unknown_department_404(client, make_employee):
    make_employee(emp_code="A", department="Eng")
    assert trend(client, "Nope", "2026-07-01", "2026-07-02").status_code == 404


def test_trend_range_92_days_ok_93_422(client, make_employee):
    make_employee(emp_code="A", department="Eng")
    r = trend(client, "Eng", "2026-07-01", "2026-09-30")  # 31+31+30 = 92 days
    assert r.status_code == 200 and len(r.json()["items"]) == 92
    assert trend(client, "Eng", "2026-07-01", "2026-10-01").status_code == 422


@pytest.mark.parametrize("frm,to", [
    ("2026-07-10", "2026-07-09"), ("2026-07-01", "bad"), ("bad", "2026-07-02"),
    ("2026-02-30", "2026-03-02"), ("07/01/2026", "07/02/2026"),
])
def test_trend_invalid_dates_422(client, make_employee, frm, to):
    make_employee(emp_code="A", department="Eng")
    assert trend(client, "Eng", frm, to).status_code == 422


def test_trend_missing_dates_422(client, make_employee):
    make_employee(emp_code="A", department="Eng")
    assert client.get("/analytics/departments/Eng/trend").status_code == 422
    assert client.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01"}).status_code == 422
    assert client.get("/analytics/departments/Eng/trend", params={"to": "2026-07-01"}).status_code == 422
