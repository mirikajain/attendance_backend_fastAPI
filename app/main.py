"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# MongoDB configuration
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")

if not MONGO_URI or not MONGO_DB:
    raise RuntimeError(
        "MONGO_URI and MONGO_DB environment variables must be set"
    )

client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=3000,
)

db = client[MONGO_DB]

# Time zones
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application startup and shutdown lifecycle.

    Create database indexes before serving requests.
    Close the MongoDB client during shutdown.
    """
    db.command("ping")
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )
    # Attendance: one record per employee per attendance date.
    db.attendance_logs.create_index(
        [
            ("emp_code", ASCENDING),
            ("date", ASCENDING),
        ],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [
            ("date", DESCENDING),
            ("emp_code", ASCENDING),
        ],
        name="ix_attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [
            ("status", ASCENDING),
            ("date", DESCENDING),
        ],
        name="ix_attendance_status_date",
    )
    yield
    client.close()
app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0
    )
    elapsed_seconds = (punch_in - start).total_seconds()
    if elapsed_seconds <= 10 * 60:
        return 0
    return int(elapsed_seconds // 60)

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(
        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )

def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    seconds = (punch_out - shift_end).total_seconds()
    if seconds < 30 * 60:
        return 0
    return int(seconds // 60)

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


from pydantic import BaseModel, StrictInt, Field
from typing import Optional

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100_000_000_000,
        le=4_102_444_800_000
    )

class RegularizeIn(BaseModel):
    status: Optional[Literal[
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    ]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str
    changed_by: str

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Employee code already exists",
        )

    doc.pop("_id", None)
    return doc



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    ):
    query = {}
    if department is not None:
        query["department"] = department
    # Page numbering starts at 1.
    skip = (page - 1) * page_size
    # Count only records matching the active filters.
    total = db.employees.count_documents(query)
    cursor = (
        db.employees
        .find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": list(cursor),
        "total": total,
        "page": page,
        "page_size": page_size,
    }

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # 1. Check whether employee exists
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(404, "Employee not found")

    # 2. Convert epoch milliseconds to UTC; default to current time
    if body.punched_at is None:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000, tz=timezone.utc
        ).replace(microsecond=0)

    # 3. Calculate attendance date in IST
    local_ts = ts.astimezone(IST)

    shift_start_time = datetime.strptime(
        emp["shift_start"], "%H:%M"
    ).time()

    shift_end_time = datetime.strptime(
        emp["shift_end"], "%H:%M"
    ).time()

    d = local_ts.date()

    # 4. Handle overnight shifts
    if (
        shift_end_time <= shift_start_time
        and local_ts.time() < shift_end_time
    ):
        d -= timedelta(days=1)

    date_str = d.isoformat()

    # 5. Calculate late minutes from the correct shift start
    shift_start = datetime.combine(
        d, shift_start_time, tzinfo=IST
    )

    elapsed_seconds = (local_ts - shift_start).total_seconds()

    late_minutes = (
        int(elapsed_seconds // 60)
        if elapsed_seconds > 10 * 60
        else 0
    )

    # 6. Create the attendance record
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    # 7. Insert safely under concurrent requests
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date"
        )

    # 8. Return timestamps in epoch milliseconds; hide MongoDB _id
    doc["punch_in"] = int(ts.timestamp() * 1000)
    doc.pop("_id", None)

    return doc

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #

from pymongo import ReturnDocument
@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    # 1. Check whether employee exists
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    # 2. Convert epoch milliseconds to UTC; default to current time
    if body.punched_at is None:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc
        ).replace(microsecond=0)
    # 3. Find the most recent punch-in at or before punch-out
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts}
        },
        sort=[("punch_in", -1)]
    )
    if record is None:
        raise HTTPException(
            404, "No punch-in found before this timestamp"
        )
    # 4. Check whether this record is already punched out
    if record.get("punch_out") is not None:
        raise HTTPException(
            409, "This attendance record is already punched out"
        )
    # 5. Normalize punch-in timestamp to UTC
    punch_in_ts = record["punch_in"]
    if punch_in_ts.tzinfo is None:
        punch_in_ts = punch_in_ts.replace(tzinfo=timezone.utc)
    else:
        punch_in_ts = punch_in_ts.astimezone(timezone.utc)
    # 6. Validate punch-out time
    duration_seconds = (ts - punch_in_ts).total_seconds()
    if duration_seconds <= 0:
        raise HTTPException(
            422, "Punch-out must be after punch-in"
        )
    if duration_seconds > 24 * 60 * 60:
        raise HTTPException(
            422, "Punch-out cannot be more than 24 hours after punch-in"
        )
    # 7. Calculate work hours rounded half-up to 2 decimals
    hours = Decimal(int(duration_seconds)) / Decimal(3600)
    work_hours = float(
        hours.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP
        )
    )
    # 8. Get employee shift timings
    shift_start_time = datetime.strptime(
        emp["shift_start"], "%H:%M"
    ).time()
    shift_end_time = datetime.strptime(
        emp["shift_end"], "%H:%M"
    ).time()
    attendance_date = datetime.strptime(
        record["date"], "%Y-%m-%d"
    ).date()
    # 9. Calculate shift end in UTC, including overnight shifts
    end_date = attendance_date
    if shift_end_time <= shift_start_time:
        end_date += timedelta(days=1)
    shift_end_local = datetime.combine(
        end_date, shift_end_time, tzinfo=IST
    )
    shift_end_utc = shift_end_local.astimezone(timezone.utc)
    # 10. Calculate overtime: minimum 30 minutes
    overtime_seconds = (ts - shift_end_utc).total_seconds()
    overtime_minutes = (
        int(overtime_seconds // 60)
        if overtime_seconds >= 30 * 60
        else 0
    )
    # 11. Half-day if rounded work hours are below 4.50
    half_day = work_hours < 4.50
    # 12. Update atomically to prevent concurrent punch-outs
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(
            409, "This attendance record is already punched out"
        )
    # 13. Return timestamps as epoch milliseconds
    updated["punch_in"] = int(
        punch_in_ts.timestamp() * 1000
    )
    updated["punch_out"] = int(
        ts.timestamp() * 1000
    )
    # Never expose MongoDB's internal _id
    updated.pop("_id", None)
    return updated 

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn
):
    # 1. Validate attendance date
    try:
        attendance_date = datetime.strptime(
            date, "%Y-%m-%d"
        ).date()

        if attendance_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid date; use YYYY-MM-DD"
        )

    # 2. Find employee and attendance record
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })
    if record is None:
        raise HTTPException(404, "Attendance record not found")

    # 3. Convert and validate epoch-millisecond timestamps
    def convert_timestamp(value):
        if value is None:
            return None

        if type(value) is not int or not (
            100_000_000_000 <= value <= 4_102_444_800_000
        ):
            raise HTTPException(
                422,
                "Timestamp must be a valid epoch-milliseconds integer"
            )

        try:
            return datetime.fromtimestamp(
                value / 1000,
                tz=timezone.utc
            ).replace(microsecond=0)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(422, "Invalid timestamp")

    # 4. Build proposed updates
    updates = {}

    if body.status is not None:
        updates["status"] = body.status

    if body.punch_in is not None:
        updates["punch_in"] = convert_timestamp(body.punch_in)

    if body.punch_out is not None:
        updates["punch_out"] = convert_timestamp(body.punch_out)

    if not updates:
        raise HTTPException(422, "No changes supplied")

    final_status = updates.get("status", record.get("status"))
    final_in = updates.get("punch_in", record.get("punch_in"))
    final_out = updates.get("punch_out", record.get("punch_out"))

    # Normalize existing naive Mongo datetimes as UTC
    def as_utc(value):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    final_in = as_utc(final_in)
    final_out = as_utc(final_out)

    # 5. Validate status and punch-time combination
    if final_status in ("ABSENT", "LEAVE"):
        if "punch_in" in updates or "punch_out" in updates:
            raise HTTPException(
                422, "ABSENT or LEAVE cannot include punch times"
            )

        final_in = None
        final_out = None
        updates["punch_in"] = None
        updates["punch_out"] = None

    elif final_status in ("PRESENT", "WFH", "ON_DUTY"):
        if final_in is None:
            raise HTTPException(
                422, "Presence status requires punch-in"
            )

    # 6. Validate punch-in date.
    # Overnight early-morning punches belong to the previous shift date.
    if final_in is not None:
        local_in = final_in.astimezone(IST)
        shift_start_time = datetime.strptime(
            emp["shift_start"], "%H:%M"
        ).time()
        shift_end_time = datetime.strptime(
            emp["shift_end"], "%H:%M"
        ).time()

        expected_date = local_in.date()

        if (
            shift_end_time <= shift_start_time
            and local_in.time() < shift_end_time
        ):
            expected_date -= timedelta(days=1)

        if expected_date != attendance_date:
            raise HTTPException(
                422,
                "Punch-in does not belong to the attendance date"
            )

    # 7. Validate punch-out
    if final_in is not None and final_out is not None:
        seconds_between = (
            final_out - final_in
        ).total_seconds()

        if seconds_between <= 0 or seconds_between > 24 * 60 * 60:
            raise HTTPException(
                422,
                "Punch-out must be after punch-in and within 24 hours"
            )

    # If punch-in is absent, punch-out cannot exist
    if final_in is None and final_out is not None:
        raise HTTPException(
            422, "Punch-out requires punch-in"
        )

    # 8. Recalculate late minutes
    if final_in is None:
        updates["late_minutes"] = 0
    else:
        local_in = final_in.astimezone(IST)
        shift_start_time = datetime.strptime(
            emp["shift_start"], "%H:%M"
        ).time()
        shift_end_time = datetime.strptime(
            emp["shift_end"], "%H:%M"
        ).time()

        shift_date = attendance_date
        if (
            shift_end_time <= shift_start_time
            and local_in.time() < shift_end_time
        ):
            shift_date -= timedelta(days=1)

        shift_start = datetime.combine(
            shift_date,
            shift_start_time,
            tzinfo=IST
        )

        elapsed_seconds = (
            local_in.replace(microsecond=0) - shift_start
        ).total_seconds()

        # Strictly more than 10 minutes late
        updates["late_minutes"] = (
            int(elapsed_seconds // 60)
            if elapsed_seconds > 600
            else 0
        )

    # 9. Recalculate work hours, half-day and overtime
    if final_in is None or final_out is None:
        updates["work_hours"] = None
        updates["overtime_minutes"] = 0
        updates["half_day"] = False
    else:
        final_in = final_in.replace(microsecond=0)
        final_out = final_out.replace(microsecond=0)

        work_seconds = int(
            (final_out - final_in).total_seconds()
        )

        work_hours_decimal = (
            Decimal(work_seconds) / Decimal(3600)
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP
        )

        updates["work_hours"] = float(work_hours_decimal)
        updates["half_day"] = work_hours_decimal < Decimal("4.50")

        shift_start_time = datetime.strptime(
            emp["shift_start"], "%H:%M"
        ).time()
        shift_end_time = datetime.strptime(
            emp["shift_end"], "%H:%M"
        ).time()

        shift_end_date = attendance_date
        if shift_end_time <= shift_start_time:
            shift_end_date += timedelta(days=1)

        shift_end = datetime.combine(
            shift_end_date,
            shift_end_time,
            tzinfo=IST
        ).astimezone(timezone.utc)

        overtime_seconds = (
            final_out - shift_end
        ).total_seconds()

        # Overtime is counted only when at least 30 minutes
        updates["overtime_minutes"] = (
            int(overtime_seconds // 60)
            if overtime_seconds >= 1800
            else 0
        )

    # Ensure normalized datetimes are persisted when supplied
    if "punch_in" in updates:
        updates["punch_in"] = final_in
    if "punch_out" in updates:
        updates["punch_out"] = final_out

    # 10. Build audit changes
    changes = {}

    for field, new_value in updates.items():
        old_value = record.get(field)

        if field in ("punch_in", "punch_out"):
            old_normalized = as_utc(old_value)
            new_normalized = as_utc(new_value)

            old_compare = (
                int(old_normalized.timestamp() * 1000)
                if old_normalized is not None else None
            )
            new_compare = (
                int(new_normalized.timestamp() * 1000)
                if new_normalized is not None else None
            )

            if old_compare != new_compare:
                changes[field] = {
                    "before": old_compare,
                    "after": new_compare
                }
        elif old_value != new_value:
            changes[field] = {
                "before": old_value,
                "after": new_value
            }

    if not changes:
        raise HTTPException(
            422, "Request does not change the record"
        )

    # 11. Atomically update record and append audit history
    history_entry = {
        "at": int(datetime.now(timezone.utc).timestamp() * 1000),
        "changed_by": body.changed_by,
        "reason": body.reason,
        "changes": changes
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": record.get("history", [])
        },
        {
            "$set": updates,
            "$push": {"history": history_entry}
        }
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "Attendance record changed concurrently; retry the request"
        )

    # 12. Return response without MongoDB _id
    updated = db.attendance_logs.find_one({
        "_id": record["_id"]
    })

    updated.pop("_id", None)

    for field in ("punch_in", "punch_out"):
        if updated.get(field) is not None:
            updated[field] = int(
                as_utc(updated[field]).timestamp() * 1000
            )

    return updated



from datetime import date, datetime, timedelta

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    # 1. Check that the employee exists
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    # 2. Parse month and calculate its date range
    try:
        start_date = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month; use YYYY-MM")

    next_month = (
        start_date.replace(
            year=start_date.year + 1, month=1
        )
        if start_date.month == 12
        else start_date.replace(month=start_date.month + 1)
    )
    end_date = next_month - timedelta(days=1)

    joined_on = date.fromisoformat(employee["joined_on"])
    effective_start = max(start_date, joined_on)

    # 3. Count Monday-Friday working days from the joining date
    working_days = 0
    current = effective_start
    while current <= end_date:
        if current.weekday() < 5:
            working_days += 1
        current += timedelta(days=1)

    # 4. Calculate attendance using MongoDB aggregation
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": start_date.isoformat(),
                    "$lt": next_month.isoformat()
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$lt": [
                                            {
                                                "$dateFromString": {
                                                    "dateString": "$date",
                                                    "format": "%Y-%m-%d"
                                                }
                                            },
                                            {
                                                "$dateFromString": {
                                                    "dateString": next_month.isoformat(),
                                                    "format": "%Y-%m-%d"
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        "$gte": [
                                            {
                                                "$dateFromString": {
                                                    "dateString": "$date",
                                                    "format": "%Y-%m-%d"
                                                }
                                            },
                                            {
                                                "$dateFromString": {
                                                    "dateString": effective_start.isoformat(),
                                                    "format": "%Y-%m-%d"
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        "$lt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                        "format": "%Y-%m-%d"
                                                    }
                                                }
                                            },
                                            7
                                        ]
                                    },
                                    {
                                        "$gt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                        "format": "%Y-%m-%d"
                                                    }
                                                }
                                            },
                                            1
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    summary = result[0] if result else {}

    present_days = float(summary.get("present_days", 0))
    attendance_pct = None
    if working_days > 0:
        attendance_pct = float(
            (Decimal(str(present_days)) * Decimal("100") /
             Decimal(working_days)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct
    }

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None
):
    # Validate month
    try:
        month_start = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise HTTPException(422, "Invalid month; use YYYY-MM")
    next_month = (
        month_start.replace(year=month_start.year + 1, month=1)
        if month_start.month == 12
        else month_start.replace(month=month_start.month + 1)
    )
    month_end = next_month - timedelta(days=1)
    # Build the cross-collection aggregation from employees so
    # employees with no attendance logs are still included.
    employee_match = {
        "joined_on": {"$lte": month_end.isoformat()}
    }
    if department:
        employee_match["department"] = department
    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lt": ["$date", next_month.isoformat()]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$gt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date",
                                                        "format": "%Y-%m-%d",
                                                        "onError": None,
                                                        "onNull": None
                                                    }
                                                }
                                            },
                                            1
                                        ]
                                    },
                                    {
                                        "$lt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date",
                                                        "format": "%Y-%m-%d",
                                                        "onError": None,
                                                        "onNull": None
                                                    }
                                                }
                                            },
                                            7
                                        ]
                                    }
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                            0
                        ]
                    }
                },
                "work_hours_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            None
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount_codes"},
                "present_days": {"$round": ["$present_days", 2]},
                "avg_work_hours": {
                    "$let": {
                        "vars": {
                            "hours": {
                                "$filter": {
                                    "input": "$work_hours_values",
                                    "as": "h",
                                    "cond": {"$ne": ["$$h", None]}
                                }
                            }
                        },
                        "in": {
                            "$cond": [
                                {"$gt": [{"$size": "$$hours"}, 0]},
                                {"$round": [{"$avg": "$$hours"}, 2]},
                                None
                            ]
                        }
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]

    items = list(db.employees.aggregate(pipeline))

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
    limit: int = Query(10, ge=1, le=100)
):
    from datetime import datetime, timedelta

    # Validate month
    try:
        month_start = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise HTTPException(422, "Invalid month; use YYYY-MM")

    if month_start.strftime("%Y-%m") != month:
        raise HTTPException(422, "Invalid month; use YYYY-MM")

    if month_start.month == 12:
        next_month = month_start.replace(
            year=month_start.year + 1, month=1
        )
    else:
        next_month = month_start.replace(
            month=month_start.month + 1
        )
    start_date = month_start.isoformat()
    end_date = next_month.isoformat()
    # Aggregate late minutes per employee for the month
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lt": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({
            "$match": {"employee.department": department}
        })
    pipeline.extend([
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$limit": limit},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
                "rank": 1
            }
        }
    ])
    items = list(db.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": items
    }

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    from datetime import date, datetime, timedelta

    # Validate date inputs
    try:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "Dates must use YYYY-MM-DD format")

    if start.isoformat() != from_date or end.isoformat() != to_date:
        raise HTTPException(422, "Dates must use YYYY-MM-DD format")

    if end < start:
        raise HTTPException(422, "'to' must be on or after 'from'")

    if (end - start).days > 91:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Department not found")

    # Generate daily rows from the requested date range.
    # This list is only the requested calendar dates, not attendance records.
    days = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)

    items = []

    for day in days:
        weekday = date.fromisoformat(day).weekday() < 5

        headcount = db.employees.count_documents({
            "department": department,
            "joined_on": {"$lte": day}
        })
        # Get employee codes for the department
        employee_codes = [
            e["emp_code"]
            for e in db.employees.find(
                {
                    "department": department,
                    "joined_on": {"$lte": day}
                },
                {"_id": 0, "emp_code": 1}
            )
        ]
        logs = list(db.attendance_logs.find({
            "emp_code": {"$in": employee_codes},
            "date": day
        }))
        present_count = sum(
            0.5 if log.get("half_day") is True else 1
            for log in logs
            if log.get("status") in ("PRESENT", "WFH", "ON_DUTY")
        )
        late_count = sum(
            1 for log in logs if log.get("late_minutes", 0) > 0
        )
        attendance_rate = None
        if weekday and headcount > 0:
            attendance_rate = round(present_count / headcount * 100, 4)
        items.append({
            "date": day,
            "headcount": headcount,
            "present_count": round(present_count, 2),
            "late_count": late_count,
            "attendance_rate": attendance_rate
        })
    # Moving average: current day and up to 6 previous days.
    # Ignore null attendance rates.
    for i, item in enumerate(items):
        window = [
            row["attendance_rate"]
            for row in items[max(0, i - 6):i + 1]
            if row["attendance_rate"] is not None
        ]
        item["moving_avg_7d"] = (
            round(sum(window) / len(window), 4)
            if window else None
        )
    return {
        "department": department,
        "from": from_date,
        "to": to_date,
        "items": items
    }

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    month: str | None = None,
    department: str | None = None,
    emp_code: str | None = None,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to")
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend"
    }
    if endpoint not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of: {', '.join(sorted(allowed))}"
        )
    def get_month_range(value):
        try:
            parsed = datetime.strptime(value, "%Y-%m").date()
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format"
            )
        if parsed.strftime("%Y-%m") != value:
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format"
            )
        if parsed.month == 12:
            next_month = parsed.replace(
                year=parsed.year + 1, month=1
            )
        else:
            next_month = parsed.replace(
                month=parsed.month + 1
            )
        return parsed.isoformat(), next_month.isoformat()
    def validate_date(value, field):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"{field} must use YYYY-MM-DD format"
            )
        if parsed.isoformat() != value:
            raise HTTPException(
                status_code=422,
                detail=f"{field} must use YYYY-MM-DD format"
            )
        return parsed
    # Validate month for month-based endpoints
    month_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard"
    }
    start_date = end_date = None
    if endpoint in month_endpoints:
        if not month:
            raise HTTPException(422, "month is required")
        start_date, end_date = get_month_range(month)
    # 1. Attendance list
    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if month:
            query["date"] = {
                "$gte": start_date,
                "$lt": end_date
            }
    # 2. Employee monthly attendance
    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required")

        query = {
            "emp_code": emp_code,
            "date": {
                "$gte": start_date,
                "$lt": end_date
            }
        }
    # 3. Department summary
    elif endpoint == "department_summary":
        query = {
            "date": {
                "$gte": start_date,
                "$lt": end_date
            }
        }
        if department:
            codes = [
                item["emp_code"]
                for item in db.employees.find(
                    {"department": department},
                    {"_id": 0, "emp_code": 1}
                )
            ]
            query["emp_code"] = {"$in": codes}
    # 4. Late leaderboard
    elif endpoint == "late_leaderboard":
        query = {
            "date": {
                "$gte": start_date,
                "$lt": end_date
            },
            "late_minutes": {"$gt": 0}
        }
        if department:
            codes = [
                item["emp_code"]
                for item in db.employees.find(
                    {"department": department},
                    {"_id": 0, "emp_code": 1}
                )
            ]
            query["emp_code"] = {"$in": codes}
    # 5. Department trend
    else:
        if not department:
            raise HTTPException(422, "department is required")

        if not from_date or not to_date:
            raise HTTPException(
                422, "'from' and 'to' are required"
            )
        start = validate_date(from_date, "from")
        end = validate_date(to_date, "to")
        if end < start:
            raise HTTPException(
                422, "'to' must be on or after 'from'"
            )
        if (end - start).days > 91:
            raise HTTPException(
                422, "Date range cannot exceed 92 days"
            )
        codes = [
            item["emp_code"]
            for item in db.employees.find(
                {"department": department},
                {"_id": 0, "emp_code": 1}
            )
        ]
        query = {
            "emp_code": {"$in": codes},
            "date": {
                "$gte": from_date,
                "$lte": to_date
            }
        }
    # Run explain using the supported PyMongo keyword argument.
    plan = db.command({
    "explain": {
        "find": "attendance_logs",
        "filter": query
    },
    "verbosity": "executionStats"
})
    return {
        "endpoint": endpoint,
        "queryPlanner": plan.get("queryPlanner", {}),
        "executionStats": plan.get("executionStats", {})
    }


