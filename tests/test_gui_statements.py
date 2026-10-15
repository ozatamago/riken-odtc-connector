"""Check that the pipetting XML verified on the instrument in August 2026 has the same structure as the statements
in the 01-07 GUI scripts, so the stage-4 commands can fill in the GUI arguments without guessing the format."""

import pathlib
import re

import pytest

FIX = pathlib.Path(__file__).parent / "fixtures"
GUI = FIX / "gui_statements"
VERIFIED = FIX / "liha_verified"


def skeleton(xml: str) -> list[str]:
    """Tag names in order, without values. List items (volumes, tips, liquid classes: one per tip) are values too and
    are dropped, as is the outer ScriptGroup. LiquidClassVariables* are ignored (their position differs between the two
    sources)."""
    body = re.sub(r"^\s*<ScriptGroup>\s*<Objects>|</Objects>.*</ScriptGroup>\s*$", "", xml, flags=re.S)
    body = re.sub(r'<Object Type="System\.(?:String|Int32)">.*?</Object>', "", body, flags=re.S)
    return [t for t in re.findall(r"<(/?[A-Za-z0-9_.]+)", body) if "LiquidClassVariables" not in t]


PAIRS = [
    ("LihaGetTipsScriptCommandDataV3_5.xml", "gettips_1ch.xml"),
    ("LihaGetTipsScriptCommandDataV3_5.xml", "gettips_8ch_reference.xml"),
    ("LihaAspirateScriptCommandDataV5_20.xml", "aspirate_1ch.xml"),
    ("LihaAspirateScriptCommandDataV5_20.xml", "aspirate_8ch_reference.xml"),
    ("LihaDispenseScriptCommandDataV6_13.xml", "dispense_1ch.xml"),
    ("LihaDispenseScriptCommandDataV6_13.xml", "dispense_8ch_reference.xml"),
    ("LihaMixScriptCommandDataV4_1.xml", "mix_1ch.xml"),
]


@pytest.mark.parametrize(("gui", "verified"), PAIRS)
def test_verified_xml_has_the_gui_statement_structure(gui, verified):
    assert skeleton((GUI / gui).read_text(encoding="utf-8")) == skeleton((VERIFIED / verified).read_text(encoding="utf-8"))


def test_every_gui_statement_is_indexed():
    files = {p.name for p in GUI.glob("*.xml")}
    indexed = {ln.split("\t")[0] for ln in (GUI / "index.tsv").read_text(encoding="utf-8").splitlines()[1:]}
    assert files == indexed
