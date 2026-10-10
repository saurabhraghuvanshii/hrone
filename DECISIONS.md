# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (one employee per code, lookups, joins from logs); `(department,
   emp_code)` (department filter sorted by code); `joined_on` (summary headcount match). `attendance_logs`: unique
   `(emp_code, date)` (one record per day, monthly and per-employee queries); `(date desc, emp_code)` (the list sort and
   month ranges); `(emp_code, punch_in desc)` (punch-out finds the latest punch-in). I rejected an index on `status`:
   five values, so it barely narrows anything; status is filtered while walking the date index.

2. **Punch-in race.** There is no "check, then insert". Both requests compute the same attendance date and call
   `insert_one`. The unique `(emp_code, date)` index lets exactly one insert commit; the other gets a duplicate-key
   error, which I turn into 409.

3. **Ties.** Ranking uses `$rank` (1, 2, 2, 4) and `limit` filters on rank, not position. With limit 2 and totals 50,
   40, 40, all three rows come back, ordered by `emp_code`. Cutting by position would drop an equally late person
   arbitrarily.

4. **Headcount.** The pipeline starts from `employees` (joined by month end), looks up each person's logs and unwinds
   with `preserveNullAndEmptyArrays`, so someone with no logs still adds 1 to headcount.

5. **100x.** Keep a per-employee monthly rollup updated on punch-out and corrections, so analytics read one document
   per employee instead of every daily record.
