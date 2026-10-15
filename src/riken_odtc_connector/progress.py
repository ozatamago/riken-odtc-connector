"""Observable Command の進捗を、裏側（別スレッド）の経過に合わせてイベントループから送る。

2026-10-15 の扉の開閉では、進捗の更新を別スレッドから直接呼んでいたため、呼び出し側には
終わったときにまとめてしか届かなかった。更新はイベントループ上の定期タスクから送る。
"""

from __future__ import annotations

import asyncio
import datetime

from unitelabs.cdk import sila

from .fluent_backend import FluentOdtcBackend

STARTUP_S = 40.0
"""FluentControl のランの開始（初期化込み）にかかるおおよその秒数。2026-10-15 の実測は 18〜48 秒。"""


async def run_with_progress(
    backend: FluentOdtcBackend,
    macro: str,
    settings: str | None,
    status: sila.Status,
    expected_s: float,
) -> float:
    """マクロを実行しながら、2 秒ごとに進捗と残り時間を送る。マクロ自体にかかった秒数を返す。

    進捗は 0〜0.1 がランの開始、0.1〜0.95 がマクロ（expected_s を目安に経過時間で按分）、1.0 が完了。
    """
    loop = asyncio.get_running_loop()
    macro_started: list[float] = []

    def report(step: str) -> None:  # 裏側のスレッドから呼ばれる
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
