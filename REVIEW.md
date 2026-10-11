# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | | | | |

Also note anything you looked at and decided was **not** a defect, and why.

## 1. Endpoint defects

| # | Where (function / line)| What is wrong | How you'd notice it (test, input, or symptom)| How you fixed it |
| 1  | `GET /health` | Database connectivity is not verified. | Stop MongoDB and call `/health`; it may still report healthy. | Ping MongoDB and return an appropriate failure response if the database is unavailable. |
| 2  | `POST /employees` - `create_employee()` | Duplicate employee codes can be inserted under concurrent requests. | Send concurrent requests with the same `emp_code`. | Add a unique index on `emp_code` and handle duplicate-key errors. |
| 3  | `GET /employees` - `list_employees()` | Pagination may use an inefficient or incorrect offset. | Request later pages and check for duplicate or missing employees. | Apply validated pagination with `skip` and `limit`. |
| 4  | `GET /employees` - employee count | The total count may not match the active query filters. | Compare `total` across filtered and unfiltered requests. | Count using the same filters as the listing query. |
| 5  | `GET /employees` - sorting | Results may not have deterministic ordering. | Repeat the same request and compare page contents. | Apply a stable sort, such as by `emp_code`. |
| 6  | `POST /attendance/punch-in` - employee validation  | Punch-in may be allowed for an unknown employee. | Submit a punch-in with a nonexistent `emp_code`. | Verify the employee exists before creating attendance. |
| 7  | `POST /attendance/punch-in` - timestamp validation | Truthiness checks may accept invalid timestamp values. | Submit `punched_at: 0`, a string, or a floating-point number. | Validate that timestamps are integer epoch milliseconds within the allowed range. |
| 8  | `POST /attendance/punch-in` - attendance date | The attendance date may use the machine's local timezone instead of IST. | Punch in near midnight UTC and inspect the resulting date. | Derive the attendance date using IST and the overnight-shift rule. |
| 9  | `POST /attendance/punch-in` - duplicate prevention | Concurrent requests can create duplicate attendance records. | Send simultaneous punch-in requests for the same employee and date. | Enforce a unique compound index on `(emp_code, date)` and handle duplicate-key errors.  |
| 10 | `POST /attendance/punch-in` - status validation  | Unsupported attendance statuses may be accepted. | Submit an invalid status such as `HOLIDAY`. | Validate status against the allowed enum.  |
| 11 | `GET /attendance` - `list_attendance()`  | Loading all records into Python before filtering or paginating is inefficient. | Test with a large attendance collection and inspect memory use and response time. | Apply filtering, sorting, pagination, and counting in MongoDB.                          |
| 12 | Employee and attendance responses | MongoDB `_id` or a synthetic identifier may leak into API responses. | Inspect response JSON for internal identifiers.   | Serialize only the documented response fields and exclude `_id`.  |

## 2. Helper-function defects

| # | Where (function / line) | What is wrong  | How you'd notice it (test, input, or symptom) | How you fixed it |
| 1 | `compute_late_minutes()` | Late calculations may mishandle the 10-minute grace period. | Test exactly 10 minutes late and just over 10 minutes late. | Apply the rule that lateness begins only when the delay is strictly greater than 10 minutes, then floor elapsed minutes. |
| 2 | `compute_late_minutes()` | Overnight shifts may use the wrong shift-start date.  | Test a shift spanning midnight.  | Calculate shift boundaries using the correct local shift date. |
| 3 | `compute_work_hours()`  | Ordinary rounding may not follow the required half-up rule.  | Test values near a two-decimal rounding boundary. | Calculate duration in seconds and use decimal half-up rounding to two decimal places.                                    |
| 4 | `compute_overtime_minutes()` | Overtime may be counted even when it is below the required threshold. | Test 29 minutes and exactly 30 minutes after shift end. | Return overtime only when it is at least 30 minutes, flooring the eligible duration.                                     |
| 5 | `compute_overtime_minutes()` | Overnight shifts may calculate shift end against the wrong date. | Test punch-out after midnight for a night shift. | Construct the shift-end instant on the correct calendar date.                                                            |
| 6 | Timestamp conversion helper  | Seconds may be mistakenly accepted as epoch milliseconds.  | Submit a 10-digit epoch-seconds value. | Require integer epoch milliseconds and reject values outside the permitted range.                                        |
| 7 | Date parsing helper | Invalid or noncanonical date strings may pass validation.  | Submit malformed dates or values such as `2026-7-5`.| Validate strict `YYYY-MM-DD` format and actual calendar validity. |
| 8 | Shift-time parsing helper    | Invalid shift times may be accepted.                                  | Submit `25:90` or malformed time strings. | Validate strict `HH:MM` format and valid hour/minute ranges.|

## Reviewed and Not Considered Defects

| # | Where / behavior | Why it is not a defect |
| - | -----------------| ---------------------- |
| 1 | `EmployeeIn` shift defaults | The defaults `09:30` and `18:30` match the API contract. |
| 2 | `PunchInIn` default status | `PRESENT` is the required default status for punch-in. |
| 3 | `compute_late_minutes()` grace-period comparison | Returning zero when elapsed time is exactly 10 minutes is correct; lateness begins only when the delay is strictly greater than 10 minutes. |
| 4 | Initial punch-in attendance values | Setting `punch_out` and `work_hours` to null, `overtime_minutes` to zero, `half_day` to false, and `history` to an empty list is correct for a newly opened attendance record. |
| 5 | Optional filters on listing endpoints | Making employee code, date range, status, and department filters optional is consistent with the API contract. |
| 6 | `load_dotenv()` | Loading local `.env` configuration is allowed, provided actual environment variables take precedence. |