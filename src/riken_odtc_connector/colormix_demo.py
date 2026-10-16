"""Hackathon demo: pipette coloured water into a plate, measure three absorbances and put the plate back, in one
SiLA command (ColorMixDemo.MixColorsAndMeasure).

It repeats the ColorMix runs of August 2026 (run_colormix_source_plate.py) in a short form:

- 4 Well SBS Trough[001] holds water (lane A1), red (A2, FRAME RED), yellow (A3, SUNNY YELLOW) and blue (A4, AQUA
  BLUE). For a test, all four lanes may hold water.
- 96 Well Flat_Custom[001] (on FluentControl's worktable at Nest7mm_Pos[6]) receives water, then each colour, per
  column with up to 8 channels at once (channel = row, so the tips line up with the wells), new 200 ul tips for
  every liquid. Nothing is mixed (as in August).
- The plate goes to the Infinite 200 PRO through the regrip position, is measured with OFML_ColorMix3.mth (430,
  520 and 630 nm) and goes back to its place (FluentPlateHandling.MeasureWorktablePlate's route).

Everything runs in one FluentControl run (opened here, with deck images at the start and end, unless a run is
already open). The definitions every step uses are checked and recorded before anything moves, as for every
command of this connector.
"""

import dataclasses

from unitelabs.cdk import sila

from .fluent_backend import BackendError, FluentOdtcBackend
from .infinite.features import MeasurementResult, _result, _with_progress
from .infinite.reader import FluentInfiniteReader
from .liquid_handling import LiquidHandling, LiquidHandlingFailed, drop_tips_xml, get_tips_xml, pipetting_xml, well_index

TROUGH = "4 Well SBS Trough[001]"
LANES = {"water": "A1", "red": "A2", "yellow": "A3", "blue": "A4"}
PLATE = "96 Well Flat_Custom[001]"
METHOD = "OFML_ColorMix3.mth"
WAVELENGTHS = (430, 520, 630)
TIP_TYPE = "FCA, 200ul SBS"
LIQUID_CLASS = "Water Free Single"
MAX_WELL_VOLUME = 300.0
"""ul per well; the wells of 96 Well Flat_Custom hold about 340 ul (6.96 mm wide, 10.9 mm deep)."""
ROWS = "ABCDEFGH"


class DemoFailed(Exception):
    """The demo was refused or failed. The description says why."""


@dataclasses.dataclass
class PlannedWell:
    well: str
    volumes: dict  # liquid -> ul


def split_wells(wells: list[str]) -> list[str]:
    """Well names; an entry may also hold several, as typed into one field of the UniteLabs App
    ('["A1", "B1"]' or "A1, B1" arrived as a single well on 2026-10-16)."""
    import re

    return [w.upper() for entry in wells for w in re.split(r"[\s,;]+", re.sub(r"[\[\]\"']", " ", entry)) if w]


def per_well(values: list[float], n: int, name: str) -> list[float]:
    """One value per well; a single value is used for every well, none means 0."""
    if not values:
        return [0.0] * n
    if len(values) == 1:
        return [float(values[0])] * n
    if len(values) != n:
        raise DemoFailed(f"Give one {name} volume per well ({n}), or a single volume for all wells")
    return [float(v) for v in values]


def plan_wells(wells: list[str], red: list[float], yellow: list[float], blue: list[float], water: float) -> list[PlannedWell]:
    names = split_wells(wells)
    if not names:
        raise DemoFailed("Give at least one well")
    red, yellow, blue = (per_well(v, len(names), n) for v, n in ((red, "red"), (yellow, "yellow"), (blue, "blue")))
    if len(set(names)) != len(names):
        raise DemoFailed("Each well may appear only once")
    out = []
    for w, r, y, b in zip(names, red, yellow, blue):
        well_index(w, 8, 12)  # refuses wells outside a 96-well plate
        vols = {"water": float(water), "red": float(r), "yellow": float(y), "blue": float(b)}
        if any(v < 0 for v in vols.values()):
            raise DemoFailed(f"{w}: volumes must not be negative")
        if sum(vols.values()) > MAX_WELL_VOLUME:
            raise DemoFailed(f"{w}: {sum(vols.values()):g} ul is more than {MAX_WELL_VOLUME:g} ul per well")
        if sum(vols.values()) <= 0:
            raise DemoFailed(f"{w}: nothing to pipette")
        out.append(PlannedWell(w, vols))
    return out


def pipetting_steps(planned: list[PlannedWell]) -> list[tuple[str, str]]:
    """Per liquid (water first) and per plate column: take one tip per well on the channel of the well's row,
    aspirate each well's volume from the trough lane, dispense into the wells, drop the tips."""
    steps: list[tuple[str, str]] = []
    for liquid, lane in LANES.items():
        by_column: dict[int, list[PlannedWell]] = {}
        for p in planned:
            if p.volumes[liquid] > 0:
                by_column.setdefault(int(p.well[1:]), []).append(p)
        for column in sorted(by_column):
            group = sorted(by_column[column], key=lambda p: ROWS.index(p.well[0]))
            channels = [ROWS.index(p.well[0]) + 1 for p in group]
            vols = [p.volumes[liquid] for p in group]
            wells = [p.well for p in group]
            what = f"{liquid} column {column} ({','.join(wells)})"
            steps += [
                (f"GetTips {TIP_TYPE} ch{channels} for {what}", get_tips_xml(TIP_TYPE, channels)),
                (f"Aspirate {'/'.join(f'{v:g}' for v in vols)} ul {liquid} from {TROUGH} {lane}",
                 pipetting_xml("Aspirate", TROUGH, [lane] * len(channels), vols, LIQUID_CLASS, channels, 1, 4)),
                (f"Dispense {liquid} into {','.join(wells)}",
                 pipetting_xml("Dispense", PLATE, wells, vols, LIQUID_CLASS, channels, 8, 12)),
                (f"DropTips ch{channels}", drop_tips_xml(channels)),
            ]
    return steps


def summarize(planned: list[PlannedWell], asc, labels: list[str]) -> list[str]:
    """One line per pipetted well: what went in and the absorbance at each wavelength."""
    lines = []
    for p in planned:
        values = []
        for i, grid in enumerate(asc.values if asc else []):
            nm = WAVELENGTHS[i] if i < len(WAVELENGTHS) else labels[i]
            v = grid.get(p.well, float("nan"))
            values.append(f"{nm} nm {v:.4f}")
        put = ", ".join(f"{k} {v:g}" for k, v in p.volumes.items() if v)
        lines.append(f"{p.well}: " + (", ".join(values) if values else "not measured (dry run)") + f" ({put} ul)")
    return lines


class ColorMixDemo(sila.Feature):
    """
    Hackathon demo of the Fluent 780: pipette water and red, yellow and blue dye into wells of a 96-well plate, read
    the plate at 430, 520 and 630 nm in the Infinite 200 PRO and put the plate back - all in one command.

    Setup on the deck: 4 Well SBS Trough[001] with water (lane 1), red (2), yellow (3) and blue (4) - water in all
    four for a test - and an empty 96 Well Flat_Custom[001] at Nest7mm_Pos[6]. The arm and the plate gripper MOVE.
    """

    def __init__(self, backend: FluentOdtcBackend, reader: FluentInfiniteReader, liquid: LiquidHandling):
        super().__init__(originator="jp.riken.bdr", category="fluent", version="1.0")
        self.backend = backend
        self.reader = reader
        self.liquid = liquid

    @sila.ObservableCommand(errors=[DemoFailed])
    async def mix_colors_and_measure(self, wells: list[str], red: list[float], yellow: list[float],
                                     blue: list[float], water: float, *,
                                     status: sila.Status) -> tuple[list[str], MeasurementResult, list[str]]:
        """
        Pipette, measure and put the plate back. Each well gets the water volume, then its red, yellow and blue
        volumes, with new 200 ul tips per liquid; up to 8 wells of one column are done at once. Nothing is mixed.

        Args:
          Wells: Wells to fill, e.g. ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"] (one column takes a few
            minutes). One entry may also hold several, e.g. "A1, B1, C1".
          Red: Red dye in ul, one per well, or a single value for every well (empty or 0 for none).
          Yellow: Yellow dye in ul, as Red.
          Blue: Blue dye in ul, as Red.
          Water: Water per well in ul, e.g. 150 (0 for none). At most 300 ul in a well altogether.

        Returns:
          Wells: One line per well: the absorbance at 430, 520 and 630 nm and what went in, e.g.
            "A1: 430 nm 0.0412, 520 nm 0.0398, 630 nm 0.0391 (water 150 ul)".
          Result: All values of the plate (3 wavelengths x 96 wells) and the result file on the Fluent PC.
          Steps: The commands that were sent, in order.
        """
        try:
            planned = plan_wells(wells, red, yellow, blue, water)
            if self.backend.tips:
                raise DemoFailed(f"Channels {sorted(self.backend.tips)} have tips; drop them first")
            # the same checks as the pipetting commands: definitions, tips, liquid class settings, volumes
            self.liquid._refresh()
            biggest = max(v for p in planned for v in p.volumes.values())
            extra = self.liquid._tip_and_class(TIP_TYPE, LIQUID_CLASS, [biggest],
                                               [self.liquid._labware_type(PLATE), self.liquid._labware_type(TROUGH)])
            await self.reader.check_method(METHOD)
            name = self.reader._measure_name()
            steps = pipetting_steps(planned) + self.reader.worktable_plate_steps(PLATE, METHOD, name)
        except LiquidHandlingFailed as exc:
            raise DemoFailed(str(exc)) from None
        except BackendError as exc:
            raise DemoFailed(str(exc)) from None
        extra["demo"] = {"wells": [dataclasses.asdict(p) for p in planned], "method": METHOD, "plate": PLATE,
                         "trough": TROUGH, "lanes": LANES}
        opened_here = not self.backend.run_open
        try:
            if opened_here:
                await self.backend.start_run()
            path, asc = await _with_progress(status, len(steps),
                                             lambda report: self._run(steps, name, report, extra))
        except BackendError as exc:
            raise DemoFailed(str(exc)) from None
        finally:
            if opened_here and self.backend.run_open:
                try:
                    await self.backend.end_run()
                except BackendError:
                    pass  # reported by RunController.RunOpen; the measurement itself is kept
        result = _result(METHOD, path, asc)
        return summarize(planned, asc, result.labels), result, [label for label, _ in steps]

    async def _run(self, steps, name, report, extra):
        # like FluentInfiniteReader.run, but with the demo's record entries
        import asyncio
        import time

        started = time.time()
        await self.backend.run_steps(steps, report, f"ColorMix demo ({len(steps)} steps)", extra)
        if self.backend.config.dry_run:
            return "", None
        return await asyncio.to_thread(self.reader._fetch_asc, name, started)
