#!/usr/bin/env python3
"""Cron script to check water disconnections.

Thin runner: delegates to src.monitoring.water_monitor so the daily timer uses the
same fixed logic (time-aware filtering, content-based email de-dup) and the same
on-disk caches/notified file as the web app. Run by water-monitor.service.
"""
import sys
import asyncio

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.monitoring.water_monitor import check_water_status


async def main():
    status = await check_water_status(force=True)
    print(f"Water check: OK={status.ok}, matches={len(status.matches)}")


if __name__ == "__main__":
    asyncio.run(main())
