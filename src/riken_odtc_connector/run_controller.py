from unitelabs.cdk import sila

from .fluent_backend import BackendError, FluentOdtcBackend


class RunControlFailed(Exception):
    """The run could not be started or ended, or no definition change was waiting."""


class RunController(sila.Feature):
    """
    Hold one FluentControl run open across several commands, and handle the definition check.

    Normally every command starts and ends a FluentControl run of its own. Between StartRun and EndRun all commands
    are sent into the same run instead, so the tips on the arm and the labware positions carry over from one command
    to the next (needed for GetTips -> Aspirate -> Dispense -> DropTips). StartRun and EndRun themselves do not move
    anything.

    Before any command is sent, every FluentControl definition it uses (liquid classes, tips, labware types,
    locations on the worktable, arm settings) is checked and saved with the command in a record folder. If a
    definition changed since it was last accepted, the command is refused; check the change, call
    AcceptDefinitionChanges and send the command again.
    """

    def __init__(self, backend: FluentOdtcBackend):
        super().__init__(originator="jp.riken.bdr", category="fluent", version="1.0")
        self.backend = backend

    @sila.ObservableCommand(errors=[RunControlFailed])
    async def start_run(self, *, status: sila.Status) -> str:
        """
        Start a FluentControl run (OFML_ApiHost) that the following commands share. Does not move anything.

        Returns:
          RecordFolder: Folder on the connector PC where the records of the commands in this run are saved.
        """
        try:
            return await self.backend.start_run()
        except BackendError as exc:
            raise RunControlFailed(str(exc)) from None

    @sila.ObservableCommand(errors=[RunControlFailed])
    async def end_run(self, *, status: sila.Status) -> int:
        """
        End the run started by StartRun. Drop the tips first if any are mounted.

        Returns:
          Commands: Number of commands sent in the run.
        """
        try:
            return await self.backend.end_run()
        except BackendError as exc:
            raise RunControlFailed(str(exc)) from None

    @sila.UnobservableProperty()
    async def get_run_open(self) -> bool:
        """True between StartRun and EndRun."""
        return self.backend.run_open

    @sila.UnobservableProperty()
    async def get_run_started(self) -> str:
        """When the open run started (YYYYMMDD-HHMMSS). Empty when no run is open."""
        return self.backend.run_started

    @sila.UnobservableProperty()
    async def get_pending_definition_changes(self) -> list[str]:
        """Definitions that changed since they were last accepted and made the last command refuse ("name (file)")."""
        return [f"{c['name']} ({rel})" for rel, c in self.backend.preflight.pending.items()]

    @sila.UnobservableCommand(errors=[RunControlFailed])
    async def accept_definition_changes(self) -> list[str]:
        """
        Accept the changed definitions listed in PendingDefinitionChanges, after checking them. Moves nothing.

        Returns:
          Accepted: The definitions accepted ("name (file)").
        """
        if not self.backend.preflight.pending:
            raise RunControlFailed("No definition change is waiting to be accepted")
        return self.backend.preflight.accept_pending_changes()

    @sila.UnobservableProperty()
    async def get_last_record(self) -> str:
        """Record folder of the last checked command (XML sent, definitions used, result)."""
        return self.backend.preflight.last_record
