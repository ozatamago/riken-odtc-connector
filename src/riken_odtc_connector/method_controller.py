import dataclasses
import datetime

from unitelabs.cdk import sila

from .door_controller import OdtcOperationFailed
from .fluent_backend import BackendError, FluentOdtcBackend
from .method_store import SNAPSHOT_TAKEN, load_methods, premethods_for
from .progress import run_with_progress

PRE_METHOD_S = 470.0
"""PreMethod の所要時間。目標温度によらず一定（PT7M45S）。2026-10-09 の ini95 は 466.2 秒、10-15 の ini25 は 466.3 秒。"""
METHOD_MARGIN = 1.07
"""Method の見積り（平坦 + Slope どおりの昇降温）に掛ける余裕。PCR 30 サイクルは見積り 84.4 分、実測 90.5 分。"""


@dataclasses.dataclass
class OdtcMethodInfo(sila.CustomDataType):
    """
    A temperature program stored on the ODTC.

    Args:
      Name: Method name as stored on the device (pass this to the run commands).
      Kind: "PreMethod" (pre-heating to a target block temperature) or "Method" (the temperature program itself).
      BlockTemperature: PreMethod - target block temperature. Method - start block temperature; run the PreMethod
        with the same value first. Degrees Celsius.
      LidTemperature: Target / start lid temperature in degrees Celsius.
      Steps: Number of steps in the program (0 for a PreMethod).
      EstimatedSeconds: Sum of the plateau times including loops, in seconds (ramps not included). 0 for a PreMethod.
      EstimatedTotalSeconds: Plateaus plus ramps at each step's slope, in seconds. For a PreMethod, the fixed
        7 min 45 s. The real run takes a few percent longer (30-cycle PCR: 84.4 min estimated, 90.5 min measured).
      MatchingPreMethods: For a Method, the PreMethods whose target equals its start block temperature.
    """

    name: str
    kind: str
    block_temperature: float
    lid_temperature: float
    steps: int
    estimated_seconds: float
    estimated_total_seconds: float
    matching_pre_methods: list[str]


class UnknownMethod(Exception):
    """The given name is not a PreMethod (or Method) stored on the ODTC. See the Methods property for valid names."""


class PreMethodRequired(Exception):
    """
    The Method needs its matching PreMethod right before it (the ODTC rejects a Method otherwise, and every Method
    needs its own PreMethod run). Run one of the Method's MatchingPreMethods first.
    """


class MethodController(sila.Feature):
    """
    Temperature programs (PreMethods and Methods) stored on the Inheco ODTC 96, and running them.

    The list is read from a copy of the device method store taken on 2026-08-10 16:37. Methods added on the device
    after that date are not shown yet.

    A Method must be preceded by the PreMethod whose target equals the Method's start block temperature.
    The connector remembers the last PreMethod it ran (LastPreMethod). This memory is lost when the connector
    restarts, and it does not know about runs started from the FluentControl GUI.
    """

    def __init__(self, backend: FluentOdtcBackend):
        super().__init__(originator="jp.riken.bdr", category="odtc", version="1.0")
        self.backend = backend
        self._methods = load_methods()
        self._last_pre_method = ""
        self._last_pre_method_at = ""

    def _find(self, name: str, kind: str):
        for m in self._methods:
            if m.name == name and m.kind == kind:
                return m
        valid = ", ".join(m.name for m in self._methods if m.kind == kind)
        raise UnknownMethod(f"{name!r} is not a {kind} on the ODTC. Valid names: {valid}")

    @sila.UnobservableProperty()
    async def get_methods(self) -> list[OdtcMethodInfo]:
        """All PreMethods and Methods stored on the ODTC."""
        return [
            OdtcMethodInfo(
                name=m.name,
                kind=m.kind,
                block_temperature=m.block_temperature,
                lid_temperature=m.lid_temperature,
                steps=m.steps,
                estimated_seconds=m.estimated_seconds,
                estimated_total_seconds=(m.estimated_seconds + m.ramp_seconds) if m.kind == "Method" else 465.0,
                matching_pre_methods=premethods_for(m, self._methods) if m.kind == "Method" else [],
            )
            for m in self._methods
        ]

    @sila.UnobservableProperty()
    async def get_method_list_taken_at(self) -> str:
        """When the method list was copied from the device (ISO 8601, local time)."""
        return SNAPSHOT_TAKEN

    @sila.UnobservableProperty()
    async def get_last_pre_method(self) -> str:
        """
        Name of the last PreMethod this connector completed, followed by when it finished
        (e.g. "ini95 @ 2026-10-15T11:40:00"). Empty if none since the connector started.
        """
        return f"{self._last_pre_method} @ {self._last_pre_method_at}" if self._last_pre_method else ""

    @sila.ObservableCommand(errors=[UnknownMethod, OdtcOperationFailed])
    async def run_pre_method(self, pre_method: str, *, status: sila.Status) -> float:
        """
        Run a PreMethod: heat the block and the lid to the PreMethod's targets (about 8 minutes, independent of the
        target temperature). Close the door first. The lid heats to 110 degrees Celsius.

        Args:
          PreMethod: Name of the PreMethod, e.g. "ini95". See the Methods property.

        Returns:
          PreMethodDuration: Seconds the PreMethod itself took (0 in dry-run mode).
        """
        self._find(pre_method, "PreMethod")
        self._last_pre_method = ""  # 途中で失敗したら「済み」とはみなさない
        try:
            elapsed = await run_with_progress(
                self.backend, "SiLA-ODTC_ExecuteMethod", pre_method, status, PRE_METHOD_S
            )
        except BackendError as exc:
            raise OdtcOperationFailed(str(exc)) from None
        self._last_pre_method = pre_method
        self._last_pre_method_at = datetime.datetime.now().isoformat(timespec="seconds")
        return elapsed

    @sila.ObservableCommand(errors=[UnknownMethod, PreMethodRequired, OdtcOperationFailed])
    async def run_method(self, method: str, *, status: sila.Status) -> float:
        """
        Run a Method (temperature program). Its matching PreMethod must have been run by this connector right
        before, and each Method needs a fresh PreMethod. Put the plate in (with the ODTC lid or a seal) and close
        the door first.

        Args:
          Method: Name of the Method, e.g. "95-30s_50-60s_72-30s_30cycle". See the Methods property.

        Returns:
          MethodDuration: Seconds the Method itself took (0 in dry-run mode).
        """
        m = self._find(method, "Method")
        matching = premethods_for(m, self._methods)
        if self._last_pre_method not in matching:
            done = self._last_pre_method or "none"
            raise PreMethodRequired(
                f"{method} starts at {m.block_temperature:g} degC and needs one of {', '.join(matching)} first "
                f"(last PreMethod run by this connector: {done})"
            )
        self._last_pre_method = ""  # PreMethod は Method ごとに 1 回。成功しても失敗しても使い切りとする
        expected = (m.estimated_seconds + m.ramp_seconds) * METHOD_MARGIN + 10
        try:
            return await run_with_progress(self.backend, "SiLA-ODTC_ExecuteMethod", method, status, expected)
        except BackendError as exc:
            raise OdtcOperationFailed(str(exc)) from None
