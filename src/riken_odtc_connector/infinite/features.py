import asyncio
import dataclasses
import datetime
import threading

from unitelabs.cdk import sila

from ..fluent_backend import BackendError
from .reader import FluentInfiniteReader  # noqa: F401 - for the type hint; the PyLabRobot backend has the same methods


class ReaderOperationFailed(Exception):
    """The plate reader operation could not be performed. The description explains why."""


@dataclasses.dataclass
class WellValue(sila.CustomDataType):
    """
    One measured value.

    Args:
      Label: Measurement label as defined in the Magellan method (e.g. "Label1"; one per wavelength).
      Well: Well name, "A1" to "H12".
      Value: Measured value (absorbance in OD for absorbance methods). NaN when the reader reported no number.
    """

    label: str
    well: str
    value: float


@dataclasses.dataclass
class MeasurementResult(sila.CustomDataType):
    """
    Result of one measurement.

    Args:
      Method: Magellan method file used for the measurement.
      Labels: Measurement labels in the order they appear in the result file.
      Values: All measured values (labels x 96 wells).
      ResultFile: Path of the ASC result file on the Fluent PC (empty in dry-run mode).
      Metadata: Lines after the values in the result file (date, instrument, plate, wavelengths, ...).
    """

    method: str
    labels: list[str]
    values: list[WellValue]
    result_file: str
    metadata: list[str]


async def _with_progress(status: sila.Status, total: int, coro_factory):
    """Count the report() calls made before each step and send progress (from the event loop)."""
    loop = asyncio.get_running_loop()
    done = [0]
    finished = [False]

    def send(progress: float) -> None:
        # Ignore updates arriving after completion, so the status does not go back to "running"
        # (2026-10-15: completion was lost in a USB dry run)
        if not finished[0]:
            status.update(progress=progress)

    def report(step: str) -> None:
        if step != "dry-run" and not step.startswith(("PrepareMethod", "waiting", "Run", "Edit")):
            progress = min(done[0] / max(total, 1), 0.95)
            done[0] += 1
            if threading.get_ident() == loop_thread:
                send(progress)  # called on the event loop (USB backend)
            else:
                loop.call_soon_threadsafe(send, progress)  # called from a backend thread (Fluent backend)

    loop_thread = threading.get_ident()
    status.update(progress=0.0)
    try:
        result = await coro_factory(report)
    finally:
        finished[0] = True
    status.update(progress=1.0, remaining_time=datetime.timedelta(0))
    return result


def _result(method: str, path: str, asc) -> MeasurementResult:
    if asc is None:
        return MeasurementResult(method=method, labels=[], values=[], result_file="", metadata=[])
    values = [WellValue(label=lbl, well=w, value=v) for lbl, grid in zip(asc.labels, asc.values) for w, v in grid.items()]
    return MeasurementResult(method=method, labels=asc.labels, values=values, result_file=path, metadata=asc.metadata)


class TrayController(sila.Feature):
    """
    Open and close the plate tray of the Tecan Infinite 200 PRO. Make sure nothing is in the tray's way.

    The reader is driven through Tecan FluentControl (Magellan driver). Each command is one short FluentControl run.
    """

    def __init__(self, reader: FluentInfiniteReader):
        super().__init__(originator="jp.riken.bdr", category="platereader", version="1.0")
        self.reader = reader

    async def _tray(self, fn, status: sila.Status) -> None:
        try:
            await _with_progress(status, 1, fn)
        except BackendError as exc:
            raise ReaderOperationFailed(str(exc)) from None

    @sila.ObservableCommand(errors=[ReaderOperationFailed])
    async def open_tray(self, *, status: sila.Status) -> None:
        """Move the tray out of the reader."""
        await self._tray(self.reader.open_tray, status)

    @sila.ObservableCommand(errors=[ReaderOperationFailed])
    async def close_tray(self, *, status: sila.Status) -> None:
        """Move the tray into the reader."""
        await self._tray(self.reader.close_tray, status)


class AbsorbanceReader(sila.Feature):
    """
    Measure the plate that is already in the Tecan Infinite 200 PRO.

    The same feature is served by two kinds of servers:
    - Fluent 780 reader (through FluentControl and Magellan): MeasureLoadedPlate runs a Magellan method file;
      ReadAbsorbance works for the wavelengths configured with a method file.
    - Stand-alone reader (USB, PyLabRobot): ReadAbsorbance takes any wavelength; MeasureLoadedPlate is not
      available (no Magellan), and Methods is empty.
    """

    def __init__(self, reader: FluentInfiniteReader):
        super().__init__(originator="jp.riken.bdr", category="platereader", version="1.0")
        self.reader = reader

    @sila.UnobservableProperty(errors=[ReaderOperationFailed])
    async def get_methods(self) -> list[str]:
        """Magellan method files (.mth) available on the Fluent PC, e.g. "Abs600.mth"."""
        try:
            return await self.reader.list_methods()
        except BackendError as exc:
            raise ReaderOperationFailed(str(exc)) from None

    @sila.ObservableCommand(errors=[ReaderOperationFailed])
    async def measure_loaded_plate(self, method: str, *, status: sila.Status) -> MeasurementResult:
        """
        Measure the plate that is in the reader now. The tray must be closed with the plate inside.

        Args:
          Method: Magellan method file, e.g. "OFML_Abs405.mth". See the Methods property. Note that "Abs600.mth"
            has 37 degC temperature control switched on.

        Returns:
          Result: The measured values and the result file.
        """
        try:
            path, asc = await _with_progress(status, 1, lambda report: self.reader.measure_loaded(method, report))
        except BackendError as exc:
            raise ReaderOperationFailed(str(exc)) from None
        return _result(method, path, asc)

    @sila.ObservableCommand(errors=[ReaderOperationFailed])
    async def read_absorbance(self, wavelength: int, *, status: sila.Status) -> MeasurementResult:
        """
        Read the absorbance of the plate that is in the reader now, at one wavelength, for all 96 wells.
        The tray must be closed with the plate inside.

        Args:
          Wavelength: Wavelength in nm (230 to 1000).

        Returns:
          Result: The measured OD values, one label named like "600nm".
        """
        try:
            path, asc = await _with_progress(status, 1, lambda report: self.reader.read_absorbance(wavelength, report))
        except BackendError as exc:
            raise ReaderOperationFailed(str(exc)) from None
        return _result(f"{wavelength}nm", path, asc)


class FluentPlateHandling(sila.Feature):
    """
    Measure a plate from the Fluent worktable: the RGA brings it into the Infinite 200 PRO, it is measured, and the
    RGA puts it back - all within one FluentControl run (FluentControl forgets labware positions between runs).

    Flat-bottom plates only: PCR plates cannot be regripped for the reader with the current Fluent configuration.
    The plate must not have a lid. The source site must be free in FluentControl's worktable memory.
    """

    def __init__(self, reader: FluentInfiniteReader, tracker=None):
        super().__init__(originator="jp.riken.bdr", category="platereader", version="1.0")
        self.reader = reader
        self.tracker = tracker
        """The LabwareTracker shared with RgaController (if any), so a plate that has been moved away is not fetched
        from its original position."""

    @sila.ObservableCommand(errors=[ReaderOperationFailed])
    async def measure_plate(
        self, labware_type: str, location: str, site: int, method: str, *, status: sila.Status
    ) -> MeasurementResult:
        """
        Bring a plate from the worktable into the reader, measure it, and put it back.

        Args:
          LabwareType: FluentControl labware type of the plate, e.g. "96 Well Flat_Custom".
          Location: Worktable location (carrier location name) where the plate is, e.g. "Nest7mm_Pos".
          Site: Site number at that location, e.g. 7.
          Method: Magellan method file, e.g. "OFML_Abs405.mth". See AbsorbanceReader.Methods.

        Returns:
          Result: The measured values and the result file.
        """
        try:
            await self.reader.check_method(method)
            name = self.reader._measure_name()
            steps = self.reader.load_measure_unload_steps(labware_type, location, site, method, name)
            path, asc = await _with_progress(status, len(steps), lambda report: self.reader.run(steps, name, report))
        except BackendError as exc:
            raise ReaderOperationFailed(str(exc)) from None
        return _result(method, path, asc)

    @sila.ObservableCommand(errors=[ReaderOperationFailed])
    async def measure_worktable_plate(self, labware: str, method: str, *, status: sila.Status) -> MeasurementResult:
        """
        Bring a plate that is registered on the FluentControl worktable (by its name) into the reader, measure it,
        and put it back to its base position - all within one FluentControl run.

        Args:
          Labware: Labware name on the worktable, e.g. "96 Well Flat_Custom[001]".
          Method: Magellan method file, e.g. "OFML_Abs405.mth". See AbsorbanceReader.Methods.

        Returns:
          Result: The measured values and the result file.
        """
        try:
            if self.tracker is not None and labware in self.tracker.positions:
                loc, site = self.tracker.where(labware)
                raise BackendError(
                    f"{labware} is currently at {loc}[{site}] (moved by RgaController). Use ReturnLabware first"
                )
            await self.reader.check_method(method)
            name = self.reader._measure_name()
            steps = self.reader.worktable_plate_steps(labware, method, name)
            path, asc = await _with_progress(status, len(steps), lambda report: self.reader.run(steps, name, report))
        except BackendError as exc:
            raise ReaderOperationFailed(str(exc)) from None
        return _result(method, path, asc)
