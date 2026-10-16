"""What FluentControl actually did, read from its log (C:\\ProgramData\\Tecan\\LoggingServer\\LogFiles).

A Smart Command (Reagent Distribution, Sample Transfer, Transfer Individual Volumes) or a worklist is expanded by
FluentControl into ordinary statements ("Smart Subroutine ...: Line 2 : Get Tips (FCA 1)"), and the API returns
nothing about them. The log has, per expanded line, the labware, the well indexes, the volume per channel, the tip
position taken and the volume really aspirated (with the liquid class's corrections). expanded_steps() turns these
log entries into one dict per line (2026-10-16: checked against Reagent Distribution, Sample Transfer, Transfer
Individual Volumes and a worklist on the instrument).
"""

import re

LOG_PS1 = r"""
$d = 'C:\ProgramData\Tecan\LoggingServer\LogFiles'
# the log switches to a new file every few tens of minutes; read the current one and the one before it
$files = Get-ChildItem "$d\*.ulf","$d\*.zlf" | Sort-Object LastWriteTime -Descending | Select-Object -First 2
Add-Type -AssemblyName System.IO.Compression.FileSystem
$out = foreach ($f in $files) {
  if ($f.Extension -eq '.zlf') { $z = [System.IO.Compression.ZipFile]::OpenRead($f.FullName); $t = (New-Object System.IO.StreamReader($z.Entries[0].Open())).ReadToEnd(); $z.Dispose() }
  else { $fs = [System.IO.File]::Open($f.FullName,'Open','Read','ReadWrite'); $t = (New-Object System.IO.StreamReader($fs)).ReadToEnd(); $fs.Close() }
  foreach ($m in [regex]::Matches($t, '<Log [^>]*TimeStamp="([0-9-]+ [0-9:.]+)"[^>]*Message="([^"]{0,2500})')) {
    $msg = $m.Groups[2].Value -replace '&#xD;&#xA;',' ' -replace '&amp;','&' -replace '&lt;','<' -replace '&gt;','>' -replace '&quot;','"' -replace '\s+',' '
    if ($m.Groups[1].Value -ge '__SINCE__' -and $msg -match 'Line \d+ : |Execution parameters for|Variable: aspirationVolume Scope|detected volume|: Filter ') {
      $m.Groups[1].Value + "`t" + $msg
    }
  }
}
$out | Sort-Object -Unique
"""

STEP = re.compile(r":Line (\d+) : (.+?) \((?:FCA|LiHa|LIHA)[^)]*\)\s*$")
PARAMS = re.compile(r"Execution parameters for '([^']+)' at line (\d+): (.*)")
ASPIRATED = re.compile(r"Variable: aspirationVolume Scope: channel(\d+)\|.*? Value: ([0-9.]+)")


def well_name(index: int, rows: int) -> str:
    """FluentControl's well index (0, down the columns) as a name like "B3"."""
    return f"{chr(ord('A') + index % rows)}{index // rows + 1}"


def expanded_steps(log_text: str) -> list[dict]:
    """One dict per expanded line, in order: line, action, labware, well_indexes, volumes ({channel: ul}),
    tip_rack / tip_index (Get Tips), aspirated ({channel: ul}, Aspirate)."""
    steps: list[dict] = []
    current: dict | None = None
    for raw in log_text.splitlines():
        if "\t" not in raw:
            continue
        stamp, msg = raw.split("\t", 1)
        m = STEP.search(msg)
        if m:
            current = {"time": stamp, "line": int(m.group(1)), "action": m.group(2)}
            steps.append(current)
            continue
        if current is None:
            continue
        m = PARAMS.search(msg)
        if m and m.group(1) == current["action"]:
            body = m.group(3)
            lab = re.search(r"(?:^| )Labware: (.+?) Tip Selection", body)
            if lab:
                current["labware"] = lab.group(1)
            wells = re.search(r"Selection of Wells: ([0-9, ]+?) Well Offset", body)
            if wells:
                current["well_indexes"] = [int(x) for x in re.findall(r"\d+", wells.group(1))]
            vols = re.search(r"Volumes: (.*?)(?:Liquid Classes:|X offset|$)", body)
            if vols:
                current["volumes"] = {int(c) + 1: float(v) for c, v in re.findall(r"\((\d+), ([0-9.]+)\)", vols.group(1))
                                      if float(v) > 0}
            lc = re.search(r"Liquid class: (.+?) (?:Volumes:|$)", body)
            if lc:
                current["liquid_class"] = lc.group(1)
            if current["action"] == "Get Tips":
                rack = re.search(r"ModifiedLabware: (.+?) Modified", body)
                tip = re.search(r"ModifiedSelectedWellIndexes: ([0-9, ]+?) Modified", body)
                if rack:
                    current["tip_rack"] = rack.group(1)
                if tip:
                    current["tip_indexes"] = [int(x) for x in re.findall(r"\d+", tip.group(1))]
            continue
        m = ASPIRATED.search(msg)
        if m and current["action"] == "Aspirate":
            current.setdefault("aspirated", {})[int(m.group(1))] = round(float(m.group(2)), 2)
    return steps


def describe(steps: list[dict], rows_of) -> list[str]:
    """Readable lines, e.g. "3 Aspirate 4 Well SBS Trough[001] A1: 90 ul (takes up 116.97 ul)". rows_of(labware)
    gives the rows of a labware (None if unknown: indexes are then shown as numbers)."""
    def wells(labware, indexes):
        rows = rows_of(labware) if labware else None
        return ",".join(well_name(i, rows) if rows else f"#{i}" for i in indexes)

    out = []
    for s in steps:
        text = f"{s['line']} {s['action']}"
        if s["action"] == "Get Tips" and s.get("tip_rack"):
            text += f" from {s['tip_rack']} {wells(s['tip_rack'], s.get('tip_indexes', []))}"
        elif s.get("labware"):
            text += f" {s['labware']}"
            if s.get("well_indexes") and "Drop Tips" not in s["action"]:
                text += f" {wells(s['labware'], s['well_indexes'])}"
        if s.get("volumes"):
            text += ": " + ", ".join(f"{v:g} ul" + (f" (ch{c})" if len(s["volumes"]) > 1 else "")
                                     for c, v in sorted(s["volumes"].items()))
        if s.get("aspirated"):
            text += " (takes up " + ", ".join(f"{v:g}" for _, v in sorted(s["aspirated"].items())) + " ul)"
        out.append(text)
    return out
