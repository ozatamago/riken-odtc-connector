"""単体の Infinite 200 PRO を、PyLabRobot で USB 直結して動かす裏側。

Fluent に組み込まれた Infinite には使わない。Fluent PC ではリーダが Tecan 独自の USB ドライバ（Tecanat_Usb）に
つながっていて、PyLabRobot（pyusb / libusb）からは開けない（2026-10-15 に確認）。単体のリーダを Mac や Linux の
PC に USB でつないで使う。

PyLabRobot の Infinite のバックエンドは実験的な扱い（ExperimentalTecanInfinite200ProBackend、M シリーズのみ）。
Feature の側から見た関数名は reader.py（Fluent 経由）と同じにしてある。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import time

from unitelabs.cdk.config import Config

from ..fluent_backend import BackendError
from .asc import ROWS, AscResult

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class UsbInfiniteConfig(Config):
    """USB でつないだ単体の Infinite 200 PRO の設定。"""

    plate_model: str = "cor_96_wellplate_360uL_Fb"
    """PyLabRobot のプレートの定義の名前。既定は Costar 96 Flat Transparent（Fluent の Magellan の結果に出ていたもの）。"""
    flashes: int = 25
    """1 ウェルあたりのフラッシュ数（PyLabRobot の既定）。"""
    dry_run: bool = True
    """True のあいだはリーダにつながず、命令をログに出して NaN を返す。"""


class PyLabRobotInfiniteReader:
    kind = "pylabrobot"

    def __init__(self, config: UsbInfiniteConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._backend = None
        self._plate = None

    # ---- 接続
    async def _ensure(self):
        if self._backend is None:
            import pylabrobot.resources as resources
            from pylabrobot.legacy.plate_reading.tecan.infinite_backend import ExperimentalTecanInfinite200ProBackend

            factory = getattr(resources, self.config.plate_model, None)
            if factory is None:
                raise BackendError(f"PyLabRobot にプレートの定義 {self.config.plate_model!r} がありません")
            self._plate = factory(name="plate")
            backend = ExperimentalTecanInfinite200ProBackend()
            try:
                await backend.setup()
            except Exception as exc:  # noqa: BLE001
                raise BackendError(f"Infinite 200 PRO に USB でつながりません: {type(exc).__name__}: {exc}") from None
            self._backend = backend
        return self._backend

    async def stop(self) -> None:
        if self._backend is not None:
            try:
                await self._backend.stop()
            finally:
                self._backend = None

    async def _do(self, label: str, report, fn):
        if self._lock.locked():
            raise BackendError("別の操作が実行中です")
        async with self._lock:
            report(label)
            if self.config.dry_run:
                logger.info("dry-run: %s をリーダに送るところでした", label)
                return None
            backend = await self._ensure()
            try:
                return await fn(backend)
            except BackendError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise BackendError(f"{label} に失敗しました: {type(exc).__name__}: {exc}") from None

    # ---- 共通の入口（reader.py と同じ名前）
    async def open_tray(self, report) -> None:
        await self._do("open", report, lambda b: b.open())

    async def close_tray(self, report) -> None:
        await self._do("close", report, lambda b: b.close(self._plate))

    async def list_methods(self) -> list[str]:
        return []

    async def measure_loaded(self, method: str, report):
        raise BackendError("USB 直結の Infinite では Magellan の測定ファイルは使えません。ReadAbsorbance を使ってください")

    async def read_absorbance(self, wavelength: int, report) -> tuple[str, AscResult]:
        if not 230 <= wavelength <= 1000:
            raise BackendError("吸光度の波長は 230〜1000 nm です")
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        results = await self._do(
            f"read_absorbance {wavelength} nm", report,
            lambda b: b.read_absorbance(self._plate, [], wavelength, flashes=self.config.flashes),
        )
        return "", matrix_to_result(results, wavelength, started, self.config)


def matrix_to_result(results, wavelength: int, started: str, config: UsbInfiniteConfig) -> AscResult:
    """PyLabRobot の返り値（[{"wavelength", "time", "temperature", "data": 8x12}]）を、ASC と同じ形に直す。"""
    grid: dict[str, float] = {f"{r}{c}": math.nan for r in ROWS for c in range(1, 13)}
    if results:
        data = results[0].get("data") or []
        for i, row in enumerate(data[: len(ROWS)]):
            for j, value in enumerate(row[:12]):
                grid[f"{ROWS[i]}{j + 1}"] = math.nan if value is None else float(value)
    metadata = [
        f"Date of measurement: {started}",
        f"PyLabRobot ExperimentalTecanInfinite200ProBackend, absorbance {wavelength} nm, {config.flashes} flashes",
        f"Plate: {config.plate_model}",
    ]
    if not results:
        metadata.append("dry-run: no data")
    return AscResult(labels=[f"{wavelength}nm"], values=[grid], metadata=metadata)
