# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.


### 1. Indexes
I created a unique index on `employees.emp_code` to prevent duplicate employee codes and a unique compound index on `attendance_logs(emp_code, date)` to guarantee one attendance record per employee per day. I also created an index on `attendance_logs(date, emp_code)` to support date-based filtering and attendance queries. I considered indexing `department` on attendance logs but rejected it because department is stored in the employee collection, so the extra index would not directly help those queries.

### 2. Punch-in race
If two identical punch-in requests arrive simultaneously, both may initially find no attendance record. Both attempt insertion, but the unique compound index allows only one to succeed. The other receives a duplicate-key error, which the API handles rather than creating a second record.

### 3. Ties 
The leaderboard uses competition ranking, so employees tied on late minutes receive the same rank, and the next rank is skipped. The `limit` is applied after ranking, so employees sharing the rank at the cutoff can all be returned. Sorting by `emp_code` makes their order deterministic.

### 4. Headcount 
The department summary starts from employees who joined on or before the month's end, then combines attendance data. Therefore, employees with no attendance records still contribute to headcount.

### 5. One thing I would change
For 100 times more data, I would review aggregation query plans and add targeted indexes, then benchmark the heaviest analytics endpoints to identify bottlenecks before scaling infrastructure.