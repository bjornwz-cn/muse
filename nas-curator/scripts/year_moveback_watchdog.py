#!/usr/bin/env python3
"""Watchdog: keep the year-moveback apply heartbeat fresh.

The apply script's in-loop progress print/heartbeat is unreachable for
real actions (all paths `continue` before it), so the heartbeat would
go stale and the UI would falsely flag the task as "possibly
interrupted". This watchdog polls plan_items for plan #151 every 45s
and rewrites the heartbeat from actual DB state. It exits once the
plan is marked done, or after 8 consecutive polls (~6 min) with zero
progress (apply likely dead; the heartbeat then correctly goes stale).
Runs inside the nas-curator container (has /app and the DB).
"""
import sqlite3, sys, time

sys.path.insert(0, '/app')
from common.progress import write_heartbeat

DB = '/data/nas-curator.db'
PLAN_ID = 151
TOTAL = 16218


def main():
    unchanged = 0
    last_processed = -1
    while True:
        c = sqlite3.connect(DB)
        c.execute('PRAGMA busy_timeout=30000')
        done, skipped, failed = 0, 0, 0
        for status, n in c.execute(
                "SELECT status, COUNT(*) FROM plan_items "
                "WHERE plan_id=? GROUP BY status", (PLAN_ID,)):
            if status == 'done':
                done = n
            elif status == 'skipped':
                skipped = n
            elif status == 'failed':
                failed = n
        plan_status = c.execute("SELECT status FROM plans WHERE id=?",
                                (PLAN_ID,)).fetchone()
        c.close()
        processed = done + skipped
        status = 'done' if (plan_status and plan_status[0] == 'done') \
            else 'running'
        try:
            write_heartbeat('year-moveback', progress_dir='/data/progress',
                            label='年份文件夹移回媒体库',
                            kind='year-moveback', phase='apply',
                            status=status, total=TOTAL, done=processed,
                            failed=failed)
        except Exception as e:  # noqa: BLE001
            print('heartbeat failed: %s' % e, flush=True)
        print('watchdog: processed=%d done=%d skipped=%d failed=%d '
              'plan=%s'
              % (processed, done, skipped, failed,
                 plan_status[0] if plan_status else '?'), flush=True)
        if status == 'done':
            print('watchdog: plan done, exiting', flush=True)
            return 0
        if processed == last_processed:
            unchanged += 1
        else:
            unchanged = 0
        last_processed = processed
        if unchanged >= 8:
            print('watchdog: no progress for ~6 min, exiting', flush=True)
            return 0
        time.sleep(45)


if __name__ == '__main__':
    raise SystemExit(main())
