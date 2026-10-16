"""FluentControl's pipetting statements of the Air FCA as SiLA commands (stage 4-4).

GetTips, Aspirate, Dispense, Mix and DropTips each send one FluentControl statement. The XML is the 1-channel form
that ran on the instrument in August 2026 (tests/fixtures/liha_verified); only the values change (labware, wells,
volumes, liquid class, channels, tip type). tests/test_gui_statements.py checks that these forms have the same
structure as the statements of the 01-07 GUI scripts.

They only work inside a run opened with RunController.StartRun, so that FluentControl keeps the mounted tips from one
command to the next, and on labware added in that run with LabwareController.AddLabware or already registered on FluentControl's
RuntimeWorkspace (so the type and its well layout are known). Refused before sending (lessons from the instrument):

- a second Aspirate with the same tips (it always failed in August 2026; one aspiration per tip, many dispenses are fine)
- Aspirate / Dispense / Mix without tips, GetTips with tips, EndRun with tips (state mix-ups)
- a volume above what the tip can hold (MaxNettoVolumeForTransferPhase of the tip definition)
- a liquid class without settings for the mounted tip type on the Air FCA
- 10 ul tips on a PCR plate on the 7 mm nest (they did not reach the bottom on 2026-10-09)
- 1000 ul tips from rack FCA_ 1000ul SBS[001] (the tips did not fit and fell off in August 2026)

The record of each command gets the liquid class settings for the mounted tip type (speeds, air gaps, liquid level
detection, positions) under "liquid_class_settings".
"""

import pathlib
import re

from unitelabs.cdk import sila

from .definitions import DefinitionCheckFailed, _unescape, liquid_class_settings, tip_values
from .fluent_backend import BackendError, FluentOdtcBackend
from .infinite.features import _with_progress
from .infinite.xml import _esc, wrap

DATA = pathlib.Path(__file__).resolve().parent / "data"
TIP_TYPES = ("FCA, 10ul SBS", "FCA, 200ul SBS", "FCA, 200ul Filtered SBS", "FCA, 1000ul SBS", "FCA, 1000ul Filtered SBS")
"""Tip types used by the 01-07 GUI scripts."""
BAD_RACKS = {"FCA_ 1000ul SBS[001]": "the tips in this rack do not fit and fell off (August 2026); use FCA_ 1000ul SBS[002]"}
WASTE = "FCA Thru Deck Waste Chute_1"
TIP_SPACING = 9  # mm; every verified and GUI statement on 96-well plates and troughs uses 9


class LiquidHandlingFailed(Exception):
    """The pipetting command was refused or failed. The description says why."""


# ---------------------------------------------------------------- XML
def _template(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8").strip()


def _set(xml: str, tag: str, value: str) -> str:
    new, n = re.subn(rf"<{tag}>[^<]*</{tag}>|<{tag} />", f"<{tag}>{_esc(value)}</{tag}>", xml, count=1)
    if n != 1:
        raise BackendError(f"Template has no {tag}")
    return new


def _set_list(xml: str, tag: str, values: list, kind: str) -> str:
    """Replace the items of a list element (<Volumes>, <SelectedTipsIndexes>) keeping the template's item form."""
    elem = {"System.String": "string", "System.Int32": "int"}[kind]
    items = "".join(f'<Object Type="{kind}">\n<{elem}>{_esc(str(v))}</{elem}>\n</Object>\n' for v in values)
    new, n = re.subn(rf"<{tag}>.*?</{tag}>|<{tag} />", f"<{tag}>\n{items}</{tag}>", xml, count=1, flags=re.S)
    if n != 1:
        raise BackendError(f"Template has no {tag}")
    return new


def well_index(well: str, rows: int, columns: int) -> int:
    """Column-major index from 0, as FluentControl serializes it (A1 = 0, B1 = 1, ..., A2 = rows)."""
    m = re.fullmatch(r"([A-Za-z])(\d+)", well.strip())
    if not m:
        raise BackendError(f"Well '{well}' is not like A1")
    row, col = ord(m.group(1).upper()) - ord("A"), int(m.group(2)) - 1
    if not (0 <= row < rows and 0 <= col < columns):
        raise BackendError(f"Well {well} is outside the labware ({rows} rows x {columns} columns)")
    return col * rows + row


def wells_fields(wells: list[str], rows: int, columns: int) -> tuple[str, str]:
    """(SerializedWellIndexes, SelectedWellsString) in the forms the GUI scripts use."""
    idx = [well_index(w, rows, columns) for w in wells]
    names = [w.strip().upper() for w in wells]
    if len(set(idx)) == 1:
        return f"{idx[0]};" * len(idx), f"{len(idx)} * {names[0]}"
    if all(b - a == 1 for a, b in zip(idx, idx[1:])):
        return f"{idx[0]}>1>{idx[-1]};", f"{names[0]} - {names[-1]}"
    return "".join(f"{i};" for i in idx), ", ".join(names)


def get_tips_xml(tip_type: str, channels: list[int], rack: str = "") -> str:
    xml = _template("liha_gettips_reference.xml")
    xml = _set_list(xml, "SelectedTipsIndexes", [c - 1 for c in channels], "System.Int32")
    xml = _set(xml, "LabwareName", rack)
    xml = re.sub(r"TOOLNAME:[^<]*<", f"TOOLNAME:{_esc(tip_type)}<", xml, count=1)
    return wrap(xml)


def drop_tips_xml(channels: list[int]) -> str:
    xml = _template("liha_droptips_reference.xml")
    xml = _set_list(xml, "SelectedTipsIndexes", [c - 1 for c in channels], "System.Int32")
    return wrap(_set(xml, "LabwareName", WASTE))


def pipetting_xml(kind: str, labware: str, wells: list[str], volumes: list[float], liquid_class: str,
                  channels: list[int], rows: int, columns: int, cycles: int | None = None) -> str:
    """Aspirate / Dispense / Mix of the given channels; wells and volumes are one per channel."""
    xml = _template({"Aspirate": "liha_aspirate_reference.xml", "Dispense": "liha_dispense_reference.xml",
                     "Mix": "liha_mix_reference.xml"}[kind])
    serialized, shown = wells_fields(wells, rows, columns)
    xml = _set_list(xml, "Volumes", [f"{v:g}" for v in volumes], "System.String")
    xml = _set(xml, "LiquidClassNameBySelection", liquid_class)
    xml = _set(xml, "SerializedWellIndexes", serialized)
    xml = _set(xml, "SelectedWellsString", shown)
    xml = _set(xml, "WellOffset", "0")
    xml = _set_list(xml, "SelectedTipsIndexes", [c - 1 for c in channels], "System.Int32")
    xml = _set(xml, "TipSpacing", str(TIP_SPACING))
    xml = _set(xml, "LabwareName", labware)
    if cycles is not None:
        xml = _set(xml, "Cycles", str(int(cycles)))
    return wrap(xml)


# ---------------------------------------------------------------- the feature
class LiquidHandling(sila.Feature):
    """
    Pipetting with the Air FCA (8 channels): GetTips, Aspirate, Dispense, Mix, DropTips. Each command sends one
    FluentControl statement. All of them MOVE the arm.

    Use them between RunController.StartRun and EndRun, on labware added with LabwareController.AddLabware. Channels
    are numbered 1-8; give one well and one volume per channel (e.g. Channels [1], Wells ["A1"], Volumes [50]).
    One aspiration per set of tips: GetTips -> Aspirate -> Dispense (one or more) -> DropTips. Volumes are in ul.
    """

    def __init__(self, backend: FluentOdtcBackend):
        super().__init__(originator="jp.riken.bdr", category="fluent", version="1.0")
        self.backend = backend
        self.pc = None
        """Where definitions are read from; None means the Fluent PC of the backend (tests put a stand-in here)."""
        self._cache: dict[tuple, object] = {}

    # ---- lookups in the FluentControl definitions (cached by file hash)
    def _definition(self, name: str, ext: str) -> bytes:
        pc = self.pc or self.backend.preflight.pc
        if "index" not in self._cache:
            self._cache["index"] = pc.index()
        index = self._cache["index"]
        hits = sorted(p for p, e in index.items() if p.lower().endswith(ext) and e.get("name") == name)
        if not hits:
            raise LiquidHandlingFailed(f"'{name}' is not defined in FluentControl")
        key = (hits[0], index[hits[0]]["sha256"])
        if key not in self._cache:
            self._cache[key] = pc.read(hits[0])
        return self._cache[key]

    def _layout(self, labware_type: str) -> tuple[int, int]:
        t = _unescape(self._definition(labware_type, ".xcmp").decode("utf-8-sig", errors="replace"))
        x = re.search(r"<(?:\w+:)?XNumberOfWells>(\d+)<", t)
        y = re.search(r"<(?:\w+:)?YNumberOfWells>(\d+)<", t)
        if not (x and y):
            raise LiquidHandlingFailed(f"No well layout in the definition of {labware_type}")
        return int(y.group(1)), int(x.group(1))  # rows, columns

    def _refresh(self) -> None:
        self._cache.pop("index", None)

    # ---- checks
    def _need_run(self) -> None:
        if not self.backend.run_open:
            raise LiquidHandlingFailed("Start a run first (RunController.StartRun): FluentControl must keep the tips "
                                       "mounted between commands")

    @staticmethod
    def _channels(channels: list[int]) -> list[int]:
        if not channels or len(set(channels)) != len(channels) or any(not 1 <= c <= 8 for c in channels):
            raise LiquidHandlingFailed("Channels must be distinct numbers from 1 to 8")
        return list(channels)

    def _labware_type(self, labware: str) -> str:
        """Type of labware added in this run, or already registered on FluentControl's RuntimeWorkspace."""
        t = self.backend.session_labware.get(labware)
        if t:
            return t
        try:
            pc = self.pc or self.backend.preflight.pc
            if "index" not in self._cache:
                self._cache["index"] = pc.index()
            known = self.backend.preflight.runtime_labware(self._cache["index"], pc)
        except DefinitionCheckFailed:
            known = {}
        if labware in known and known[labware]["type"]:
            return known[labware]["type"]
        raise LiquidHandlingFailed(f"{labware} is neither added in this run nor registered on FluentControl's "
                                   "worktable; add it with LabwareController.AddLabware")

    async def _send(self, label: str, xml: str, status: sila.Status, extra: dict | None = None) -> None:
        try:
            await _with_progress(status, 1, lambda report: self.backend.run_steps([(label, xml)], report, label, extra))
        except BackendError as exc:
            raise LiquidHandlingFailed(str(exc)) from None

    # ---- commands
    @sila.ObservableCommand(errors=[LiquidHandlingFailed])
    async def get_tips(self, tip_type: str, channels: list[int], rack: str, *, status: sila.Status) -> None:
        """
        Pick up disposable tips on the given channels (FluentControl takes the next unused tips). MOVES the arm.

        Args:
          TipType: "FCA, 10ul SBS", "FCA, 200ul SBS", "FCA, 200ul Filtered SBS", "FCA, 1000ul SBS" or
            "FCA, 1000ul Filtered SBS".
          Channels: Channels 1-8 that get a tip, e.g. [1].
          Rack: Rack to take the tips from, e.g. "FCA_ 1000ul SBS[002]", or empty to let FluentControl choose.
        """
        self._need_run()
        self._refresh()
        channels = self._channels(channels)
        if tip_type not in TIP_TYPES:
            raise LiquidHandlingFailed(f"TipType must be one of {', '.join(TIP_TYPES)}")
        if rack in BAD_RACKS:
            raise LiquidHandlingFailed(f"{rack}: {BAD_RACKS[rack]}")
        if tip_type.startswith("FCA, 1000ul SBS") and not rack:
            raise LiquidHandlingFailed("Name the rack for 1000 ul tips (FCA_ 1000ul SBS[002]); rack [001] does not fit")
        mounted = sorted(set(channels) & set(self.backend.tips))
        if mounted:
            raise LiquidHandlingFailed(f"Channels {mounted} already have tips; drop them first")
        try:
            values = tip_values(self._definition(tip_type, ".xcmp"))
        except DefinitionCheckFailed as exc:
            raise LiquidHandlingFailed(str(exc)) from None
        await self._send(f"GetTips {tip_type} ch{channels}" + (f" from {rack}" if rack else ""),
                         get_tips_xml(tip_type, channels, rack), status, {"tip": {"type": tip_type, **values}})
        for c in channels:
            self.backend.tips[c] = {"type": tip_type, "group": values.get("DiTiTypeGroup", ""),
                                    "capacity": float(values.get("MaxNettoVolumeForTransferPhase", 0) or 0),
                                    "aspirated": False, "volume": 0.0}

    async def _pipette(self, kind: str, labware: str, wells: list[str], volumes: list[float], liquid_class: str,
                       channels: list[int], status: sila.Status, cycles: int | None = None) -> None:
        self._need_run()
        self._refresh()
        channels = self._channels(channels)
        if not (len(wells) == len(volumes) == len(channels)):
            raise LiquidHandlingFailed("Give one well and one volume per channel")
        if any(v <= 0 for v in volumes):
            raise LiquidHandlingFailed("Volumes must be above 0")
        missing = [c for c in channels if c not in self.backend.tips]
        if missing:
            raise LiquidHandlingFailed(f"Channels {missing} have no tips; GetTips first")
        tips = [self.backend.tips[c] for c in channels]
        labware_type = self._labware_type(labware)
        if any(t["type"] == "FCA, 10ul SBS" for t in tips) and "PCR" in labware_type:
            raise LiquidHandlingFailed("10 ul tips do not reach the bottom of a PCR plate on the 7 mm nest (2026-10-09); "
                                       "use 50 ul or larger")
        for c, t, v in zip(channels, tips, volumes):
            if kind == "Aspirate":
                if t["aspirated"]:
                    raise LiquidHandlingFailed(f"Channel {c} already aspirated with these tips; a second aspiration "
                                               "with the same tips fails. DropTips and GetTips first")
                if t["capacity"] and v > t["capacity"]:
                    raise LiquidHandlingFailed(f"{v:g} ul is more than a {t['type']} tip holds ({t['capacity']:g} ul)")
            elif kind == "Dispense" and v > t["volume"] + 1e-9:
                raise LiquidHandlingFailed(f"Channel {c} holds {t['volume']:g} ul; cannot dispense {v:g} ul")
            elif kind == "Mix" and t["capacity"] and v > t["capacity"]:
                raise LiquidHandlingFailed(f"{v:g} ul is more than a {t['type']} tip holds ({t['capacity']:g} ul)")
        try:
            rows, columns = self._layout(labware_type)
            settings = {}
            for group in sorted({t["group"] for t in tips}):
                s = liquid_class_settings(self._definition(liquid_class, ".xlqc"), group)
                if s is None:
                    raise LiquidHandlingFailed(f"Liquid class '{liquid_class}' has no Air FCA settings for {group} tips")
                settings[group] = s
            xml = pipetting_xml(kind, labware, wells, volumes, liquid_class, channels, rows, columns, cycles)
        except (BackendError, DefinitionCheckFailed) as exc:
            raise LiquidHandlingFailed(str(exc)) from None
        label = f"{kind} {'/'.join(f'{v:g}' for v in volumes)} ul {labware} {','.join(wells)} ch{channels} ({liquid_class})"
        await self._send(label, xml, status, {"liquid_class_settings": {liquid_class: settings}})
        for t, v in zip(tips, volumes):
            if kind == "Aspirate":
                t["aspirated"], t["volume"] = True, t["volume"] + v
            elif kind == "Dispense":
                t["volume"] = max(0.0, t["volume"] - v)

    @sila.ObservableCommand(errors=[LiquidHandlingFailed])
    async def aspirate(self, labware: str, wells: list[str], volumes: list[float], liquid_class: str,
                       channels: list[int], *, status: sila.Status) -> None:
        """
        Aspirate with the given channels (one aspiration per set of tips). MOVES the arm.

        Args:
          Labware: Name of the labware added in this run, e.g. "WaterTrough[001]".
          Wells: One well per channel, e.g. ["A1"].
          Volumes: One volume in ul per channel, e.g. [50].
          LiquidClass: FluentControl liquid class, e.g. "Water Free Single".
          Channels: Channels 1-8, e.g. [1].
        """
        await self._pipette("Aspirate", labware, wells, volumes, liquid_class, channels, status)

    @sila.ObservableCommand(errors=[LiquidHandlingFailed])
    async def dispense(self, labware: str, wells: list[str], volumes: list[float], liquid_class: str,
                       channels: list[int], *, status: sila.Status) -> None:
        """
        Dispense with the given channels (several dispenses from one aspiration are fine). MOVES the arm.

        Args:
          Labware: Name of the labware added in this run, e.g. "TestPlate[001]".
          Wells: One well per channel, e.g. ["A1"].
          Volumes: One volume in ul per channel, e.g. [50].
          LiquidClass: FluentControl liquid class; use the one used for aspirating.
          Channels: Channels 1-8, e.g. [1].
        """
        await self._pipette("Dispense", labware, wells, volumes, liquid_class, channels, status)

    @sila.ObservableCommand(errors=[LiquidHandlingFailed])
    async def mix(self, labware: str, wells: list[str], volumes: list[float], liquid_class: str, channels: list[int],
                  cycles: int, *, status: sila.Status) -> None:
        """
        Mix by aspirating and dispensing in place. MOVES the arm.

        Args:
          Labware: Name of the labware added in this run.
          Wells: One well per channel.
          Volumes: Mixing volume in ul per channel.
          LiquidClass: FluentControl liquid class, e.g. "Water Mix_1".
          Channels: Channels 1-8.
          Cycles: Number of mixing cycles (01-07 use 1, 5 and 10).
        """
        if not 1 <= cycles <= 20:
            raise LiquidHandlingFailed("Cycles must be 1 to 20")
        await self._pipette("Mix", labware, wells, volumes, liquid_class, channels, status, cycles)

    @sila.ObservableCommand(errors=[LiquidHandlingFailed])
    async def drop_tips(self, channels: list[int], *, status: sila.Status) -> None:
        """
        Drop the tips of the given channels into the waste chute. MOVES the arm.

        Args:
          Channels: Channels 1-8 whose tips are dropped, e.g. [1].
        """
        self._need_run()
        channels = self._channels(channels)
        missing = [c for c in channels if c not in self.backend.tips]
        if missing:
            raise LiquidHandlingFailed(f"Channels {missing} have no tips")
        await self._send(f"DropTips ch{channels}", drop_tips_xml(channels), status)
        for c in channels:
            self.backend.tips.pop(c, None)

    @sila.UnobservableProperty()
    async def get_mounted_tips(self) -> list[str]:
        """Tips mounted in the open run, e.g. "channel 1: FCA, 200ul SBS, aspirated, holds 50 ul"."""
        return [f"channel {c}: {t['type']}, {'aspirated' if t['aspirated'] else 'fresh'}, holds {t['volume']:g} ul"
                for c, t in sorted(self.backend.tips.items())]
