"""Extract the 96 well values from the ASC files (latin-1) written by Magellan.

Two layouts seen as of 2026-08:
- Grid layout: label name lines first (Label1 ...), then one block per label of a "<>\\t1\\t2..." header
  followed by the 8 rows A-H (OFML_Abs405.mth, OFML_ColorMix3.mth)
- One-well-per-line layout: 96 lines of plain numbers, then metadata (Abs600.mth, notes from 08-13)
"""

from __future__ import annotations

import dataclasses
import re

ROWS = "ABCDEFGH"


@dataclasses.dataclass
class AscResult:
    labels: list[str]
    values: list[dict[str, float]]  # one {"A1": 0.05, ...} per label
    metadata: list[str]


def _num(text: str) -> float:
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        return float("nan")  # values such as "OVER" are not numbers, so use NaN


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
                    raise ValueError("ASC grid ends prematurely")
                parts = lines[i].split("\t")
                if parts[0].strip() != row:
                    raise ValueError(f"Unexpected ASC row header: {parts[0]!r} (expected {row})")
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
            raise ValueError(f"Could not read 96 well values from the ASC file (got {len(numbers)})")
        grids.append({f"{ROWS[k // 12]}{k % 12 + 1}": v for k, v in enumerate(numbers)})
    if not labels or len(labels) != len(grids):
        labels = [f"Label{k + 1}" for k in range(len(grids))]
    return AscResult(labels, grids, [ln for ln in lines[i:] if ln.strip()])
