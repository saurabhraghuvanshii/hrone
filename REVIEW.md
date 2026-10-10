# REVIEW.md

Defects found in the starter `app/main.py`. Line numbers refer to the original starter file. Examples use a 09:30-18:30
shift unless stated, and times are IST.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | module, L22 | `MongoClient` without `tz_aware=True`: stored datetimes come back naive, so subtracting them from aware IST values raises `TypeError`. | `compute_overtime` with a naive `punch_out` raises `TypeError: can't subtract offset-naive and offset-aware datetimes`, which becomes a 500. | `MongoClient(..., tz_aware=True)`. All datetimes are aware UTC internally, and IST is used only to read wall-clock times. |
| 2 | module | No indexes are created. Uniqueness is never enforced, and every list or filter is a collection scan. | Explain shows `COLLSCAN`. Duplicates appear under concurrency (#12, #22). | `ensure_indexes()` runs in the FastAPI lifespan. It is idempotent and creates unique `emp_code` and unique `(emp_code, date)`, plus the query indexes. |
| 3 | `health`, L71 | Returns `ok` without contacting MongoDB, but the contract says 503 when the DB is down. | Stop Mongo and `/health` still returns 200. | Runs `ping`. Any `PyMongoError` returns 503 with `{"detail": ...}`. |
| 4 | `compute_late_minutes`, L35 | Applies the shift time to the instant's own time zone and date (UTC or server-local), not IST. | Punch-in at 10:05 IST, stored as 04:35Z, gives 0 instead of 35. | Shift start is built as an IST datetime on the attendance date and compared with the aware instant. |
| 5 | `compute_late_minutes`, L36-37 | The grace check runs on minutes that were already floored (`minutes > 10`). R2 compares the exact duration with 10:00. | 09:40:01 and 09:40:30 give 0 instead of 10. | `delta > 10 min` on the exact duration, then `delta // 1 min`. |
| 6 | `compute_late_minutes` | No overnight-shift handling. A punch after midnight belongs to the shift that started the day before (R1). | Shift 22:00-06:00, punch-in 00:30 gives 0 instead of 150. | The signature takes the attendance date and the employee. Shift start is on the attendance date, and R1 decides that date. |
| 7 | `compute_work_hours`, L41 | `round()` rounds halves to even and works on a float. R4 says half-up. | 9 h 07 m 30 s gives 9.12 instead of 9.13. 7 m 30 s gives 0.12 instead of 0.13. | Whole seconds as a `Decimal` / 3600, then `quantize(0.01, ROUND_HALF_UP)`. |
| 8 | `compute_overtime`, L47 | No 30-minute minimum (R3). | Punch-out at 18:45 gives 15 instead of 0. | Returns minutes only when `>= 30`, otherwise 0. |
| 9 | `compute_overtime`, L46 | For an overnight shift, the shift end is on the next calendar day. | Shift 22:00-06:00, out at 06:40 the next day, gives 1480 instead of 40. | `shift_end_at()` adds one day when `shift_end <= shift_start`. |
| 10 | all helpers / `punch_in` L102 | Instants are not truncated to whole seconds before computing (R1). | 09:40:00.900 should count as 09:40:00, but sub-second parts leak into the stored and returned values. | `from_ms()` keeps whole seconds only. `now_utc()` drops microseconds. |
| 11 | `create_employee`, L79-83 | Check-then-insert: two concurrent creates both pass `find_one`. | 10 parallel POSTs with the same `emp_code` give several 201s and duplicate documents. | Plain `insert_one`. The unique index rejects the loser and `DuplicateKeyError` becomes 409. |
| 12 | `create_employee`, L82 | `datetime.now()` is naive server-local time, so it is stored shifted by the server's UTC offset. It is also returned as a datetime string instead of epoch ms. | Response has `"created_at": "2026-...T..."`. On an IST server the stored value is 5:30 h off. | `datetime.now(UTC)`, returned through `to_ms()`. |
| 13 | `EmployeeIn`, L54-61 | No validation: `emp_code` format, email, lengths, `HH:MM`, `joined_on` date, `shift_start != shift_end`. | `shift_start = shift_end = "09:30"`, `shift_start = "9:30"` or `joined_on = "2026-02-30"` all give 201 instead of 422. | Pydantic constraints from the OpenAPI schema, plus a model validator for equal shifts. |
| 14 | `list_employees`, L93 | `skip = page * page_size` skips the whole first page. | Seeded 6 employees with `page_size=2`: page 1 starts at EMP0003. | `skip = (page - 1) * page_size`. |
| 15 | `list_employees`, L94 | `total` counts the whole collection and ignores `department` (R10). | `?department=Sales` reports total 6 instead of the Sales count. | `count_documents(q)` with the same filter as the find. |
| 16 | `list_employees`, L95 | No sort, so the order and page contents depend on natural order. | Insert EMP0002 before EMP0001 and page 1 starts with EMP0002. | `.sort("emp_code", 1)`, backed by the unique index. |
| 17 | `list_employees` / `list_attendance` params | `page` and `page_size` are not bounded (R10). | `page=-1` gives a negative skip and a 500. `page_size=0` means no limit and returns everything. `page_size=1000` is accepted. | `page >= 1` and `1 <= page_size <= 100`, so other values give 422. Past the last page, `items` is empty and `total` is still correct. |
| 18 | `punch_in`, L101/113 | Unknown employee: `emp["shift_start"]` on `None` raises `TypeError`. | `emp_code=EMP9999` gives 500 instead of 404. | Checks the employee first and returns 404. |
| 19 | `punch_in`, L102-103 | `fromtimestamp()` gives naive server-local time, and `date` is taken from it, not from IST or R1's overnight rule. | On a UTC server, a punch at 01:00 IST (19:30Z the day before) is dated the previous day. | Aware UTC instant, with `attendance_date()` implementing R1 in IST. |
| 20 | `punch_in` / `PunchInIn`, L64-66, L102 | `punched_at: Optional[int]` is lax, so numeric strings, integral floats, `true` and second-scale values are accepted. `if body.punched_at` also treats 0 as missing. | `"punched_at": "1783312500000"`, `1783312500000.0` and `1783312500` all give 201 instead of 422. | Strict `int` with `100000000000 <= v <= 4102444800000`, `is not None` checks, and explicit `null` rejected. |
| 21 | `PunchInIn.status`, L66 | Free text. Only PRESENT, WFH or ON_DUTY may be created by punch-in. | `"status": "LEAVE"` or `"present"` gives 201. | `Literal["PRESENT", "WFH", "ON_DUTY"]`, so other values give 422. |
| 22 | `punch_in`, L104-118 | Check-then-insert race: the contract says "do not rely on check-then-insert". | 10 parallel identical punch-ins create several records for one day. | Unique `(emp_code, date)` index. `insert_one` then `DuplicateKeyError` gives 409, so exactly one 201 wins. |
| 23 | `punch_in` L119, `list_attendance` L147 | Responses leak an `id` (the ObjectId), and instants are datetimes, not epoch ms. | Response contains `"id": "66a..."` and `"punch_in": "2026-07-06T..."`. | `serialize_record()`: no id, instants converted with `to_ms()`, including `history[].at` and `history[].changes.*.from/to`. |
| 24 | `list_attendance`, L144-146 | Loads every matching document into memory and sorts in Python. Sorting by `date` alone leaves ties unordered (the contract wants `emp_code` as tie-break). | Slow and memory-heavy at 100k records. Rows on the same date come back in arbitrary order. | Database `find().sort([date desc, emp_code asc]).skip().limit()` on the `(date desc, emp_code asc)` index, and `count_documents` for `total`. |
| 25 | `list_attendance`, L126-129 | No validation: date format, `date_from > date_to`, `status` enum. | `date_from=2026-08-01&date_to=2026-07-01` gives 200 instead of 422. `status=FOO` gives 200. | Date pattern plus real-date check, `Status` enum, and a 422 for a reversed range. |
| 26 | `list_attendance` | Legacy documents without `history` or `half_day` are returned with those keys missing. | Seeded sample #9 has no `history` or `half_day` keys in the response. | `serialize_record()` defaults them to `[]` and `false` (and missing `late_minutes`/`overtime_minutes` to 0). |

## Looked at and decided were not defects
- `load_dotenv()` (L20): the default `override=False` already lets real environment variables win over `.env`, as the
  contract requires.
- `compute_work_hours`' formula `(out - in) seconds / 3600` is right; only the rounding was wrong (#7), so the helper is
  kept.
- One module-level `MongoClient` shared by FastAPI's thread pool (sync `def` endpoints) is fine: PyMongo clients are
  thread-safe and pool connections.
- `$gte` / `$lte` on `YYYY-MM-DD` strings (L136-141): ISO dates compare correctly as strings, so a range filter on the
  stored string is correct and can use an index.
- `status_code=201` on `POST /employees` and `POST /attendance/punch-in` matches the contract.
- Starting punch-in records with `overtime_minutes=0`, `half_day=False` and `history=[]` matches DATA_MODEL; it is now
  produced by the shared `derived_fields()` helper.
