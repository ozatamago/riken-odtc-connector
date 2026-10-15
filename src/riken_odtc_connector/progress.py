"""Send Observable Command progress from the event loop, following the backend (worker thread) as it runs.

In the 2026-10-15 door open/close tests, progress updates were called directly from the worker thread, so the client
received them only all at once at the end. Updates are now sent from a periodic task on the event loop.
"""

from __future__ import annotations

import asyncio
import datetime

from unitelabs.cdk import sila

from .fluent_backend import FluentOdtcBackend

STARTUP_S = 40.0
"""Approximate seconds for a FluentControl run to start (including initialisation). Measured 18-48 s on 2026-10-15."""


async def run_with_progress(
    backend: FluentOdtcBackend,
    macro: str,
    settings: str | None,
    status: sila.Status,
    expected_s: float,
) -> float:
    """Run the macro and send progress and remaining time every 2 s. Returns the seconds the macro itself took.

    Progress 0-0.1 is the run start, 0.1-0.95 the macro (elapsed time scaled against expected_s), 1.0 completion.
    """
    loop = asyncio.get_running_loop()
    macro_started: list[float] = []

    def report(step: str) -> None:  # called from the backend thread
        if step == macro or step == "dry-run":
            loop.call_soon_threadsafe(macro_started.append, loop.time())

    async def ticker() -> None:
        t0 = loop.time()
        while True:
            if not macro_started:
                elapsed = loop.time() - t0
                status.update(
                    progress=min(elapsed / STARTUP_S, 1.0) * 0.1,
                    remaining_time=datetime.timedelta(seconds=max(STARTUP_S - elapsed, 0) + expected_s),
                )
            else:
                elapsed = loop.time() - macro_started[0]
                fraction = min(elapsed / expected_s, 1.0) if expected_s > 0 else 1.0
                status.update(
                    progress=0.1 + 0.85 * fraction,
                    remaining_time=datetime.timedelta(seconds=max(expected_s - elapsed, 0)),
                )
            await asyncio.sleep(2)

    task = asyncio.create_task(ticker())
    try:
        elapsed = await backend.run_macro(macro, settings, report)
    finally:
        task.cancel()
    status.update(progress=1.0, remaining_time=datetime.timedelta(0))
    return elapsed
