"""Entry point of the pilot check worker: `python -m pilot_check_worker`.

Runs queued pilot checks (docs/pilot-check.md, "Where runs happen") until
SIGTERM or SIGINT, then cancels what is still going and exits.
"""

from __future__ import annotations

import asyncio
import signal

from config import get_settings
from database import build_database
from logging_config import configure_logging
from services.pilot_check import worker
from services.pilot_check.checks import validate_severities


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.app.log_level)
    validate_severities(settings.pilot_check)
    database = build_database(settings)
    await database.connect()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await worker.serve(database, settings.pilot_check, stop)
    finally:
        await database.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
