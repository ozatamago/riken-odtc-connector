"""Magellan が書き出す ASC ファイル（latin-1）から、96 ウェルの値を取り出す。

2026-08 に見た形は 2 通り:
- 格子形式: 先頭にラベル名の行（Label1 ...）、そのあと「<>\\t1\\t2...」の見出しと A〜H の 8 行が、ラベルの数だけ続く
  （OFML_Abs405.mth、OFML_ColorMix3.mth）
- 1 行 1 ウェル形式: 数値だけの行が 96 行、そのあとメタデータ（Abs600.mth、8/13 のメモ）
"""

from __future__ import annotations

import dataclasses
import re

ROWS = "ABCDEFGH"


@dataclasses.dataclass
class AscResult:
    labels: list[str]
    values: list[dict[str, float]]  # ラベルごとに {"A1": 0.05, ...}
    metadata: list[str]


def _num(text: str) -> float:
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        return float("nan")  # "OVER" などはそのまま値にできないので NaN にする


def parse_asc(text: str) -> AscResult:
    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    grids: list[dict[str, float]] = []
    labels: list[str] = []
    i = 0
    while i < len(lines) and lines[i] and not lines[i].startswith("<>") and not re.match(r"^\s*-?\d", lines[i]):
        labels.append(lines[i].strip())
        i += 1
    if i < len(lines) and lines[i].startswith("<>"):
        while i < len(lines) and lines[i].startswith("<>"):
            cols = [c for c in lines[i].split("\t")[1:] if c.strip()]
            i += 1
            grid: dict[str, float] = {}
            for row in ROWS:
                if i >= len(lines):
                    raise ValueError("ASC の格子が途中で終わっています")
                parts = lines[i].split("\t")
                if parts[0].strip() != row:
                    raise ValueError(f"ASC の行見出しが想定と違います: {parts[0]!r}（{row} のはず）")
                for col, value in zip(cols, parts[1:]):
                    grid[f"{row}{int(col)}"] = _num(value)
                i += 1
            grids.append(grid)
    else:
        numbers: list[float] = []
        while i < len(lines) and len(numbers) < 96 and re.match(r"^\s*-?[\d.]+", lines[i]):
            numbers.append(_num(lines[i].split("\t")[0]))
            i += 1
        if len(numbers) != 96:
            raise ValueError(f"ASC から 96 ウェルの値を読めませんでした（{len(numbers)} 個）")
        grids.append({f"{ROWS[k // 12]}{k % 12 + 1}": v for k, v in enumerate(numbers)})
    if not labels or len(labels) != len(grids):
        labels = [f"Label{k + 1}" for k in range(len(grids))]
    return AscResult(labels, grids, [ln for ln in lines[i:] if ln.strip()])
