"""Backend that drives a stand-alone Infinite 200 PRO directly over USB with PyLabRobot.

Not for the Infinite built into the Fluent: on the Fluent PC the reader is bound to Tecan's own USB driver
(Tecanat_Usb) and cannot be opened from PyLabRobot (pyusb / libusb) (confirmed 2026-10-15). Use it with a
stand-alone reader connected over USB to a Mac or Linux PC.

PyLabRobot's Infinite backend is experimental (ExperimentalTecanInfinite200ProBackend, M series only).
The methods used by the features have the same names as in reader.py (Fluent backend).
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
    """Configuration for a stand-alone Infinite 200 PRO connected over USB."""

    plate_model: str = "cor_96_wellplate_360uL_Fb"
    """Name of the PyLabRobot plate definition. Default: Costar 96 Flat Transparent (as shown in the Fluent's
    Magellan results)."""
    flashes: int = 25
    """Number of flashes per well (PyLabRobot default)."""
    dry_run: bool = True
    """While True, do not connect to the reader; log the commands and return NaN."""


class PyLabRobotInfiniteReader:
    kind = "pylabrobot"

    def __init__(self, config: UsbInfiniteConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._backend = None
        self._plate = None

    # ---- Connection
    async def _ensure(self):
        if self._backend is None:
            import pylabrobot.resources as resources
            from pylabrobot.legacy.plate_reading.tecan.infinite_backend import ExperimentalTecanInfinite200ProBackend

            factory = getattr(resources, self.config.plate_model, None)
            if factory is None:
                raise BackendError(f"PyLabRobot has no plate definition {self.config.plate_model!r}")
            self._plate = factory(name="plate")
            backend = ExperimentalTecanInfinite200ProBackend()
            try:
                await backend.setup()
            except Exception as exc:  # noqa: BLE001
                raise BackendError(f"Cannot open the Infinite 200 PRO over USB: {type(exc).__name__}: {exc}") from None
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
            raise BackendError("Another operation is in progress")
        async with self._lock:
            report(label)
            if self.config.dry_run:
                logger.info("dry-run: would have sent %s to the reader", label)
                return None
            backend = await self._ensure()
            try:
                return await fn(backend)
            except BackendError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise BackendError(f"{label} failed: {type(exc).__name__}: {exc}") from None

    # ---- Common entry points (same names as in reader.py)
    async def open_tray(self, report) -> None:
        await self._do("open", report, lambda b: b.open())

    async def close_tray(self, report) -> None:
        await self._do("close", report, lambda b: b.close(self._plate))

    async def list_methods(self) -> list[str]:
        return []

    async def measure_loaded(self, method: str, report):
        raise BackendError("Magellan method files are not available on a USB-connected Infinite. Use ReadAbsorbance")

    async def read_absorbance(self, wavelength: int, report) -> tuple[str, AscResult]:
        if not 230 <= wavelength <= 1000:
            raise BackendError("Absorbance wavelength must be 230 to 1000 nm")
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        results = await self._do(
            f"read_absorbance {wavelength} nm", report,
            lambda b: b.read_absorbance(self._plate, [], wavelength, flashes=self.config.flashes),
        )
        return "", matrix_to_result(results, wavelength, started, self.config)


def matrix_to_result(results, wavelength: int, started: str, config: UsbInfiniteConfig) -> AscResult:
    """Convert PyLabRobot's result ([{"wavelength", "time", "temperature", "data": 8x12}]) to the ASC form."""
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
