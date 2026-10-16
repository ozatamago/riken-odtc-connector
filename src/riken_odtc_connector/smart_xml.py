"""XML of the remaining FluentControl pipetting statements of the 01-07 GUI scripts (stage 4-5).

Each builder starts from a statement of the 01-07 scripts (src/riken_odtc_connector/data/*_reference.xml, copied
from tests/fixtures/gui_statements) and only changes values, like liquid_handling.py does for GetTips ... DropTips:

| Statement | Used in | Template |
|---|---|---|
| LihaInlineFilterCheck | 02 | InlineFilterCheck_1 |
| LihaDetectLiquid | 02 | DetectLiquid_1 |
| LihaSetClldErrorHandling | 06 | SetClldErrorHandling_2 (all "UseMicroscriptSettings") |
| LihaReagentDistributionSmartCommand | 03, 06, 07 | ReagentDistribution_5 (03 line 55) |
| LihaSampleTransferSmartCommand | 06 | SampleTransfer_2 |
| LihaTransferIndividualVolumesSmartCommand | 04 | TransferIndividualVolumes_2 |
| WorklistImport, LoadWorklist, ExecuteWorklist | 02, 05, 07 | WorklistImport_1, LoadWorklist_2, ExecuteWorklist_1 |

Values that are expressions in the GUI scripts (volumes, sample counts, file names held in variables) are sent as
literals; file names are expressions, so they are sent as quoted strings ("C:\\...") as in ExportVariable.

The Smart Commands carry a picture of the source and target labware for FluentControl's editor (well counts and
spacing, the shape and size of a well, LabwarePosition). They are filled from the labware definition
(labware_geometry); LabwarePosition is kept from the template (its meaning is not known; it is not the position on
the worktable: two plates of 06 that are on the deck at the same time both have 27).
"""

import re

from .fluent_backend import BackendError
from .infinite.xml import _esc, wrap
from .liquid_handling import _set, _set_list, _template, wells_fields, well_index

WASTE = "FCA Thru Deck Waste Chute_1"

CLLD_ERRORS = {
    # SiLA parameter -> (element of the statement, error shown by FluentControl)
    "Aspirate": ("LLDErrorAction", "Liquid Level Detection Errors (Aspirate)"),
    "ContactDispense": ("DispenseLLDErrorAction", "Liquid Level Detection Errors (Contact Dispense)"),
    "AspirationSupervision": ("SupervisionErrorAction", "Aspiration supervision failed"),
    "RetractSupervision": ("RetractSupervisionErrorAction", "Retract supervision failed"),
    "PressureAspirate": ("PressureOutOfRangeAspirateErrorAction", "Pressure out of range errors during aspirate"),
    "PressureDispense": ("PressureOutOfRangeDispenseErrorAction", "Pressure out of range errors during dispense"),
    "PmpAspiration": ("PmpAspirateErrorAction", "PMP aspiration errors"),
}
CLLD_ACTIONS = ("UseMicroscriptSettings", "AspirateAvailable", "IgnoreAndContinue")
"""Actions seen in the 01-07 scripts that do not stop the run. "UserPrompt" (also seen) opens a dialog on the
instrument PC that an API run cannot answer, so it is not offered."""


def quoted(path: str) -> str:
    """A file name as a FluentControl string expression."""
    if '"' in path:
        raise BackendError(f"File name {path} contains a quote")
    return f'"{path}"'


def channel_mask(channels: list[int]) -> int:
    return sum(1 << (c - 1) for c in channels)


# ---------------------------------------------------------------- simple statements
def inline_filter_check_xml(channels: list[int]) -> str:
    xml = _template("liha_inlinefiltercheck_reference.xml")
    return wrap(_set(xml, "SelectedTips", str(channel_mask(channels))))


def detect_liquid_xml(labware: str, wells: list[str], channels: list[int], rows: int, columns: int,
                      tip_spacing: float) -> str:
    xml = _template("liha_detectliquid_reference.xml")
    serialized, shown = wells_fields(wells, rows, columns)
    if len(wells) == 1:
        shown = wells[0].strip().upper()  # 02 writes one well as "A1", not "1 * A1"
    xml = _set(xml, "SerializedWellIndexes", serialized)
    xml = _set(xml, "SelectedWellsString", shown)
    xml = _set_list(xml, "SelectedTipsIndexes", [c - 1 for c in channels], "System.Int32")
    xml = _set(xml, "TipSpacing", f"{tip_spacing:g}")
    return wrap(_set(xml, "LabwareName", labware))


def set_clld_error_handling_xml(actions: dict[str, str]) -> str:
    """actions: SiLA parameter name (CLLD_ERRORS) -> action; missing ones keep "UseMicroscriptSettings"."""
    xml = _template("liha_setclld_reference.xml")
    for key, action in actions.items():
        if key not in CLLD_ERRORS:
            raise BackendError(f"Unknown error kind {key}; kinds are {', '.join(CLLD_ERRORS)}")
        if action not in CLLD_ACTIONS:
            raise BackendError(f"{key}: action must be one of {', '.join(CLLD_ACTIONS)} (not {action})")
        element, _ = CLLD_ERRORS[key]
        new, n = re.subn(rf"(<{element}>.*?<Action>)[^<]*(</Action>)", rf"\g<1>{action}\g<2>", xml, count=1, flags=re.S)
        if n != 1:
            raise BackendError(f"Template has no {element}")
        xml = new
    return wrap(xml)


# ---------------------------------------------------------------- labware pictures for the Smart Commands
def labware_geometry(xcmp: str) -> dict:
    """Well layout and well size from a labware definition (.xcmp text, unescaped), in the form the Smart Commands
    use (checked against the 03, 04 and 06 statements: wells of 96-well plates are ellipses with the top diameter,
    trough wells are rectangles; a picture cell is the well plus 2 mm on each side unless wells are closer)."""
    def num(tag):
        m = re.search(rf"<(?:\w+:)?{tag}>([^<]+)<", xcmp)
        if not m:
            raise BackendError(f"The labware definition has no {tag}")
        return float(m.group(1))

    xcount, ycount = int(num("XNumberOfWells")), int(num("YNumberOfWells"))
    xspacing, yspacing = abs(num("XSpacing")), abs(num("YSpacing"))
    tips = int(num("TipsPerWell"))
    shapes = re.findall(r"<(?:\w+:)?CavityShape\b[^>]*>(.*?)</(?:\w+:)?CavityShape>", xcmp, re.S)
    diameter = next((float(m.group(1)) for s in shapes for m in [re.search(r"DiameterTop>([^<]+)<", s)] if m), None)
    if diameter is not None:
        kind, height, width = "Ellipse", diameter, diameter
    else:
        box = next((s for s in shapes if re.search(r"<(?:\w+:)?Width>", s) and re.search(r"<(?:\w+:)?Length>", s)), None)
        if box is None:
            raise BackendError("The labware definition has no well shape that the Smart Commands can use")
        height = float(re.search(r"Length>([^<]+)<", box).group(1))
        width = float(re.search(r"<(?:\w+:)?Width>([^<]+)<", box).group(1))
        kind = "Rectangle"
    if kind == "Rectangle" or xcount == 1:
        xspacing = width + 4
    if kind == "Rectangle" or ycount == 1:
        yspacing = height + 4
    return {"xcount": xcount, "ycount": ycount, "xspacing": round(xspacing, 3), "yspacing": round(yspacing, 3),
            "shape": kind, "height": height, "width": width, "tips_per_well": tips}


def _sub_first(block: str, tag: str, value: str) -> str:
    new, n = re.subn(rf"<{tag}>[^<]*</{tag}>", lambda _: f"<{tag}>{_esc(value)}</{tag}>", block, count=1)
    if n != 1:
        raise BackendError(f"Template section has no {tag}")
    return new


def _section(xml: str, prefix: str, labware: str, geo: dict, well_indexes: list[int] | None, shown: str | None) -> str:
    """Fill the Source / Target / EmptyTipContent labware selection of a Smart Command."""
    start = xml.find(f"<PropertyPrefix>{prefix}</PropertyPrefix>")
    if start < 0:
        raise BackendError(f"Template has no {prefix} labware")
    end = xml.find("</LiHaLabwareSelectionDataV1>", start)
    block = xml[start:end]
    block = re.sub(r"<LabwareName>[^<]*</LabwareName>", f"<LabwareName>{_esc(labware)}</LabwareName>", block)
    dim_start, dim_end = block.find("<LabwareDimension>"), block.find("</LabwareDimension>")
    dim = block[dim_start:dim_end]
    for tag, key in (("XCount", "xcount"), ("YCount", "ycount"), ("XSpacing", "xspacing"), ("YSpacing", "yspacing")):
        dim = _sub_first(dim, tag, f"{geo[key]:g}")
    block = block[:dim_start] + dim + block[dim_end:]
    cav_start, cav_end = block.find("<CavityGeometry>"), block.find("</CavityGeometry>")
    cav = block[cav_start:cav_end]
    cav = _sub_first(cav, "CavityGeometryType", geo["shape"])
    cav = _sub_first(cav, "Height", f"{geo['height']:g}")
    cav = _sub_first(cav, "Width", f"{geo['width']:g}")
    cx, cy = round(geo["xspacing"] / 2, 3), round(geo["yspacing"] / 2, 3)
    tx, ty = round((geo["xspacing"] - geo["width"]) / 2, 3), round((geo["yspacing"] - geo["height"]) / 2, 3)
    cav = re.sub(r"<Center>\s*<X>[^<]*</X>\s*<Y>[^<]*</Y>", f"<Center>\n<X>{cx:g}</X>\n<Y>{cy:g}</Y>", cav, count=1)
    cav = re.sub(r"<TopLeft>\s*<X>[^<]*</X>\s*<Y>[^<]*</Y>", f"<TopLeft>\n<X>{tx:g}</X>\n<Y>{ty:g}</Y>", cav, count=1)
    block = block[:cav_start] + cav + block[cav_end:]
    block = _sub_first(block, "TipsPerWell", str(geo["tips_per_well"]))
    if well_indexes is not None:
        items = "".join(f"<int>{i}</int>\n" for i in well_indexes)
        block, n = re.subn(r"<SelectedWellIndexes>.*?</SelectedWellIndexes>|<SelectedWellIndexes />",
                           f"<SelectedWellIndexes>\n{items}</SelectedWellIndexes>" if well_indexes else "<SelectedWellIndexes />",
                           block, count=1, flags=re.S)
        if n != 1:
            raise BackendError(f"Template {prefix} labware has no SelectedWellIndexes")
    if shown is not None and "<SelectedWellsString>" in block:
        block = _sub_first(block, "SelectedWellsString", shown)
    return xml[:start] + block + xml[end:]


def _common(xml: str, tip_type: str, liquid_class: str, channels: list[int], reuse: int, multi: int) -> str:
    xml = re.sub(r"TOOLNAME:[^<]*<", lambda _: f"TOOLNAME:{_esc(tip_type)}<", xml, count=1)
    xml = _set(xml, "LiquidClassNameBySelection", liquid_class)
    xml = _set(xml, "LiquidClassName", liquid_class)
    xml = _set(xml, "MaxNumberOfDiTiReuse", str(int(reuse)))
    xml = _set(xml, "MaxNumberOfMultiDispense", str(int(multi)))
    xml = _set(xml, "DitiWaste", WASTE)
    xml = _set(xml, "UseDynamicDiTiHandling" if "<UseDynamicDiTiHandling>" in xml else "IsDynamicDiTiHandling", "False")
    return _set_list(xml, "SelectedTipsIndexes", [c - 1 for c in channels], "System.Int32")


def _indexes(wells: list[str], geo: dict) -> list[int]:
    return [well_index(w, geo["ycount"], geo["xcount"]) for w in wells]


def reagent_distribution_xml(source: str, source_well: str, source_geo: dict, target: str, target_wells: list[str],
                             target_geo: dict, volume: float, liquid_class: str, tip_type: str, channels: list[int],
                             reuse: int, multi: int) -> str:
    """Distribute volume from one source well (a trough lane, a tube) into each target well."""
    xml = _template("liha_reagentdistribution_reference.xml")
    xml = _common(xml, tip_type, liquid_class, channels, reuse, multi)
    src = _indexes([source_well], source_geo)
    xml = _section(xml, "Source", source, source_geo, src, wells_fields([source_well], source_geo["ycount"], source_geo["xcount"])[1].split(" * ")[-1])
    xml = _section(xml, "EmptyTipContent", source, source_geo, None, None)
    xml = _section(xml, "Target", target, target_geo, _indexes(target_wells, target_geo),
                   wells_fields(target_wells, target_geo["ycount"], target_geo["xcount"])[1])
    xml = _set(xml, "TargetVolumePerWell", f"{volume:g}")
    xml = _set(xml, "SampleCount", str(len(target_wells)))
    return wrap(xml)


def sample_transfer_xml(source: str, source_wells: list[str], source_geo: dict, target: str, target_wells: list[str],
                        target_geo: dict, volume: float, liquid_class: str, tip_type: str, channels: list[int]) -> str:
    """Move volume from each source well to the target well in the same place of the list (one tip per sample)."""
    xml = _template("liha_sampletransfer_reference.xml")
    xml = _common(xml, tip_type, liquid_class, channels, 1, 1)
    xml = _section(xml, "Source", source, source_geo, _indexes(source_wells, source_geo),
                   wells_fields(source_wells, source_geo["ycount"], source_geo["xcount"])[1])
    xml = _section(xml, "Target", target, target_geo, _indexes(target_wells, target_geo),
                   wells_fields(target_wells, target_geo["ycount"], target_geo["xcount"])[1])
    xml = _set(xml, "TargetVolumePerWell", f"{volume:g}")
    xml = _set(xml, "SampleCount", str(len(source_wells)))
    return wrap(_set(xml, "Pooling", "False"))


def individual_volumes_file(rows: list[tuple[int, int, float]]) -> str:
    """The instruction file of TransferIndividualVolumes, as in 04 (C:\\FluentData\\04_...\\Medium1.csv): source well
    index; target well index; volume, wells numbered from 1 in FluentControl's order (down the columns)."""
    return "Source well index;Target well index;Volume [ul]\r\n" + "".join(f"{s};{t};{v:g}\r\n" for s, t, v in rows)


def individual_volumes_xml(source: str, source_geo: dict, target: str, target_geo: dict, file_path: str,
                           liquid_class: str, tip_type: str, channels: list[int], reuse: int, multi: int) -> str:
    xml = _template("liha_transferindividualvolumes_reference.xml")
    xml = _common(xml, tip_type, liquid_class, channels, reuse, multi)
    xml = _section(xml, "Source", source, source_geo, None, None)
    xml = _section(xml, "Target", target, target_geo, None, None)  # left-over tip content goes to the waste chute
    return wrap(_set(xml, "PipettingInstructionFilePath", quoted(file_path)))


# ---------------------------------------------------------------- worklists (02, 05, 07)
WORKLIST_COLUMNS = ("SourceLabel", "SourcePosition", "DestLabel", "DestPosition", "Volume")


def worklist_csv(rows: list[tuple[str, str, str, str, float]]) -> str:
    """The CSV that 02 and 07 import (C:\\FluentData\\02_PCR\\PickPrimerList.csv): a header line, then source label,
    source well, target label, target well, volume."""
    for r in rows:
        if any("," in str(x) for x in r):
            raise BackendError(f"Worklist values must not contain commas: {r}")
    return "SourceLabel,SourcePosition,DestLabel,DestPosition,Volume\r\n" + \
        "".join(f"{a},{b},{c},{d},{v:g}\r\n" for a, b, c, d, v in rows)


def worklist_xml(csv_path: str, gwl_path: str, tip_type: str, liquid_class: str, channels: list[int]) -> list[tuple[str, str]]:
    """The three statements 02 and 07 use: import the CSV into a worklist file, load it, execute it."""
    imp = _template("worklist_import_reference.xml")
    imp = _set(imp, "CsvExpressionOrFilename", quoted(csv_path))
    imp = _set(imp, "GwlExpressionOrFilename", quoted(gwl_path))
    imp = _set(imp, "StopWithLine", "1")
    load = _template("worklist_load_reference.xml")
    load = _set(load, "WorklistPath", quoted(gwl_path))
    load = re.sub(r"TOOLNAME:[^<]*<", lambda _: f"TOOLNAME:{_esc(tip_type)}<", load, count=1)
    load = re.sub(r"(<LiquidClassName>)[^<]*(</LiquidClassName>)(\s*<IsLiquidClassNameByExpressionEnabled>False)",
                  lambda m: m.group(1) + _esc(liquid_class) + m.group(2) + m.group(3), load, count=1)
    load = _set_list(load, "SelectedTips", [c - 1 for c in channels], "System.Int32")
    return [("WorklistImport", wrap(imp)), ("LoadWorklist", wrap(load)),
            ("ExecuteWorklist", wrap(_template("worklist_execute_reference.xml")))]
