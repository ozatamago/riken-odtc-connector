"""List of the temperature programs (PreMethod / Method) stored on the ODTC.

For now this reads a copy of the MethodSet exported from the device on 2026-08-10 16:37 (data/methodstore_20260810.xml).
Replace it once it can be fetched directly from the device (GetParameters returns no values in its SOAP response).
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import xml.etree.ElementTree as ET

SNAPSHOT = pathlib.Path(__file__).parent / "data" / "methodstore_20260810.xml"
SNAPSHOT_TAKEN = "2026-08-10T16:37"


@dataclasses.dataclass(frozen=True)
class OdtcMethod:
    name: str
    kind: str  # "PreMethod" | "Method"
    block_temperature: float
    """Target block temperature for a PreMethod; start block temperature for a Method (= target of its PreMethod)."""
    lid_temperature: float
    steps: int
    estimated_seconds: float
    """Total hold time of a Method (loops included), excluding ramps. 0 for a PreMethod."""
    ramp_seconds: float = 0.0
    """Total ramp time of a Method if each step ramps at its Slope. 0 for a PreMethod."""


def _num(el: ET.Element | None, default: float = 0.0) -> float:
    try:
        return float(el.text) if el is not None and el.text else default
    except ValueError:
        return default


def _expand_steps(method: ET.Element) -> list[ET.Element]:
    """Expand the GotoNumber / LoopNumber loops and list the Steps in execution order."""
    steps = method.findall("Step")
    by_number = {int(_num(s.find("Number"))): s for s in steps}
    order: list[ET.Element] = []
    i, loops, guard = 1, {}, 0
    while i in by_number and guard < 10_000:
        guard += 1
        s = by_number[i]
        order.append(s)
        goto, count = int(_num(s.find("GotoNumber"))), int(_num(s.find("LoopNumber")))
        if goto and count:
            done = loops.get(i, 0)
            if done < count:
                loops[i] = done + 1
                i = goto
                continue
            loops[i] = 0
        i += 1
    return order


def _plateau_seconds(method: ET.Element) -> float:
    return sum(_num(s.find("PlateauTime")) for s in _expand_steps(method))


def _ramp_seconds(method: ET.Element) -> float:
    """Total time to ramp at each Slope (degC/s) from the start block temperature to each step's hold temperature."""
    total, temp = 0.0, _num(method.find("StartBlockTemperature"))
    for s in _expand_steps(method):
        target, slope = _num(s.find("PlateauTemperature")), _num(s.find("Slope"), 1.0) or 1.0
        total += abs(target - temp) / slope
        temp = target
    return total


def load_methods(path: pathlib.Path = SNAPSHOT) -> list[OdtcMethod]:
    text = path.read_text(encoding="utf-8")
    root = ET.fromstring(re.sub(r"^<\?xml[^>]*\?>", "", text.strip()))
    out: list[OdtcMethod] = []
    for el in root:
        if el.tag == "PreMethod":
            out.append(OdtcMethod(el.get("methodName", ""), "PreMethod", _num(el.find("TargetBlockTemperature")),
                                  _num(el.find("TargetLidTemp")), 0, 0.0))
        elif el.tag == "Method":
            out.append(OdtcMethod(el.get("methodName", ""), "Method", _num(el.find("StartBlockTemperature")),
                                  _num(el.find("StartLidTemperature")), len(el.findall("Step")), _plateau_seconds(el),
                                  _ramp_seconds(el)))
    return out


def premethods_for(method: OdtcMethod, methods: list[OdtcMethod]) -> list[str]:
    """Names of the PreMethods whose target temperature equals the Method's start block temperature."""
    return [m.name for m in methods if m.kind == "PreMethod" and m.block_temperature == method.block_temperature]
