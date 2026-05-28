"""Scheduler subsystem (F12).

`SchedulerManager` wraps an APScheduler 3.x `AsyncIOScheduler` and
registers periodic callbacks that enqueue work into the `jobs` table.
The BL11 jobs worker is the executor — the scheduler does not run
handlers itself; it just stamps queued rows.
"""
