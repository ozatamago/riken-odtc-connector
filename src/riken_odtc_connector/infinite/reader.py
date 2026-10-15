"""Backend that drives the Infinite 200 PRO in the Fluent 780 through FluentControl (Magellan macros and the RGA).

Measurement results are not returned through the FluentControl API, so the ASC file written by Magellan is read
over SSH. Loading, measuring and unloading happen in a single FluentControl run (a new run forgets labware
positions; confirmed 2026-08-13).
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime
import logging
import time

from unitelabs.cdk.config import Config

from ..fluent_backend import BackendError, FluentOdtcBackend
from .asc import AscResult, parse_asc
from .xml import add_labware_xml, magellan_xml, remove_labware_xml, rga_xml

logger = logging.getLogger(__name__)

TEMP_LABEL = "SiLA_ReaderPlate[001]"
"""Labware name that MeasurePlate temporarily registers in FluentControl. Must not clash with existing names on
the worktable."""


@dataclasses.dataclass
class InfiniteConfig(Config):
    """Configuration for the Infinite 200 PRO inside the Fluent 780. Values as verified on the device on 2026-08-13."""

    reader_location: str = "InfiniteM200_New"
    reader_site: int = 1
    regrip_location: str = "Regrip_Pos"
    """Regrip position for flat-bottom plates (7mm Regrip Nest). PCR plates cannot go into the reader with the
    current configuration."""
    regrip_site: int = 1
    output_dir: str = r"C:\Users\Public\Documents\Tecan\Magellan Pro\asc"
    """Magellan_Measure output directory. Results are written here as "riken_<timestamp>" and the resulting .asc
    is searched for below it."""
    method_dir: str = r"C:\Users\Public\Documents\Tecan\Magellan Pro\mth"
    wavelength_methods: dict = dataclasses.field(default_factory=dict)
    """Method files used by ReadAbsorbance(wavelength) (e.g. {"600": "RIKEN_Abs600_noTemp.mth"}).
    List only single-wavelength, single-label method files. If empty, ReadAbsorbance is unavailable."""


class FluentInfiniteReader:
    def __init__(self, backend: FluentOdtcBackend, config: InfiniteConfig):
        self.backend = backend
        self.config = config

    # ---- Method files (Magellan methods)
    async def list_methods(self) -> list[str]:
        """Return the .mth files under method_dir as paths relative to it (e.g. "Abs600.mth", "Azumi\\x.mth")."""
        d = self.config.method_dir
        out = await asyncio.to_thread(
            self.backend._ssh_ps,
            f"-Command \"Get-ChildItem -LiteralPath '{d}' -Recurse -Filter *.mth | "
            f"ForEach-Object {{ $_.FullName.Substring({len(d) + 1}) }}\"",
        )
        return sorted(line.strip() for line in out.splitlines() if line.strip())

    async def check_method(self, method: str) -> None:
        if method not in await self.list_methods():
            raise BackendError(f"Method file {method!r} not found in {self.config.method_dir}")

    # ---- Step assembly
    def _measure_name(self) -> str:
        return "riken_" + datetime.datetime.now().strftime("%y%m%d_%H%M%S")

    def measure_steps(self, method: str, name: str) -> list[tuple[str, str]]:
        settings = f"{self.config.output_dir}\\{name},{method},0,0,0"
        return [(f"Magellan_Measure {method}", magellan_xml("Magellan_Measure", settings))]

    def load_measure_unload_steps(self, labware_type: str, location: str, site: int, method: str,
                                  name: str) -> list[tuple[str, str]]:
        c, lbl = self.config, TEMP_LABEL
        return [
            (f"AddLabware {labware_type} at {location}[{site}]", add_labware_xml(labware_type, lbl, location, site)),
            ("Magellan_Open", magellan_xml("Magellan_Open")),
            (f"RGA -> {c.regrip_location}[{c.regrip_site}]",
             rga_xml(lbl, c.regrip_location, str(c.regrip_site), fixed_site=True)),
            (f"RGA -> {c.reader_location}[{c.reader_site}]",
             rga_xml(lbl, c.reader_location, str(c.reader_site), fixed_site=True)),
            ("Magellan_Close", magellan_xml("Magellan_Close")),
            *self.measure_steps(method, name),
            ("Magellan_Open", magellan_xml("Magellan_Open")),
            (f"RGA -> {c.regrip_location}[{c.regrip_site}]",
             rga_xml(lbl, c.regrip_location, str(c.regrip_site), fixed_site=True)),
            (f"RGA -> {location}[{site}]", rga_xml(lbl, location, str(site), fixed_site=True)),
            ("Magellan_Close", magellan_xml("Magellan_Close")),
            (f"RemoveLabware {lbl}", remove_labware_xml(lbl)),
        ]

    def worktable_plate_steps(self, labware: str, method: str, name: str) -> list[tuple[str, str]]:
        """Move a plate registered on the worktable (by name) into the reader, measure it, and return it to its base.

        Same route as run with the Tecan server's TransferLabware / TransferLabwareBackToBase on 2026-08-13.
        Here the same motions are sent as RGA macros (XML). The move-to-base macro was verified on the device
        with ODTC_Lid on 10-09.
        """
        c = self.config
        return [
            ("Magellan_Open", magellan_xml("Magellan_Open")),
            (f"RGA {labware} -> {c.regrip_location}[{c.regrip_site}]",
             rga_xml(labware, c.regrip_location, str(c.regrip_site), fixed_site=True)),
            (f"RGA {labware} -> {c.reader_location}[{c.reader_site}]",
             rga_xml(labware, c.reader_location, str(c.reader_site), fixed_site=True)),
            ("Magellan_Close", magellan_xml("Magellan_Close")),
            *self.measure_steps(method, name),
            ("Magellan_Open", magellan_xml("Magellan_Open")),
            (f"RGA {labware} -> {c.regrip_location}[{c.regrip_site}]",
             rga_xml(labware, c.regrip_location, str(c.regrip_site), fixed_site=True)),
            (f"RGA {labware} -> base", rga_xml(labware, move_to_base=True)),
            ("Magellan_Close", magellan_xml("Magellan_Close")),
        ]

    # ---- Result retrieval
    def _fetch_asc(self, name: str, since: float) -> tuple[str, AscResult]:
        d = self.config.output_dir
        script = (
            f"$f = Get-ChildItem -LiteralPath '{d}' -Recurse -Filter '{name}*.asc' | "
            "Sort-Object LastWriteTime -Descending | Select-Object -First 1; "
            "if ($f) { $f.FullName; [Convert]::ToBase64String([IO.File]::ReadAllBytes($f.FullName)) } else { 'NOTFOUND' }"
        )
        deadline = time.monotonic() + 60
        while True:
            out = self.backend._ssh_ps(f'-Command "{script}"').strip().splitlines()
            if out and out[0] != "NOTFOUND" and len(out) >= 2:
                text = base64.b64decode(out[1]).decode("latin-1")
                return out[0], parse_asc(text)
            if time.monotonic() > deadline:
                raise BackendError(f"Result file {name}*.asc not found under {d}")
            time.sleep(3)

    # ---- Entry points called from the features
    async def run(self, steps: list[tuple[str, str]], name: str | None, report) -> tuple[str, AscResult | None]:
        started = time.time()
        labels = [label for label, _ in steps]
        what = "Infinite: " + (labels[0] if len(labels) == 1 else next((x for x in labels if "Measure" in x), labels[0]) + f" and others ({len(labels)} steps)")
        await self.backend.run_steps(steps, report, what)
        if name is None or self.backend.config.dry_run:
            return "", None
        return await asyncio.to_thread(self._fetch_asc, name, started)

    # ---- Common entry points (features.py does not know which backend it uses; plr_reader.py has the same names)
    kind = "fluent"

    async def open_tray(self, report) -> None:
        await self.run([("Magellan_Open", magellan_xml("Magellan_Open"))], None, report)

    async def close_tray(self, report) -> None:
        await self.run([("Magellan_Close", magellan_xml("Magellan_Close"))], None, report)

    async def measure_loaded(self, method: str, report) -> tuple[str, AscResult | None]:
        await self.check_method(method)
        name = self._measure_name()
        return await self.run(self.measure_steps(method, name), name, report)

    async def read_absorbance(self, wavelength: int, report) -> tuple[str, AscResult | None]:
        method = self.config.wavelength_methods.get(str(wavelength))
        if not method:
            raise BackendError(
                f"No method file is configured for {wavelength} nm on this Infinite (through Fluent)"
                " (setting infinite.wavelength_methods). Use MeasureLoadedPlate with a method file instead"
            )
        path, asc = await self.measure_loaded(method, report)
        if asc is not None:
            asc.labels = [f"{wavelength}nm"] + asc.labels[1:]
        return path, asc
