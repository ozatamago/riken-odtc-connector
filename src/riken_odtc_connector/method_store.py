"""ODTC に登録されている温度プログラム（PreMethod / Method）の一覧。

今は 2026-08-10 16:37 に装置から取り出した MethodSet の控え（data/methodstore_20260810.xml）を読む。
装置から直接取り直す方法（GetParameters は SOAP 応答に値が返らない）が確立したら差し替える。
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
    """PreMethod は目標ブロック温度、Method は開始ブロック温度（対応する PreMethod の目標と一致させる）。"""
    lid_temperature: float
    steps: int
    estimated_seconds: float
    """Method の平坦時間の合計（ループ込み）。昇降温の時間は入っていない。PreMethod は 0。"""
    ramp_seconds: float = 0.0
    """Method の昇降温を各ステップの Slope どおりに行ったときの時間の合計。PreMethod は 0。"""


def _num(el: ET.Element | None, default: float = 0.0) -> float:
    try:
        return float(el.text) if el is not None and el.text else default
    except ValueError:
        return default


def _expand_steps(method: ET.Element) -> list[ET.Element]:
    """Step を GotoNumber / LoopNumber のループを展開して、実行される順に並べる。"""
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
    """開始ブロック温度から各ステップの平坦温度まで、Slope（degC/s）で昇降温する時間の合計。"""
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
    """Method の開始ブロック温度と目標温度が一致する PreMethod の名前。"""
    return [m.name for m in methods if m.kind == "PreMethod" and m.block_temperature == method.block_temperature]
