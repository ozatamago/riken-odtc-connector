"""RGA（プレートを掴む腕）でラボウェアを運ぶ Feature と、動かしたラボウェアの位置の記録。

FluentControl はランを始めるたびにラボウェアの位置をワークテーブルの初期位置に戻して覚え直す（2026-08-13 確認）。
「運ぶ」と「戻す」を別のランにするには、このコネクタが今の位置を覚えておき、次のランの最初に
RemoveLabware → AddLabware で FluentControl の記憶を実際の位置に合わせてから運ぶ（10-09 の回収と同じやり方）。
"""

import dataclasses
import json
import pathlib

from unitelabs.cdk import sila
from unitelabs.cdk.config import Config

from .fluent_backend import BackendError, FluentOdtcBackend
from .infinite.features import _with_progress
from .infinite.xml import add_labware_xml, remove_labware_xml, rga_xml


@dataclasses.dataclass
class PlateTransferConfig(Config):
    """RGA で運んでよいラボウェアと、その元の位置（ワークテーブルの初期位置）。"""

    labware: dict = dataclasses.field(
        default_factory=lambda: {
            # 2026-10-09 の手順書: 96 Well Flat_Custom[001] は 7mm Nest_Custom[006]（Nest7mm_Pos の site 6）
            "96 Well Flat_Custom[001]": {"type": "96 Well Flat_Custom", "base_location": "Nest7mm_Pos", "base_site": 6},
        }
    )
    positions_file: str = "~/.riken_fluent780/labware_positions.json"
    """動かしたラボウェアの今の位置を残すファイル。コネクタを起動し直しても覚えている。"""


class LabwareTracker:
    def __init__(self, config: PlateTransferConfig):
        self.config = config
        self.path = pathlib.Path(config.positions_file).expanduser()
        try:
            self.positions: dict[str, list] = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.positions = {}

    def spec(self, labware: str) -> dict:
        if labware not in self.config.labware:
            raise BackendError(f"{labware} は運んでよいラボウェアに登録されていません（設定 plates.labware）")
        return self.config.labware[labware]

    def base(self, labware: str) -> tuple[str, int]:
        s = self.spec(labware)
        return s["base_location"], int(s["base_site"])

    def where(self, labware: str) -> tuple[str, int]:
        """このコネクタが知っている今の位置。動かしていなければ元の位置。"""
        if labware in self.positions:
            loc, site = self.positions[labware]
            return loc, int(site)
        return self.base(labware)

    def moved_away(self, labware: str) -> bool:
        return labware in self.positions

    def record(self, labware: str, location: str, site: int) -> None:
        if (location, int(site)) == self.base(labware):
            self.positions.pop(labware, None)
        else:
            self.positions[labware] = [location, int(site)]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.positions, ensure_ascii=False, indent=1), encoding="utf-8")

    def move_steps(self, labware: str, location: str, site: int) -> list[tuple[str, str]]:
        """今の位置から (location, site) へ運ぶ命令。元の位置にないときは、先に FluentControl の記憶を合わせる。"""
        spec = self.spec(labware)
        steps: list[tuple[str, str]] = []
        if self.moved_away(labware):
            loc, s = self.where(labware)
            steps += [
                (f"RemoveLabware {labware}（記憶のみ）", remove_labware_xml(labware)),
                (f"AddLabware {labware} at {loc}[{s}]（記憶のみ）", add_labware_xml(spec["type"], labware, loc, s)),
            ]
        steps.append((f"RGA {labware} -> {location}[{site}]", rga_xml(labware, location, str(site), fixed_site=True)))
        return steps


class PlateTransferFailed(Exception):
    """The labware could not be moved. The description explains why (not registered, FluentControl busy, ...)."""


class RgaController(sila.Feature):
    """
    Move labware with the RGA (plate gripper) of the Fluent 780.

    Only labware listed in the connector configuration can be moved. The connector remembers where it put each
    labware, so a plate can be moved in one command and brought back in a later one: FluentControl forgets labware
    positions between runs, and the connector re-registers the plate at its actual position before moving it.
    If someone moves a plate by hand, tell the connector with RecordLabwarePosition before the next move.
    """

    def __init__(self, backend: FluentOdtcBackend, tracker: LabwareTracker):
        super().__init__(originator="jp.riken.bdr", category="fluent", version="1.0")
        self.backend = backend
        self.tracker = tracker

    async def _move(self, labware: str, location: str, site: int, status: sila.Status) -> str:
        try:
            steps = self.tracker.move_steps(labware, location, site)
            await _with_progress(
                status, len(steps),
                lambda report: self.backend.run_steps(steps, report, f"RGA: {labware} -> {location}[{site}]"),
            )
        except BackendError as exc:
            raise PlateTransferFailed(str(exc)) from None
        self.tracker.record(labware, location, site)
        return f"{location}[{site}]"

    @sila.UnobservableProperty()
    async def get_labware_positions(self) -> list[str]:
        """Movable labware and where the connector believes each one is ("name: location[site] (base ...)")."""
        out = []
        for name in self.tracker.config.labware:
            loc, site = self.tracker.where(name)
            bloc, bsite = self.tracker.base(name)
            note = "at base" if not self.tracker.moved_away(name) else f"base {bloc}[{bsite}]"
            out.append(f"{name}: {loc}[{site}] ({note})")
        return out

    @sila.ObservableCommand(errors=[PlateTransferFailed])
    async def move_labware(self, labware: str, location: str, site: int, *, status: sila.Status) -> str:
        """
        Move labware to a worktable position with the RGA (one FluentControl run).

        Args:
          Labware: Labware name, e.g. "96 Well Flat_Custom[001]". See LabwarePositions.
          Location: Target location name, e.g. "Regrip_Pos" (the regrip position used before the reader).
          Site: Target site number, e.g. 1.

        Returns:
          NewPosition: Where the labware is now, e.g. "Regrip_Pos[1]".
        """
        return await self._move(labware, location, site, status)

    @sila.ObservableCommand(errors=[PlateTransferFailed])
    async def return_labware(self, labware: str, *, status: sila.Status) -> str:
        """
        Bring labware back to its base position (its position in the worktable definition) with the RGA.

        Args:
          Labware: Labware name, e.g. "96 Well Flat_Custom[001]".

        Returns:
          NewPosition: The base position, e.g. "Nest7mm_Pos[6]".
        """
        try:
            location, site = self.tracker.base(labware)
        except BackendError as exc:
            raise PlateTransferFailed(str(exc)) from None
        return await self._move(labware, location, site, status)

    @sila.UnobservableCommand(errors=[PlateTransferFailed])
    async def record_labware_position(self, labware: str, location: str, site: int) -> str:
        """
        Tell the connector where a labware really is, without moving anything (e.g. after moving it by hand).

        Args:
          Labware: Labware name.
          Location: Location name where the labware is now.
          Site: Site number.

        Returns:
          NewPosition: The recorded position.
        """
        try:
            self.tracker.spec(labware)
        except BackendError as exc:
            raise PlateTransferFailed(str(exc)) from None
        self.tracker.record(labware, location, site)
        return f"{location}[{site}]"
