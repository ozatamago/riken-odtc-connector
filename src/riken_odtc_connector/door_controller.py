from unitelabs.cdk import sila

from .fluent_backend import BackendError, FluentOdtcBackend
from .progress import run_with_progress

DOOR_S = 10.0
"""扉のマクロ自体の所要時間。2026-10-15 の実測は 9.3〜10.3 秒。"""


class OdtcOperationFailed(Exception):
    """The ODTC operation could not be performed. The description explains why (FluentControl state, dialogs, ...)."""


class DoorController(sila.Feature):
    """
    Open and close the door (drawer) of the Inheco ODTC 96 thermal cycler.

    The ODTC is driven through Tecan FluentControl. Each command starts one short FluentControl run, sends the
    ODTC door macro, and returns FluentControl to edit mode. Only one ODTC operation runs at a time, and nothing
    is started while FluentControl is busy with another run or waiting on a dialog.
    """

    def __init__(self, backend: FluentOdtcBackend):
        super().__init__(originator="jp.riken.bdr", category="odtc", version="1.0")
        self.backend = backend

    async def _run(self, macro: str, status: sila.Status) -> float:
        try:
            return await run_with_progress(self.backend, macro, None, status, DOOR_S)
        except BackendError as exc:
            raise OdtcOperationFailed(str(exc)) from None

    @sila.ObservableCommand(errors=[OdtcOperationFailed])
    async def open_door(self, *, status: sila.Status) -> float:
        """
        Open the ODTC door so that a plate can be put in or taken out.

        Returns:
          DoorDuration: Seconds the door macro itself took (0 in dry-run mode).
        """
        return await self._run("SiLA-ODTC_OpenDoor", status)

    @sila.ObservableCommand(errors=[OdtcOperationFailed])
    async def close_door(self, *, status: sila.Status) -> float:
        """
        Close the ODTC door. Make sure nothing (hands, plate half inside) is in the way.

        Returns:
          DoorDuration: Seconds the door macro itself took (0 in dry-run mode).
        """
        return await self._run("SiLA-ODTC_CloseDoor", status)
