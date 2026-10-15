"""Fluent 780 の Infinite 200 PRO 用に、FluentControl へ送る XML を組み立てる。

どれも実機で成功した XML と同じ形にしてある（tests/ で 1 文字ずつ比べる）。
- Magellan_Open / Magellan_Close / Magellan_Measure: 2026-08-13 に GenericCommand で成功、10-09 にも同じ形で送った
- RGA1_TransferLabware: 2026-10-09 に ODTC への搬送などで成功（liha_ref から写した雛形）
- AddLabware / RemoveLabware: GUI のスクリプト 02_PCR_Ver1_0 の文の形
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
    """FluentControl の文 1 つを ScriptGroup で包む（build_liha_xml.wrap と同じ）。"""
    body = "\n".join("    " + ln for ln in obj_xml.strip().split("\n"))
    return ("<ScriptGroup>\n  <Objects>\n" + body + "\n  </Objects>\n"
            "  <Name></Name>\n"
            "  <IsBreakpoint>False</IsBreakpoint>\n"
            "  <IsDisabledForExecution>False</IsDisabledForExecution>\n"
            "  <LineNumber>0</LineNumber>\n</ScriptGroup>")


def magellan_xml(name: str, settings: str | None = None) -> str:
    """Magellan のマクロ。Measure の settings は「出力先,測定ファイル.mth,0,0,0」。"""
    if name not in MAGELLAN_MACROS:
        raise BackendError(f"{name} は許可していない Magellan のマクロです")
    if (name == "Magellan_Measure") != (settings is not None):
        raise BackendError(f"{name} の引数の指定が正しくありません")
    if settings is not None and ("<" in settings or ">" in settings):
        # 2026-08-13: <YYMMDD-NNN> の連番パターンを入れると GenericCommand が壊れた
        raise BackendError("Magellan_Measure の引数に < > は使えません（固定のファイル名にする）")
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
    """RGA1_TransferLabware。雛形の値を 3 段エスケープして差し替える（qpcr_water_demo.rga と同じ）。

    実物の <BASE> は &amp;amp;lt;BASE&amp;amp;gt; と保存されている
    = 内側 XML のテキスト（1 段）→ 文字列値（2 段）→ ScriptGroup の XML（3 段）。
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
            raise BackendError(f"RGA の雛形に {tag} がありません")
        src = new
    src = re.sub(r'LineNumber="\d+"', 'LineNumber="0"', src, count=1)
    return wrap(src)


def add_labware_xml(labware_type: str, label: str, location: str, position: int) -> str:
    """FluentControl の記憶にラボウェアを登録する（装置は動かない）。02_PCR_Ver1_0 の文の形。"""
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
    """FluentControl の記憶からラボウェアを消す（装置は動かない）。02_PCR_Ver1_0 の文の形。"""
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
