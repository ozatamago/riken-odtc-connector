from unitelabs.cdk import sila

from .door_controller import OdtcOperationFailed
from .fluent_backend import BackendError, FluentOdtcBackend


class StatusController(sila.Feature):
    """
    Whether the connector can take a command now. Reading these properties never moves or heats anything.

    The devices are driven through Tecan FluentControl, so a command can only start while FluentControl is idle
    (EditMode or RunModeRunFinished), no FluentControl dialog is waiting, and this connector is not already
    running another operation (on any device).
    """

    def __init__(self, backend: FluentOdtcBackend):
        super().__init__(originator="jp.riken.bdr", category="odtc", version="1.0")
        self.backend = backend

    @sila.UnobservableProperty()
    async def get_busy(self) -> bool:
        """True while this connector is sending something to FluentControl (any device: ODTC, Infinite, ...)."""
        return self.backend.busy

    @sila.UnobservableProperty()
    async def get_current_operation(self) -> str:
        """What this connector is doing now, e.g. "ODTC: SiLA-ODTC_ExecuteMethod ini95". Empty when idle."""
        return self.backend.current_operation

    @sila.UnobservableProperty(errors=[OdtcOperationFailed])
    async def get_fluent_control_state(self) -> str:
        """State of FluentControl, e.g. "EditMode", "RunModeRunning", "RunModeRunFinished"."""
        try:
            return (await self.backend.fluent_status())[0]
        except BackendError as exc:
            raise OdtcOperationFailed(str(exc)) from None

    @sila.UnobservableProperty(errors=[OdtcOperationFailed])
    async def get_active_dialogs(self) -> list[str]:
        """FluentControl dialogs waiting for an answer ("identifier: text"). Empty when none."""
        try:
            return (await self.backend.fluent_status())[1]
        except BackendError as exc:
            raise OdtcOperationFailed(str(exc)) from None

    @sila.UnobservableProperty(errors=[OdtcOperationFailed])
    async def get_ready(self) -> bool:
        """True when an ODTC command can start now (FluentControl idle, no dialog, connector not busy)."""
        if self.backend.busy:
            return False
        try:
            state, dialogs = await self.backend.fluent_status()
        except BackendError as exc:
            raise OdtcOperationFailed(str(exc)) from None
        return state in {"EditMode", "RunModeRunFinished"} and not dialogs
