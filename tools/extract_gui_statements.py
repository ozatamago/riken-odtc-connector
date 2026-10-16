"""Extract pipetting, labware and gripper-finger statements from FluentControl GUI scripts (.xscr) as test fixtures.

    uv run python tools/extract_gui_statements.py <folder of .xscr files> tests/fixtures/gui_statements

Device macros are handled by extract_gui_macros.py. This tool takes the other statements that stage 4 turns into SiLA
commands (LihaGetTips, LihaAspirate, ..., AddLabware, CgaGetFingers, ...). Each top-level <Object> of a wanted type is
saved verbatim, one per distinct content (LineNumber and GroupLineNumber are set to 0 before comparing).
Disabled statements are skipped. index.tsv records the script and line of each one plus the main arguments.
"""

import pathlib
import re
import sys

from extract_gui_macros import WANT

TYPES = (
    "LihaGetTipsScriptCommandDataV3", "LihaDropTipsScriptCommandDataV2",
    "LihaAspirateScriptCommandDataV5", "LihaDispenseScriptCommandDataV6", "LihaMixScriptCommandDataV4",
    "LihaEmptyTipsScriptCommandDataV2", "LihaDetectLiquidScriptCommandDataV3",
    "LihaSetClldErrorHandlingScriptCommandDataV4", "LihaInlineFilterCheckScriptCommandDataV1",
    "LihaReagentDistributionSmartCommand", "LihaSampleTransferSmartCommand",
    "LihaTransferIndividualVolumesSmartCommand",
    "AddLabwareDataV1", "RemoveLabwareDataV1",
    "CgaGetFingersScriptCommandDataV1", "CgaDropFingersScriptCommandDataV1",
    # worklists (02, 05, 07) and variables written to / read from files (results of DetectLiquid)
    "RUPGetFileStatement", "WorklistImportStatementDataV2", "LoadWorklistStatementDataV4",
    "ExecuteWorklistStatementDataV1", "ExportVariableStatement", "ImportVariableStatement",
)
OPEN = re.compile(r'<Object Type="([^"]+)">|</Object>')


def top_level_objects(text: str):
    """Yield (type, xml) for every <Object> whose type is wanted and that is not inside another wanted object."""
    stack: list[tuple[str, int]] = []
    for m in OPEN.finditer(text):
        if m.group(1):
            stack.append((m.group(1), m.start()))
            continue
        kind, start = stack.pop()
        short = kind.rsplit(".", 1)[-1]
        if short in TYPES and not any(k.rsplit(".", 1)[-1] in TYPES for k, _ in stack):
            yield short, text[start:m.end()]


def first(tag: str, xml: str) -> str:
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", xml)
    return m.group(1) if m else ""


def summary(xml: str) -> str:
    """The main arguments, for index.tsv."""
    volumes = re.findall(r"<string>([^<]*)</string>", (re.search(r"<Volumes>(.*?)</Volumes>", xml, re.S) or [""," "])[1])
    tips = re.findall(r"<int>(\d+)</int>", (re.search(r"<SelectedTipsIndexes>(.*?)</SelectedTipsIndexes>", xml, re.S)
                                             or ["", ""])[1])
    parts = {
        "labware": first("LabwareName", xml) or first("LabwareLable", xml),
        "wells": first("SelectedWellsString", xml),
        "volumes": ",".join(volumes),
        "lc": first("LiquidClassNameBySelection", xml) or first("LiquidClassNameByExpression", xml),
        "tips": ",".join(tips),
        "diti": first("DiTiType", xml) or first("DynamicDiTiVolumeExpression", xml),
        "type": first("LabwareType", xml),
        "location": first("Location", xml) and f"{first('Location', xml)}[{first('Position', xml)}]",
    }
    return "; ".join(f"{k}={v}" for k, v in parts.items() if v)


def main(src: pathlib.Path, out: pathlib.Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    seen: dict[str, str] = {}
    counts: dict[str, int] = {}
    index = ["file\tstatement\tsummary\tsource\tline"]
    for f in sorted(src.glob("*.xscr")):
        text = f.read_text(encoding="utf-8-sig")
        name = re.search(r"<ObjectName>([^<]*)", text).group(1)
        if name not in WANT:
            continue
        for kind, obj in top_level_objects(text):
            if re.search(r"<IsDisabledForExecution>True</IsDisabledForExecution>", obj, re.I) or \
                    re.search(r'IsDisabledForExecution="true"', obj):
                continue
            line = first("LineNumber", obj) or (re.search(r'LineNumber="(\d+)"', obj) or ["", ""])[1]
            obj = re.sub(r"<(Group)?LineNumber>\d+</(Group)?LineNumber>",
                         lambda m: f"<{m.group(1) or ''}LineNumber>0</{m.group(1) or ''}LineNumber>", obj)
            obj = re.sub(r'LineNumber="\d+"', 'LineNumber="0"', obj)
            obj = "\n".join(ln.strip() for ln in obj.splitlines() if ln.strip())
            if obj in seen:
                continue
            counts[kind] = counts.get(kind, 0) + 1
            fname = f"{kind}_{counts[kind]}.xml"
            (out / fname).write_text(obj + "\n", encoding="utf-8")
            seen[obj] = fname
            index.append(f"{fname}\t{kind}\t{summary(obj)}\t{name}\t{line}")
    (out / "index.tsv").write_text("\n".join(index) + "\n", encoding="utf-8")
    print(f"Saved {len(seen)} statements to {out}")


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))
