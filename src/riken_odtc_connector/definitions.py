"""Check and record every FluentControl definition an operation uses, before it is sent.

Rule of the lab (2026-10-16): before anything runs on the Fluent, confirm the definitions and parameters of everything
it uses (liquid classes, tips, labware, locations, the worktable, arms, device macros), save them, then run. This makes
accidents less likely and errors easier to trace.

How it works
------------
1. A local mirror of the definition folders of the FluentControl database (``C:\\ProgramData\\Tecan\\VisionX\\DataBase``)
   is kept in ``<record_dir>/database``. Before each operation only changed files are copied over SSH (by size and
   modification time).
2. The names used by the XML about to be sent are extracted (``extract_references``) and looked up in the mirror.
   Missing names, locations that the worktable does not have, and liquid classes without an Air FCA part (the arm of
   this instrument) refuse the operation.
3. The SHA-256 of every definition file used is compared with the accepted baseline (``<record_dir>/baseline.json``).
   If a file changed since it was last accepted, the operation is refused and the change is listed. After checking,
   ``accept_pending_changes`` accepts it and the operation can be sent again. Files seen for the first time are
   accepted and noted in the record.
4. A record folder ``<record_dir>/runs/<time>_<operation>/`` gets the XML sent, ``manifest.json`` (each name, the file
   it resolved to, its hash and key values), copies of the definition files, and ``result.json`` when it finishes.
"""

import dataclasses
import datetime
import hashlib
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from unitelabs.cdk.config import Config

REMOTE_ROOT = r"C:\ProgramData\Tecan\VisionX\DataBase"
REMOTE_WORK = r"C:\Users\Tecan\OFML_screen\definition_sync"
# Folders of the database that hold definitions (meshes, textures, connectors and undo history are not needed)
FOLDERS = (
    r"SystemSpecific\LiquidClasses",
    r"SystemSpecific\Worktable\Components",
    r"SystemSpecific\Worktable\Sites",
    r"SystemSpecific\Worktable\Workspaces",
    r"SystemSpecific\Worktable\CustomAttributes",
    r"SystemSpecific\Configuration",
    r"SystemSpecific\DriverConfiguration",
    "UserSpecific",
)
ARM_LIQUID_CLASS_PART = "AirFca"
"""The pipetting arm of this instrument is Air FCA 1, so only the AirFca part of a liquid class applies."""


class DefinitionCheckFailed(Exception):
    """The operation was refused before sending: a definition is missing, does not fit, or changed."""


@dataclasses.dataclass
class DefinitionsConfig(Config):
    """Where definitions are mirrored and records are kept."""

    enabled: bool = True
    record_dir: str = "~/.riken_fluent780"
    workspace: str = "WT2411004293_Ver.0.5_OFML"
    """Worktable used by OFML_ApiHost. Locations are checked against it."""
    ssh_target: str = "Tecan@10.5.1.156"


# ---------------------------------------------------------------- names used by an XML command
def _unescape(xml: str) -> str:
    # RGA macro settings are escaped twice inside the ScriptGroup; unescape until stable
    for _ in range(4):
        new = html.unescape(xml)
        if new == xml:
            break
        xml = new
    return xml


def extract_references(xml: str) -> dict[str, list]:
    """Names an XML command uses, by kind. Values are sorted and unique (pairs for locations and macros)."""
    t = _unescape(xml)
    refs: dict[str, set] = {k: set() for k in (
        "liquid_classes", "auxiliary_liquid_classes", "tips", "labware_types", "labware", "locations",
        "macros", "arms")}
    for tag in ("LiquidClassNameBySelection", "LiquidClassName"):
        refs["liquid_classes"].update(v for v in re.findall(rf"<{tag}>([^<]+)</{tag}>", t) if v.strip())
    for block in re.findall(r"<LiquidClassNames>(.*?)</LiquidClassNames>", t, re.S):
        refs["liquid_classes"].update(v for v in re.findall(r"<string>([^<]+)</string>", block) if v.strip())
    for tag in ("EmptyTipContentLiquidClassNameBySelection", "EmptyTipsLiquidClassNameBySelection",
                "WashLiquidClassNameBySelection"):
        refs["auxiliary_liquid_classes"].update(v for v in re.findall(rf"<{tag}>([^<]+)</{tag}>", t) if v.strip())
    refs["tips"].update(m.strip() for m in re.findall(r"TOOLNAME:([^<]+)<", t))
    refs["labware_types"].update(re.findall(r"<LabwareType>([^<]+)</LabwareType>", t))
    for tag in ("LabwareName", "LabwareLable", "Labware"):
        refs["labware"].update(v for v in re.findall(rf"<{tag}>([^<]+)</{tag}>", t) if v.strip())
    # RGA: <Location>..<Site>; AddLabware: <Location>..<Position>
    for m in re.finditer(r"<Location>([^<]*)</Location>", t):
        after = t[m.end():m.end() + 600]
        site = re.search(r"<(?:Site|Position)>([^<]*)</(?:Site|Position)>", after)
        refs["locations"].add((m.group(1), site.group(1) if site else ""))
    for m in re.finditer(r'<(LegacyDriverMacro|ApplicationDriverMacro) [^>]*Name="([^"]+)" ModuleName="([^"]+)"', t):
        refs["macros"].add((m.group(3), m.group(2)))
        if m.group(2).startswith("RGA"):
            refs["arms"].add("RGA 1")
    if re.search(r'<Object Type="[^"]*\.Li[Hh]a', t):
        refs["arms"].add("Air FCA 1")
    return {k: sorted(v) for k, v in refs.items()}


# ---------------------------------------------------------------- local mirror of the database
def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _object_name(path: pathlib.Path) -> str:
    with open(path, "rb") as f:
        head = f.read(4096).decode("utf-8", errors="replace")
    m = re.search(r"<ObjectName>([^<]*)</ObjectName>", head)
    return m.group(1).strip() if m else ""


class DatabaseMirror:
    """Copy of the definition folders of the FluentControl database, refreshed over SSH."""

    def __init__(self, root: pathlib.Path, ssh_target: str):
        self.root = root
        self.ssh_target = ssh_target
        self.files = root / "files"
        self.index_path = root / "index.json"
        self.index: dict[str, dict] = json.loads(self.index_path.read_text()) if self.index_path.exists() else {}

    # -- remote helpers
    def _ssh(self, command: str, timeout: int = 300) -> str:
        out = subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", self.ssh_target, command],
                             capture_output=True, timeout=timeout)
        if out.returncode != 0:
            raise DefinitionCheckFailed(f"SSH to the Fluent PC failed: {out.stderr.decode(errors='replace')[:300]}")
        return out.stdout.decode("utf-8", errors="replace").replace("\r", "")

    def _ps_file(self, script: str, timeout: int = 300) -> str:
        """Copy a PowerShell script to the Fluent PC and run it (avoids quoting problems)."""
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as f:
            f.write(script)
            local = f.name
        try:
            self._scp_to(local, REMOTE_WORK + r"\run.ps1")
            return self._ssh(f'powershell -NoProfile -ExecutionPolicy Bypass -File "{REMOTE_WORK}\\run.ps1"', timeout)
        finally:
            os.unlink(local)

    def _scp_to(self, local: str, remote: str) -> None:
        self._ssh(f'powershell -NoProfile -Command "New-Item -ItemType Directory -Force \'{REMOTE_WORK}\' | Out-Null"')
        out = subprocess.run(["scp", "-q", local, f"{self.ssh_target}:{remote.replace(chr(92), '/')}"],
                             capture_output=True, timeout=300)
        if out.returncode != 0:
            raise DefinitionCheckFailed(f"Copy to the Fluent PC failed: {out.stderr.decode(errors='replace')[:300]}")

    def _scp_from(self, remote: str, local: str) -> None:
        out = subprocess.run(["scp", "-q", f"{self.ssh_target}:{remote.replace(chr(92), '/')}", local],
                             capture_output=True, timeout=600)
        if out.returncode != 0:
            raise DefinitionCheckFailed(f"Copy from the Fluent PC failed: {out.stderr.decode(errors='replace')[:300]}")

    def refresh(self) -> list[str]:
        """Bring the mirror up to date. Returns the relative paths that were (re)copied."""
        folders = ",".join(f"'{REMOTE_ROOT}\\{f}'" for f in FOLDERS)
        listing = self._ps_file(
            f"$r = '{REMOTE_ROOT}'\n"
            f"Get-ChildItem {folders} -Recurse -File | ForEach-Object {{ "
            "$_.FullName.Substring($r.Length + 1) + \"`t\" + $_.Length + \"`t\" + $_.LastWriteTimeUtc.Ticks }\n")
        remote = {}
        for line in listing.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                remote[parts[0]] = (int(parts[1]), parts[2])
        changed = [p for p, (size, ticks) in remote.items()
                   if self.index.get(p, {}).get("size") != size or self.index.get(p, {}).get("ticks") != ticks]
        removed = [p for p in self.index if p not in remote]
        if changed:
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8-sig") as f:
                f.write("\n".join(changed))
                listfile = f.name
            try:
                self._scp_to(listfile, REMOTE_WORK + r"\changed.txt")
            finally:
                os.unlink(listfile)
            self._ps_file(
                f"$r = '{REMOTE_ROOT}'; $w = '{REMOTE_WORK}'\n"
                "Remove-Item -Recurse -Force \"$w\\pack\", \"$w\\pack.zip\" -ErrorAction SilentlyContinue\n"
                "foreach ($p in Get-Content \"$w\\changed.txt\") {\n"
                "  $d = Join-Path \"$w\\pack\" $p; New-Item -ItemType Directory -Force (Split-Path $d) | Out-Null\n"
                "  Copy-Item -LiteralPath (Join-Path $r $p) -Destination $d }\n"
                "Compress-Archive -Path \"$w\\pack\\*\" -DestinationPath \"$w\\pack.zip\" -Force\n", timeout=900)
            with tempfile.TemporaryDirectory() as tmp:
                zpath = os.path.join(tmp, "pack.zip")
                self._scp_from(REMOTE_WORK + r"\pack.zip", zpath)
                with zipfile.ZipFile(zpath) as z:
                    for info in z.infolist():
                        rel = info.filename.replace("\\", "/")
                        if rel.endswith("/"):
                            continue
                        dest = self.files / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(info) as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)
        for p in changed:
            local = self.files / p.replace("\\", "/")
            size, ticks = remote[p]
            self.index[p] = {"size": size, "ticks": ticks, "sha256": _sha256(local), "name": _object_name(local)}
        for p in removed:
            self.index.pop(p, None)
            (self.files / p.replace("\\", "/")).unlink(missing_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(self.index, ensure_ascii=False, indent=1))
        return changed

    # -- lookups
    def local(self, rel: str) -> pathlib.Path:
        return self.files / rel.replace("\\", "/")

    def find(self, name: str, ext: str) -> str | None:
        """Relative path of the definition with this object name and extension (e.g. ".xlqc"), or None."""
        hits = [p for p, v in self.index.items() if p.lower().endswith(ext) and v.get("name") == name]
        return sorted(hits)[0] if hits else None


# ---------------------------------------------------------------- worktable
C = "{http://schemas.datacontract.org/2004/07/Tecan.VisionX.Worktable.Core}"
A = "{http://schemas.microsoft.com/2003/10/Serialization/Arrays}"


def parse_workspace(mirror: DatabaseMirror, rel: str) -> tuple[dict[str, int], set[str]]:
    """Location groups of a worktable with their number of sites, and the labels of the labware placed on it."""
    site_group: dict[str, str] = {}
    for p in mirror.index:
        if p.lower().endswith(".xsit"):
            text = mirror.local(p).read_text(encoding="utf-8-sig", errors="replace")
            m = re.search(r"<LocationGroupName>([^<]*)</LocationGroupName>", text)
            site_group[pathlib.PureWindowsPath(p).stem] = m.group(1) if m else ""
    root = ET.parse(mirror.local(rel)).getroot()
    groups: dict[str, int] = {}
    labels: set[str] = set()

    def walk(comp):
        lab = comp.find(C + "LabwareName/" + A + "KeyValueOfstringstring/" + A + "Value")
        if lab is not None and lab.text:
            labels.add(lab.text)
        arrs = comp.find(C + "Arrangements")
        for arr in (arrs.findall(C + "Arrangement") if arrs is not None else []):
            sites = arr.find(C + "Sites")
            for kv in (list(sites) if sites is not None else []):
                val = kv.find(A + "Value")
                g = site_group.get(val.findtext(C + "BaseTemplateGuid") or "", "")
                if g:
                    groups[g] = groups.get(g, 0) + 1
                cc = val.find(C + "ConnectedComponent")
                if cc is not None and len(cc):
                    walk(cc)

    walk(root.find(".//" + C + "Workspace").find(".//" + C + "Frame"))
    return groups, labels


def liquid_class_has_part(path: pathlib.Path, part: str) -> bool:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return f"<PipettingDeviceType>{part}</PipettingDeviceType>" in text


def _tip_values(path: pathlib.Path) -> dict[str, str]:
    u = _unescape(path.read_text(encoding="utf-8-sig", errors="replace"))
    kv = dict(re.findall(r"<(?:\w+:)?Key>([^<]*)</(?:\w+:)?Key>\s*<(?:\w+:)?Value[^>]*>(?:\s*<[^>]+>)*([^<]{0,80})", u))
    keep = ("DiTiTypeGroup", "TipCapacity", "Filter", "MaxNettoVolumeForTransferPhase", "ZTravel", "ZStart",
            "ZMax", "ZBottom", "MaxImmersionDepth")
    return {k: kv[k].strip() for k in keep if k in kv}


# ---------------------------------------------------------------- check and record
class Preflight:
    """Checks the definitions an operation uses and writes the run record."""

    def __init__(self, config: DefinitionsConfig, dry_run: bool):
        self.config = config
        self.dry_run = dry_run
        self.dir = pathlib.Path(os.path.expanduser(config.record_dir))
        self.mirror = DatabaseMirror(self.dir / "database", config.ssh_target)
        self.baseline_path = self.dir / "baseline.json"
        self.pending: dict[str, dict] = {}
        """Changes found by the last refused check: relative path -> {name, accepted, now}."""
        self.last_record = ""

    def _baseline(self) -> dict[str, str]:
        return json.loads(self.baseline_path.read_text()) if self.baseline_path.exists() else {}

    def accept_pending_changes(self) -> list[str]:
        """Accept the changed definitions listed by the last refusal. Returns their names."""
        base = self._baseline()
        names = []
        for rel, change in self.pending.items():
            base[rel] = change["now"]
            names.append(f"{change['name']} ({rel})")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.baseline_path.write_text(json.dumps(base, ensure_ascii=False, indent=1))
        self.pending = {}
        return names

    def begin(self, steps: list[tuple[str, str]], what: str, parent: pathlib.Path | None = None) -> "RunRecord":
        """Check everything the steps use and create the record. Raises DefinitionCheckFailed to refuse."""
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", what)[:60].strip("_") or "operation"
        folder = (parent or self.dir / "runs") / f"{stamp}_{slug}"
        record = RunRecord(folder)
        record.write_steps(steps)
        merged: dict[str, set] = {}
        for _, xml in steps:
            for kind, values in extract_references(xml).items():
                merged.setdefault(kind, set()).update(tuple(v) if isinstance(v, (list, tuple)) else v for v in values)
        refs = {k: sorted(v) for k, v in merged.items()}
        manifest: dict = {"operation": what, "time": stamp, "dry_run": self.dry_run, "references": refs,
                          "definitions": [], "warnings": [], "first_seen": []}
        if not self.config.enabled:
            manifest["warnings"].append("definition check disabled in the configuration")
            record.write_manifest(manifest)
            return record
        if self.dry_run and not self.mirror.index:
            manifest["warnings"].append("dry run without a definition mirror: names recorded, not checked")
            record.write_manifest(manifest)
            return record
        if not self.dry_run:
            manifest["mirror_refreshed"] = len(self.mirror.refresh())

        problems: list[str] = []
        used: dict[str, str] = {}  # relative path -> name

        def need(name: str, ext: str, kind: str) -> str | None:
            rel = self.mirror.find(name, ext)
            if rel is None:
                problems.append(f"{kind} '{name}' is not defined in FluentControl")
            else:
                used[rel] = name
            return rel

        ws_rel = need(self.config.workspace, ".xwsp", "Worktable")
        groups, placed = parse_workspace(self.mirror, ws_rel) if ws_rel else ({}, set())
        for lc in refs.get("liquid_classes", []):
            rel = need(lc, ".xlqc", "Liquid class")
            if rel and not liquid_class_has_part(self.mirror.local(rel), ARM_LIQUID_CLASS_PART):
                problems.append(f"Liquid class '{lc}' has no {ARM_LIQUID_CLASS_PART} part (the arm of this instrument)")
        for lc in refs.get("auxiliary_liquid_classes", []):
            rel = self.mirror.find(lc, ".xlqc")
            if rel:
                used[rel] = lc
        for tip in refs.get("tips", []):
            need(tip, ".xcmp", "Tip type")
        for lt in refs.get("labware_types", []):
            need(lt, ".xcmp", "Labware type")
        for loc, site in refs.get("locations", []):
            if loc.startswith(("<", "GetCover")) or not loc:
                continue
            if loc not in groups:
                problems.append(f"Location '{loc}' is not on worktable {self.config.workspace}")
            elif site.isdigit() and not 1 <= int(site) <= groups[loc]:
                problems.append(f"{loc}[{site}] does not exist ({loc} has {groups[loc]} sites)")
        for lab in refs.get("labware", []):
            if lab not in placed and not lab.startswith("FCA Thru Deck Waste"):
                manifest["warnings"].append(f"Labware '{lab}' is not on the worktable template (added at run time?)")
        arm_files = {"RGA 1": ("RGA 1", ".xdrv"), "Air FCA 1": ("Fca", ".xcfg")}
        for arm in refs.get("arms", []):
            name, ext = arm_files[arm]
            need(name, ext, f"Arm setting for {arm}")
        method_rel = self.mirror.find("OFML_ApiHost", ".xmth")
        if method_rel:
            used[method_rel] = "OFML_ApiHost"

        # compare with the accepted baseline
        base = self._baseline()
        changes = {}
        for rel, name in sorted(used.items()):
            entry = self.mirror.index[rel]
            info = {"name": name, "file": rel, "sha256": entry["sha256"], "size": entry["size"]}
            if rel.lower().endswith(".xcmp") and name in refs.get("tips", []):
                info["values"] = _tip_values(self.mirror.local(rel))
            manifest["definitions"].append(info)
            if rel not in base:
                base[rel] = entry["sha256"]
                manifest["first_seen"].append(f"{name} ({rel})")
            elif base[rel] != entry["sha256"]:
                changes[rel] = {"name": name, "accepted": base[rel], "now": entry["sha256"]}
            record.copy_definition(self.mirror.local(rel), rel)
        manifest["problems"] = problems
        manifest["changed_since_accepted"] = changes
        record.write_manifest(manifest)
        self.last_record = str(folder)
        if problems:
            record.finish({"status": "refused", "reason": problems})
            raise DefinitionCheckFailed("Refused before sending: " + "; ".join(problems) + f" (record: {folder})")
        if changes:
            self.pending = changes
            record.finish({"status": "refused", "reason": "definitions changed", "changes": changes})
            names = ", ".join(f"{c['name']} ({r})" for r, c in changes.items())
            raise DefinitionCheckFailed(
                f"Refused before sending: these definitions changed since they were last accepted: {names}. "
                f"Check them, then call AcceptDefinitionChanges and send again (record: {folder})")
        if manifest["first_seen"]:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.baseline_path.write_text(json.dumps(base, ensure_ascii=False, indent=1))
        return record


class RunRecord:
    """One folder per operation: the XML sent, the definitions used, and the result."""

    def __init__(self, folder: pathlib.Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def write_steps(self, steps: list[tuple[str, str]]) -> None:
        d = self.folder / "steps"
        d.mkdir(exist_ok=True)
        for i, (label, xml) in enumerate(steps, 1):
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:60]
            (d / f"{i:02d}_{safe}.xml").write_text(xml, encoding="utf-8")

    def copy_definition(self, src: pathlib.Path, rel: str) -> None:
        dest = self.folder / "definitions" / rel.replace("\\", "/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def write_manifest(self, manifest: dict) -> None:
        (self.folder / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1, default=list), encoding="utf-8")

    def finish(self, result: dict) -> None:
        result = {"finished": datetime.datetime.now().isoformat(timespec="seconds"), **result}
        (self.folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
