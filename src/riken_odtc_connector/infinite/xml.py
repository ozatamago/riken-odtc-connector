"""Build the XML sent to FluentControl for the Infinite 200 PRO in the Fluent 780.

Each matches XML that succeeded on the device (compared character by character in tests/).
- Magellan_Open / Magellan_Close / Magellan_Measure: succeeded via GenericCommand on 2026-08-13, sent in the
  same form again on 10-09
- RGA1_TransferLabware: succeeded on 2026-10-09 for transfers to the ODTC etc. (template copied from liha_ref)
- AddLabware / RemoveLabware: statement form from the GUI script 02_PCR_Ver1_0
"""

from __future__ import annotations

import pathlib
import re

from ..fluent_backend import BackendError

RGA_TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "data" / "rga_transfer_reference.xml"
MAGELLAN_MACROS = {"Magellan_Open", "Magellan_Close", "Magellan_Measure"}


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def wrap(obj_xml: str) -> str:
    """Wrap one FluentControl statement in a ScriptGroup (same as build_liha_xml.wrap)."""
    body = "\n".join("    " + ln for ln in obj_xml.strip().split("\n"))
    return ("<ScriptGroup>\n  <Objects>\n" + body + "\n  </Objects>\n"
            "  <Name></Name>\n"
            "  <IsBreakpoint>False</IsBreakpoint>\n"
            "  <IsDisabledForExecution>False</IsDisabledForExecution>\n"
            "  <LineNumber>0</LineNumber>\n</ScriptGroup>")


def magellan_xml(name: str, settings: str | None = None) -> str:
    """A Magellan macro. Measure settings are "<output path>,<method>.mth,0,0,0"."""
    if name not in MAGELLAN_MACROS:
        raise BackendError(f"{name} is not an allowed Magellan macro")
    if (name == "Magellan_Measure") != (settings is not None):
        raise BackendError(f"Invalid arguments for {name}")
    if settings is not None and ("<" in settings or ">" in settings):
        # 2026-08-13: a <YYMMDD-NNN> sequence pattern broke GenericCommand
        raise BackendError("Magellan_Measure arguments must not contain < > (use a fixed file name)")
    es = "<ExecutionSettings />" if settings is None else f"<ExecutionSettings>{_esc(settings)}</ExecutionSettings>"
    return wrap('<Object Type="Tecan.VisionX.ApplicationDriver.LegacyDriverMacro">\n'
                f'  <LegacyDriverMacro Version="1" Name="{name}" ModuleName="Magellan"'
                ' ExecutionTime="PT2S" IsBreakpoint="false"'
                ' IsDisabledForExecution="false" LineNumber="0">\n'
                f'    {es}\n'
                '  </LegacyDriverMacro>\n'
                '</Object>')


def rga_xml(labware: str, location: str = "<BASE>", site: str = "",
            move_to_base: bool = False, fixed_site: bool = False) -> str:
    """RGA1_TransferLabware. Replace the template values with triple-escaped text (same as qpcr_water_demo.rga).

    The real <BASE> is stored as &amp;amp;lt;BASE&amp;amp;gt;
    = inner XML text (level 1) -> string value (level 2) -> ScriptGroup XML (level 3).
    """
    src = RGA_TEMPLATE.read_text(encoding="utf-8").strip()
    fields = {
        "FixedSite": "true" if fixed_site else "false",
        "Labware": labware,
        "Location": location,
        "MoveToBase": "true" if move_to_base else "false",
        "Site": site,
    }
    for tag, value in fields.items():
        pattern = rf"(&amp;lt;{tag}&amp;gt;).*?(&amp;lt;/{tag}&amp;gt;)"
        new, count = re.subn(pattern, lambda m: m.group(1) + _esc(_esc(_esc(value))) + m.group(2), src, count=1)
        if count != 1:
            raise BackendError(f"RGA template has no {tag}")
        src = new
    src = re.sub(r'LineNumber="\d+"', 'LineNumber="0"', src, count=1)
    return wrap(src)


def add_labware_xml(labware_type: str, label: str, location: str, position: int) -> str:
    """Register labware in FluentControl's worktable memory (no device motion). Statement form from 02_PCR_Ver1_0."""
    return wrap('<Object Type="Tecan.Core.Scripting.Worktable.Data.AddLabwareDataV1">\n'
                "  <AddLabwareDataV1>\n"
                f"    <LabwareType>{_esc(labware_type)}</LabwareType>\n"
                f"    <LabwareLable>{_esc(label)}</LabwareLable>\n"
                f"    <Location>{_esc(location)}</Location>\n"
                f"    <Position>{int(position)}</Position>\n"
                "    <Rotation>0</Rotation>\n"
                "    <HasLid>False</HasLid>\n"
                '    <Data Type="Tecan.Core.Scripting.Programming.ProgrammingStatementBaseDataV1">\n'
                "      <ProgrammingStatementBaseDataV1>\n"
                "        <IsBreakpoint>False</IsBreakpoint>\n"
                "        <IsDisabledForExecution>False</IsDisabledForExecution>\n"
                "        <LineNumber>0</LineNumber>\n"
                "      </ProgrammingStatementBaseDataV1>\n"
                "    </Data>\n"
                "  </AddLabwareDataV1>\n"
                "</Object>")


def remove_labware_xml(label: str) -> str:
    """Remove labware from FluentControl's worktable memory (no device motion). Statement form from 02_PCR_Ver1_0."""
    return wrap('<Object Type="Tecan.Core.Scripting.Worktable.Data.RemoveLabwareDataV1">\n'
                "  <RemoveLabwareDataV1>\n"
                f"    <LabwareName>{_esc(label)}</LabwareName>\n"
                '    <Data Type="Tecan.Core.Scripting.Programming.ProgrammingStatementBaseDataV1">\n'
                "      <ProgrammingStatementBaseDataV1>\n"
                "        <IsBreakpoint>False</IsBreakpoint>\n"
                "        <IsDisabledForExecution>False</IsDisabledForExecution>\n"
                "        <LineNumber>0</LineNumber>\n"
                "      </ProgrammingStatementBaseDataV1>\n"
                "    </Data>\n"
                "  </RemoveLabwareDataV1>\n"
                "</Object>")
