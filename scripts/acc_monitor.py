#!/usr/bin/env python3
"""Cron script to check water (Water provider) disconnections.

Thin runner: delegates to src.monitoring.acc_monitor so the daily timer uses the
same fixed logic (time-aware filtering, content-based email de-dup) and the same
on-disk caches/notified file as the web app. Run by acc-monitor.service.
"""
import sys
import asyncio

sys.path.insert(0, '/home/user/netops-control-plane')

from src.monitoring.acc_monitor import check_acc_status


async def main():
    status = await check_acc_status(force=True)
    print(f"ACC check: OK={status.ok}, matches={len(status.matches)}")


if __name__ == "__main__":
    asyncio.run(main())
