#!/usr/bin/env python3
"""Watchdog: keep the year-moveback-batch3 apply heartbeat fresh.

Same role as year_moveback_watchdog.py (plan #151), but parameterized:
takes plan_id, total and heartbeat id from argv, so it works for any
batch-2 apply run. Polls plan_items for the plan every 45s and rewrites
the heartbeat from actual DB state. Exits once the plan is marked done,
or after 8 consecutive polls (~6 min) with zero progress.
Runs inside the nas-curator container (has /app and the DB).
Usage: python3 /data/year_moveback_batch2_watchdog.py <plan_id> <total> [hb_id]
"""
import sqlite3, sys, time

sys.path.insert(0, '/app')
from common.progress import write_heartbeat

DB = '/data/nas-curator.db'
PLAN_ID = int(sys.argv[1])
TOTAL = int(sys.argv[2])
HB_ID = sys.argv[3] if len(sys.argv) > 3 else 'year-moveback-batch3'


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
            write_heartbeat(HB_ID, progress_dir='/data/progress',
                            label='年份文件夹移回媒体库（第三批）',
                            kind='year-moveback-batch3', phase='apply',
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
