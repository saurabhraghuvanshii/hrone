# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/punch-out, manual corrections with an audit trail, and analytics built on MongoDB
aggregation pipelines. All application code is in `app/main.py`; the contract is `openapi.yaml`, the stored data model
is `DATA_MODEL.md`.

## Run
Requires Python 3.11+ and MongoDB 6.0+ (developed against MongoDB Atlas / 7.0).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set MONGO_URI and MONGO_DB
python sample_seed.py         # optional: load the nine sample documents
uvicorn app.main:app --port 8000
```

- `MONGO_URI` and `MONGO_DB` are read from the environment; a local `.env` is loaded too, but real environment
  variables win.
- All indexes are created at startup (idempotent); no manual steps.
- `GET /health` returns 200 only when MongoDB answers a ping, otherwise 503.

## Notes
- Instants in the API are epoch milliseconds (strict integers); MongoDB stores them as BSON dates (UTC). The client is
  `tz_aware`, and IST is used only to read wall-clock times for the business rules.
- Uniqueness and races are handled by the database: unique indexes on `employees.emp_code` and
  `attendance_logs.(emp_code, date)`, and compare-and-set updates for punch-out and corrections.
- Analytics are single aggregation pipelines. Rounding is half-up (MongoDB's `$round` rounds half to even, so the
  pipelines round with `floor(x * 10^n + 0.5) / 10^n` on Decimal128).
- Part A findings are in `REVIEW.md`, design answers in `DECISIONS.md`.

## Status
- Done: all CRUD, attendance, regularization and analytics endpoints.
- Pending: `GET /admin/explain/{endpoint}`.
