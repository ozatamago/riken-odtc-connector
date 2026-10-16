"""FluentControl's pipetting statements of the Air FCA as SiLA commands (stage 4-4).

GetTips, Aspirate, Dispense, Mix, DropTips and MoveArm each send one FluentControl statement. The XML is the 1-channel form
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

import asyncio
import pathlib
import re

from unitelabs.cdk import sila

from .definitions import DefinitionCheckFailed, _unescape, liquid_class_settings, tip_values
from .fluent_backend import BackendError, FluentOdtcBackend
from .infinite.features import _with_progress
from .infinite.xml import _esc, wrap

DATA = pathlib.Path(__file__).resolve().parent / "data"
TIP_TYPES = ("FCA, 10ul SBS", "FCA, 50ul SBS", "FCA, 200ul SBS", "FCA, 200ul Filtered SBS", "FCA, 1000ul SBS",
             "FCA, 1000ul Filtered SBS")
"""Tip types used by the 01-07 GUI scripts (50ul SBS only by the worklists of 02 and 07)."""
BAD_RACKS = {"FCA_ 1000ul SBS[001]": "the tips in this rack do not fit and fell off (August 2026); use FCA_ 1000ul SBS[002]"}
WASTE = "FCA Thru Deck Waste Chute_1"
TIP_SPACING = 9  # mm; every verified and GUI statement on 96-well plates and troughs uses 9


DETECTED_VOLUMES_PS1 = r"""
$d = 'C:\ProgramData\Tecan\LoggingServer\LogFiles'
$f = Get-ChildItem "$d\*.ulf" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$fs = [System.IO.File]::Open($f.FullName, 'Open', 'Read', 'ReadWrite'); $t = (New-Object System.IO.StreamReader($fs)).ReadToEnd(); $fs.Close()
foreach ($m in [regex]::Matches($t, 'TimeStamp="([0-9-]+ [0-9:.]+)"[^>]*Message="channel (\d+): detected volume ([0-9.]+) ul')) {
  if ($m.Groups[1].Value -ge '__SINCE__') { $m.Groups[2].Value + ' ' + $m.Groups[3].Value }
}
"""
"""Reads FluentControl's log on the Fluent PC for the volumes Detect Liquid found (the statement returns nothing)."""

FILTER_RESULTS_PS1 = DETECTED_VOLUMES_PS1.replace(
    "Message=\"channel (\\d+): detected volume ([0-9.]+) ul')) {", "Message=\"(Channel \\d+ : Filter [^\"]*)')) {").replace(
    "{ $m.Groups[2].Value + ' ' + $m.Groups[3].Value }", "{ $m.Groups[2].Value }")
"""The same for the verdicts of the inline filter check ("Channel 1 : Filter OK (passed) (...)", 2026-10-16)."""


def _evaluate(formula: str, volume: float) -> float | None:
    """A liquid-class formula that only uses numbers, + - * / and "volume" (e.g. "0.25*volume"); None otherwise."""
    import ast
    import operator

    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}

    def ev(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id == "volume":
            return volume
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        raise ValueError

    try:
        return ev(ast.parse(formula.strip(), mode="eval").body)
    except (SyntaxError, ValueError, ZeroDivisionError):
        return None


def corrected_volume(formula: str, volume: float) -> float:
    """The volume FluentControl moves to deliver volume, from AdjustAccuracy(volume, set1, delivered1, ...): the set
    volume is interpolated from the delivered volumes (50 ul -> about 53.5 ul for 200 ul tips; FluentControl logged
    53.52 on 2026-10-16). Returns volume itself for other formulas."""
    m = re.fullmatch(r"\s*AdjustAccuracy\(\s*volume\s*,(.*)\)\s*", formula or "")
    if not m:
        return volume
    nums = [float(x) for x in m.group(1).split(",")]
    pairs = sorted(zip(nums[1::2], nums[0::2]))  # (delivered, set)
    if len(pairs) < 2:
        return volume
    for (d0, s0), (d1, s1) in zip(pairs, pairs[1:]):
        if volume <= d1 or (d1, s1) == pairs[-1]:
            return s0 + (volume - d0) * (s1 - s0) / (d1 - d0)
    return volume


def planned_aspiration(equations: dict, volume: float, dispenses: int) -> dict:
    """What one aspiration for `dispenses` dispenses of `volume` takes up, from the liquid class equations: the
    dispensed volume corrected as a whole, plus the excess volume (Water Free Multi: 25 %) and conditioning volume.
    FluentControl aspirated 116.97 ul for 3 x 30 ul with Water Free Multi (94.47 + 22.5; 2026-10-16)."""
    total = volume * dispenses
    corrected = corrected_volume(equations.get("CorrectionVolumeFormular", ""), total)
    excess = _evaluate(equations.get("ExcessVolumeFormular", "0") or "0", total)
    conditioning = _evaluate(equations.get("ConditioningVolumeFormular", "0") or "0", total)
    known = excess is not None and conditioning is not None
    return {"dispensed": total, "corrected": round(corrected, 2), "excess": excess, "conditioning": conditioning,
            "liquid_in_tip": round(corrected + (excess or 0) + (conditioning or 0), 2), "formulas_understood": known}


class LiquidHandlingFailed(Exception):
    """The pipetting command was refused or failed. The description says why."""


# ---------------------------------------------------------------- XML
def _template(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8").strip()


def _set(xml: str, tag: str, value: str) -> str:
    new, n = re.subn(rf"<{tag}>[^<]*</{tag}>|<{tag} />", lambda _: f"<{tag}>{_esc(value)}</{tag}>", xml, count=1)
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


def move_arm_xml(labware: str, well: str, channels: list[int], rows: int, columns: int) -> str:
    """Move the Air FCA arm at travel height to a well (FluentControl's Move Arm, "travel to labware"). The form is the
    one in FluentControl's own "AirFCA Leakage Test and cLLD Self Test" script; nothing is aspirated or dispensed."""
    xml = _template("liha_movearm_reference.xml")
    serialized, shown = wells_fields([well] * len(channels), rows, columns)
    xml = _set(xml, "SerializedWellIndexes", serialized)
    xml = _set(xml, "SelectedWellsString", shown)
    xml = _set_list(xml, "SelectedTipsIndexes", [c - 1 for c in channels], "System.Int32")
    return wrap(_set(xml, "LabwareName", labware))


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

    @sila.ObservableCommand(errors=[LiquidHandlingFailed])
    async def move_arm(self, labware: str, well: str, *, status: sila.Status) -> None:
        """
        Move the pipetting arm at travel height above a well, e.g. to clear the view of the deck cameras. Nothing is
        aspirated or dispensed and mounted tips stay on. MOVES the arm.

        Args:
          Labware: Labware to move above, e.g. "4 Well SBS Trough[001]".
          Well: Well that channel 1 goes above, e.g. "A1".
        """
        self._need_run()
        self._refresh()
        rows, columns = self._layout(self._labware_type(labware))
        try:
            xml = move_arm_xml(labware, well, [1], rows, columns)
        except BackendError as exc:
            raise LiquidHandlingFailed(str(exc)) from None
        await self._send(f"MoveArm to {labware} {well}", xml, status)

    # ---- stage 4-5: the remaining statements of the 01-07 scripts (XML in smart_xml.py)
    def _geometry(self, labware: str) -> tuple[str, dict]:
        from .definitions import _unescape
        from .smart_xml import labware_geometry

        labware_type = self._labware_type(labware)
        xcmp = _unescape(self._definition(labware_type, ".xcmp").decode("utf-8-sig", errors="replace"))
        return labware_type, labware_geometry(xcmp)

    def _tip_and_class(self, tip_type: str, liquid_class: str, volumes: list[float], labware_types: list[str],
                       dispenses: int = 1) -> dict:
        """Checks for statements that take their own tips (Smart Commands, worklists); returns the record extras.
        dispenses: dispenses per aspiration; the liquid in the tip then is checked against the tip size."""
        if tip_type not in TIP_TYPES:
            raise LiquidHandlingFailed(f"TipType must be one of {', '.join(TIP_TYPES)}")
        if tip_type.startswith("FCA, 1000ul SBS"):
            raise LiquidHandlingFailed("FluentControl picks the rack itself here and may take FCA_ 1000ul SBS[001], whose "
                                       "tips do not fit (August 2026); use 1000 ul Filtered tips or GetTips with a rack")
        if self.backend.tips:
            raise LiquidHandlingFailed(f"Channels {sorted(self.backend.tips)} have tips; this command takes and drops "
                                       "its own tips, so DropTips first")
        if tip_type == "FCA, 10ul SBS" and any("PCR" in t for t in labware_types):
            raise LiquidHandlingFailed("10 ul tips do not reach the bottom of a PCR plate on the 7 mm nest (2026-10-09)")
        if any(v <= 0 for v in volumes):
            raise LiquidHandlingFailed("Volumes must be above 0")
        values = tip_values(self._definition(tip_type, ".xcmp"))
        capacity = float(values.get("MaxNettoVolumeForTransferPhase", 0) or 0)
        group = values.get("DiTiTypeGroup", "")
        settings = liquid_class_settings(self._definition(liquid_class, ".xlqc"), group)
        if settings is None:
            raise LiquidHandlingFailed(f"Liquid class '{liquid_class}' has no Air FCA settings for {group} tips")
        plan = planned_aspiration(settings["equations"], max(volumes), max(1, int(dispenses)))
        if capacity and plan["liquid_in_tip"] > capacity:
            raise LiquidHandlingFailed(
                f"One aspiration would take up about {plan['liquid_in_tip']:g} ul ({plan['dispensed']:g} ul to dispense, "
                f"corrected to {plan['corrected']:g}, plus excess {plan['excess']} and conditioning "
                f"{plan['conditioning']} ul from '{liquid_class}'); a {tip_type} tip holds {capacity:g} ul. "
                "Use fewer dispenses per aspiration or larger tips")
        return {"tip": {"type": tip_type, **values}, "liquid_class_settings": {liquid_class: {group: settings}},
                "planned_aspiration": plan}

    def _put_file(self, name: str, text: str) -> str:
        """Save a file the statement reads (worklist, volume list) in the open run's record on the Fluent PC."""
        import datetime

        folder = self.backend.session_dir
        path = f"{folder}\\files\\{datetime.datetime.now():%Y%m%d-%H%M%S}_{name}"
        if not self.backend.config.dry_run:
            (self.pc or self.backend.preflight.pc).write_file(path, text)
        return path

    @sila.ObservableCommand(errors=[LiquidHandlingFailed])
    async def check_inline_filters(self, channels: list[int], *, status: sila.Status) -> list[str]:
        """
        Check the inline filters of the given channels (FluentControl's Inline Filter Check, used by 02).

        Args:
          Channels: Channels 1-8 to check, e.g. [1, 2, 3, 4, 5, 6, 7, 8].

        Returns:
          Results: FluentControl's verdict per channel, e.g. "Channel 1 : Filter OK (passed) (relative filter
            resistance = 1.31 ...)", read from its log (a pressure test; the arm does not move).
        """
        from .smart_xml import inline_filter_check_xml

        self._need_run()
        channels = self._channels(channels)
        pc = self.pc or self.backend.preflight.pc
        since = "" if self.backend.config.dry_run else self._pc_time(pc)
        await self._send(f"InlineFilterCheck ch{channels}", inline_filter_check_xml(channels), status)
        if self.backend.config.dry_run or not since:
            return []
        try:
            text = await asyncio.to_thread(pc.run_ps, FILTER_RESULTS_PS1.replace("__SINCE__", since), 120)
        except Exception:  # noqa: BLE001 - the check itself passed; only reading the verdict failed
            return []
        return [ln.strip() for ln in text.decode("utf-8-sig", errors="replace").splitlines() if ln.strip()]

    @sila.ObservableCommand(errors=[LiquidHandlingFailed])
    async def detect_liquid(self, labware: str, wells: list[str], channels: list[int], *,
                            status: sila.Status) -> list[float]:
        """
        Check that there is liquid in the given wells with the mounted tips (liquid level detection, as 02 does before
        pipetting primers). Fails if FluentControl reports an error. MOVES the arm.

        Args:
          Labware: Labware to check, e.g. "4 Well SBS Trough[001]".
          Wells: One well per channel, e.g. ["A1"].
          Channels: Channels 1-8 with tips, e.g. [1].

        Returns:
          DetectedVolumes: Volume in each well in ul, as FluentControl computes it from the liquid level and the well
            shape (read from FluentControl's log; -1 if it could not be read).
        """
        from .smart_xml import detect_liquid_xml

        self._need_run()
        self._refresh()
        channels = self._channels(channels)
        if len(wells) != len(channels):
            raise LiquidHandlingFailed("Give one well per channel")
        missing = [c for c in channels if c not in self.backend.tips]
        if missing:
            raise LiquidHandlingFailed(f"Channels {missing} have no tips; GetTips first")
        try:
            _, geo = self._geometry(labware)
            spacing = geo["yspacing"] if geo["ycount"] > 1 and geo["shape"] == "Ellipse" else TIP_SPACING
            xml = detect_liquid_xml(labware, wells, channels, geo["ycount"], geo["xcount"], spacing)
        except (BackendError, DefinitionCheckFailed) as exc:
            raise LiquidHandlingFailed(str(exc)) from None
        pc = self.pc or self.backend.preflight.pc
        since = "" if self.backend.config.dry_run else self._pc_time(pc)
        await self._send(f"DetectLiquid {labware} {','.join(wells)} ch{channels}", xml, status)
        if self.backend.config.dry_run:
            return [-1.0 for _ in channels]
        found = await asyncio.to_thread(self._detected_volumes, pc, since)
        return [found.get(c - 1, -1.0) for c in channels]

    @staticmethod
    def _pc_time(pc) -> str:
        try:
            return pc.run_ps("Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'", 60).decode("utf-8-sig").strip()
        except Exception:  # noqa: BLE001 - only the result reading suffers
            return ""

    @staticmethod
    def _detected_volumes(pc, since: str) -> dict[int, float]:
        """Volumes FluentControl logged ("channel 0: detected volume 33940.72 ul", 2026-10-16) since a PC time."""
        if not since:
            return {}
        script = DETECTED_VOLUMES_PS1.replace("__SINCE__", since)
        try:
            text = pc.run_ps(script, 120).decode("utf-8-sig", errors="replace")
        except Exception:  # noqa: BLE001
            return {}
        found: dict[int, float] = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2:
                found[int(parts[0])] = float(parts[1])  # the last detection of a channel wins
        return found

    @sila.ObservableCommand(errors=[LiquidHandlingFailed])
    async def set_liquid_detection_error_handling(self, aspirate: str, contact_dispense: str,
                                                  aspiration_supervision: str, retract_supervision: str,
                                                  pressure_aspirate: str, pressure_dispense: str, pmp_aspiration: str,
                                                  *, status: sila.Status) -> None:
        """
        Set what FluentControl does on pipetting errors for the rest of the run (as 06 does before each step). Each
        is "UseMicroscriptSettings" (the liquid class decides; also when empty), "AspirateAvailable" or
        "IgnoreAndContinue". "UserPrompt" is refused: it opens a dialog that an API run cannot answer.

        Args:
          Aspirate: Liquid level detection errors when aspirating.
          ContactDispense: Liquid level detection errors when dispensing into liquid.
          AspirationSupervision: Aspiration supervision failed.
          RetractSupervision: Retract supervision failed (liquid exit not found).
          PressureAspirate: Pressure out of range while aspirating.
          PressureDispense: Pressure out of range while dispensing.
          PmpAspiration: Pressure monitoring (PMP) errors when aspirating.
        """
        from .smart_xml import set_clld_error_handling_xml

        self._need_run()
        given = {"Aspirate": aspirate, "ContactDispense": contact_dispense,
                 "AspirationSupervision": aspiration_supervision, "RetractSupervision": retract_supervision,
                 "PressureAspirate": pressure_aspirate, "PressureDispense": pressure_dispense,
                 "PmpAspiration": pmp_aspiration}
        actions = {k: (v or "UseMicroscriptSettings") for k, v in given.items()}
        try:
            xml = set_clld_error_handling_xml(actions)
        except BackendError as exc:
            raise LiquidHandlingFailed(str(exc)) from None
        changed = {k: v for k, v in actions.items() if v != "UseMicroscriptSettings"}
        await self._send(f"SetClldErrorHandling {changed or 'all UseMicroscriptSettings'}", xml, status,
                         {"error_handling": actions})

    @sila.ObservableCommand(errors=[LiquidHandlingFailed])
    async def distribute_reagent(self, source: str, source_well: str, target: str, target_wells: list[str],
                                 volume: float, liquid_class: str, tip_type: str, channels: list[int],
                                 max_tip_reuse: int, max_multi_dispense: int, *, status: sila.Status) -> None:
        """
        Put the same volume of one reagent into many wells (FluentControl's Reagent Distribution, used by 03, 06 and
        07). FluentControl takes the tips, aspirates, dispenses and drops the tips itself. MOVES the arm.

        Args:
          Source: Labware holding the reagent, e.g. "4 Well SBS Trough[001]".
          SourceWell: Well (trough lane, tube) of the reagent, e.g. "A1".
          Target: Labware to fill, e.g. "TestPlate[001]".
          TargetWells: Wells to fill, e.g. ["A1", "B1", "C1"].
          Volume: Volume per target well in ul.
          LiquidClass: Liquid class, e.g. "Water Free Single" or "Water Free Multi" (for multi-dispense).
          TipType: e.g. "FCA, 200ul SBS".
          Channels: Channels 1-8 to use, e.g. [1, 2, 3, 4, 5, 6, 7, 8].
          MaxTipReuse: How often a tip may be used before a new one is taken (03 uses 4 and 12).
          MaxMultiDispense: Dispenses per aspiration (1 = one aspiration per well; 03 uses 12 with Water Free Multi).
        """
        from .smart_xml import reagent_distribution_xml

        self._need_run()
        self._refresh()
        channels = self._channels(channels)
        if not target_wells:
            raise LiquidHandlingFailed("Give at least one target well")
        if not (1 <= max_tip_reuse <= 96 and 1 <= max_multi_dispense <= 96):
            raise LiquidHandlingFailed("MaxTipReuse and MaxMultiDispense must be 1 to 96")
        try:
            source_type, source_geo = self._geometry(source)
            target_type, target_geo = self._geometry(target)
            extra = self._tip_and_class(tip_type, liquid_class, [volume], [target_type],
                                        min(max_multi_dispense, len(target_wells)))
            xml = reagent_distribution_xml(source, source_well, source_geo, target, target_wells, target_geo, volume,
                                           liquid_class, tip_type, channels, max_tip_reuse, max_multi_dispense)
        except (BackendError, DefinitionCheckFailed) as exc:
            raise LiquidHandlingFailed(str(exc)) from None
        extra["labware_pictures"] = {source: source_geo, target: target_geo}
        await self._send(f"ReagentDistribution {volume:g} ul {source} {source_well} -> {target} "
                         f"{len(target_wells)} wells ({liquid_class}, {tip_type})", xml, status, extra)

    @sila.ObservableCommand(errors=[LiquidHandlingFailed])
    async def transfer_samples(self, source: str, source_wells: list[str], target: str, target_wells: list[str],
                               volume: float, liquid_class: str, tip_type: str, channels: list[int], *,
                               status: sila.Status) -> None:
        """
        Move the same volume from each source well to the target well at the same place in the lists, with a new tip
        for every sample (FluentControl's Sample Transfer, used by 06). MOVES the arm.

        Args:
          Source: Labware with the samples.
          SourceWells: Source wells, e.g. ["A1", "B1"].
          Target: Labware to fill.
          TargetWells: Target wells, as many as SourceWells.
          Volume: Volume per sample in ul.
          LiquidClass: Liquid class, e.g. "Water Free Single".
          TipType: e.g. "FCA, 200ul SBS".
          Channels: Channels 1-8 to use.
        """
        from .smart_xml import sample_transfer_xml

        self._need_run()
        self._refresh()
        channels = self._channels(channels)
        if not source_wells or len(source_wells) != len(target_wells):
            raise LiquidHandlingFailed("Give as many target wells as source wells (at least one)")
        try:
            source_type, source_geo = self._geometry(source)
            target_type, target_geo = self._geometry(target)
            extra = self._tip_and_class(tip_type, liquid_class, [volume], [source_type, target_type])
            xml = sample_transfer_xml(source, source_wells, source_geo, target, target_wells, target_geo, volume,
                                      liquid_class, tip_type, channels)
        except (BackendError, DefinitionCheckFailed) as exc:
            raise LiquidHandlingFailed(str(exc)) from None
        extra["labware_pictures"] = {source: source_geo, target: target_geo}
        await self._send(f"SampleTransfer {volume:g} ul {source} -> {target} {len(source_wells)} samples "
                         f"({liquid_class}, {tip_type})", xml, status, extra)

    @sila.ObservableCommand(errors=[LiquidHandlingFailed])
    async def transfer_individual_volumes(self, source: str, target: str, source_wells: list[str],
                                          target_wells: list[str], volumes: list[float], liquid_class: str,
                                          tip_type: str, channels: list[int], max_tip_reuse: int,
                                          max_multi_dispense: int, *, status: sila.Status) -> str:
        """
        Move a different volume for each pair of wells (FluentControl's Transfer Individual Volumes, used by 04). The
        lists are written to a file in the run's record, which FluentControl reads. MOVES the arm.

        Args:
          Source: Source labware, e.g. "4 Well SBS Trough[001]".
          Target: Target labware, e.g. "TestPlate[001]".
          SourceWells: Source well of each transfer.
          TargetWells: Target well of each transfer.
          Volumes: Volume of each transfer in ul.
          LiquidClass: Liquid class, e.g. "Water Free Multi".
          TipType: e.g. "FCA, 200ul SBS".
          Channels: Channels 1-8 to use.
          MaxTipReuse: How often a tip may be used (04 uses 4 and 6).
          MaxMultiDispense: Dispenses per aspiration (04 uses 12).

        Returns:
          File: The volume list on the Fluent PC.
        """
        from .smart_xml import individual_volumes_file, individual_volumes_xml

        self._need_run()
        self._refresh()
        channels = self._channels(channels)
        if not volumes or not (len(source_wells) == len(target_wells) == len(volumes)):
            raise LiquidHandlingFailed("Give one source well, one target well and one volume per transfer")
        try:
            source_type, source_geo = self._geometry(source)
            target_type, target_geo = self._geometry(target)
            extra = self._tip_and_class(tip_type, liquid_class, list(volumes), [target_type],
                                        min(max_multi_dispense, len(volumes)))
            rows = [(well_index(s, source_geo["ycount"], source_geo["xcount"]) + 1,
                     well_index(t, target_geo["ycount"], target_geo["xcount"]) + 1, float(v))
                    for s, t, v in zip(source_wells, target_wells, volumes)]
            text = individual_volumes_file(rows)
            path = self._put_file("individual_volumes.csv", text)
            xml = individual_volumes_xml(source, source_geo, target, target_geo, path, liquid_class, tip_type,
                                         channels, max_tip_reuse, max_multi_dispense)
        except (BackendError, DefinitionCheckFailed) as exc:
            raise LiquidHandlingFailed(str(exc)) from None
        extra.update({"volume_file": path, "volume_file_text": text})
        await self._send(f"TransferIndividualVolumes {source} -> {target} {len(volumes)} transfers "
                         f"({liquid_class}, {tip_type})", xml, status, extra)
        return path

    @sila.ObservableCommand(errors=[LiquidHandlingFailed])
    async def run_worklist(self, source_labware: list[str], source_wells: list[str], target_labware: list[str],
                           target_wells: list[str], volumes: list[float], tip_type: str, liquid_class: str,
                           channels: list[int], *, status: sila.Status) -> str:
        """
        Pipette a list of transfers as a FluentControl worklist (as 02, 05 and 07 do with a CSV file chosen on the
        instrument PC). Each line moves a volume from a source well to a target well, with a new tip per line.
        The list is saved as a CSV in the run's record, imported into a worklist, loaded and executed. MOVES the arm.

        Args:
          SourceLabware: Source labware of each line, e.g. ["4 Well SBS Trough[001]", ...].
          SourceWells: Source well of each line.
          TargetLabware: Target labware of each line.
          TargetWells: Target well of each line.
          Volumes: Volume of each line in ul.
          TipType: e.g. "FCA, 200ul SBS" (02 and 07 use "FCA, 50ul SBS").
          LiquidClass: e.g. "Water Free Single".
          Channels: Channels 1-8 to use.

        Returns:
          File: The CSV on the Fluent PC.
        """
        from .smart_xml import worklist_csv, worklist_xml

        self._need_run()
        self._refresh()
        channels = self._channels(channels)
        n = len(volumes)
        if not n or not (len(source_labware) == len(source_wells) == len(target_labware) == len(target_wells) == n):
            raise LiquidHandlingFailed("Give the same number of source labware, source wells, target labware, "
                                       "target wells and volumes (at least one line)")
        try:
            types = {name: self._labware_type(name) for name in {*source_labware, *target_labware}}
            for name, w in [*zip(source_labware, source_wells), *zip(target_labware, target_wells)]:
                rows, columns = self._layout(types[name])
                well_index(w, rows, columns)  # refuses wells outside the labware
            extra = self._tip_and_class(tip_type, liquid_class, list(volumes), list(types.values()))
            text = worklist_csv(list(zip(source_labware, source_wells, target_labware, target_wells, map(float, volumes))))
            csv_path = self._put_file("worklist.csv", text)
            steps = worklist_xml(csv_path, csv_path[:-4] + ".gwl", tip_type, liquid_class, channels)
        except (BackendError, DefinitionCheckFailed) as exc:
            raise LiquidHandlingFailed(str(exc)) from None
        extra.update({"worklist_file": csv_path, "worklist_text": text, "worklist_labware": types})
        try:
            await _with_progress(status, len(steps), lambda report: self.backend.run_steps(
                steps, report, f"Worklist {n} lines ({liquid_class}, {tip_type})", extra))
        except BackendError as exc:
            raise LiquidHandlingFailed(str(exc)) from None
        return csv_path

    @sila.UnobservableProperty()
    async def get_mounted_tips(self) -> list[str]:
        """Tips mounted in the open run, e.g. "channel 1: FCA, 200ul SBS, aspirated, holds 50 ul"."""
        return [f"channel {c}: {t['type']}, {'aspirated' if t['aspirated'] else 'fresh'}, holds {t['volume']:g} ul"
                for c, t in sorted(self.backend.tips.items())]
