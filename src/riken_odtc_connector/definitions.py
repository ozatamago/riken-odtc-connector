"""Check and record every FluentControl definition an operation uses, before it is sent.

Rule of the lab (2026-10-16): before anything runs on the Fluent, confirm the definitions and parameters of everything
it uses (liquid classes, tips, labware, locations, the worktable, arms, device macros), save them, then run. This makes
accidents less likely and errors easier to trace.

Everything is kept on the Fluent PC under ``C:\\Users\\Tecan\\OFML`` (the computer running the connector has little
space, and the lab wants the records next to the instrument):

- ``OFML\\definitions\\index.json``: one entry per definition file of the FluentControl database (size, time, SHA-256,
  object name; the location name of sites; whether a liquid class has an Air FCA part). Only changed files are hashed.
- ``OFML\\definitions\\baseline.json``: the accepted hash of each definition file.
- ``OFML\\definitions\\versions\\<sha256>.<ext>``: every version of a definition file that an operation used, stored
  once. FluentControl overwrites a definition in place when it is edited, so this is the only way to see later what
  a liquid class or worktable looked like when an operation ran.
- ``OFML\\records\\runs\\<time>_<operation>\\``: the XML sent (``steps``), ``manifest.json`` (each name used, the file it
  resolved to, its hash, where that version is stored, key values) and ``result.json``. No copies of definitions.

Before each operation the index is brought up to date, the names the XML uses are looked up, and the operation is
refused if a name is missing, a location or site is not on the worktable, a liquid class has no Air FCA part (the arm
of this instrument), or a definition file changed since it was last accepted (``accept_pending_changes`` after
checking). Files seen for the first time are accepted and noted in the record.

Records are sent to the Fluent PC in the background, so a slow or hanging SSH connection never holds up a command
(2026-10-16: an SSH call hung for 300 s after a plate had already been put down, and the command looked frozen).
A finished record waits in ``~/.riken_fluent780/pending`` until it has been copied, and is then removed there.
Dry-run records stay in ``~/.riken_fluent780/dry-run`` and are deleted as soon as a real operation has finished.
"""

import dataclasses
import datetime
import html
import json
import os
import pathlib
import re
import shutil
import tempfile
import threading
import xml.etree.ElementTree as ET

from unitelabs.cdk.config import Config

from . import remote

REMOTE_DB = r"C:\ProgramData\Tecan\VisionX\DataBase"
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
    """Checking of definitions and where records are kept."""

    enabled: bool = True
    workspace: str = "WT2411004293_Ver.0.5_OFML"
    """Worktable used by OFML_ApiHost. Locations are checked against it."""
    runtime_workspace: str = "RuntimeWorkspace"
    """The worktable FluentControl actually runs on (it starts from the method's worktable and keeps labware added in
    earlier runs, e.g. 4 Well SBS Trough[001] at Nest7mm_Pos[5]). Read for the labware already registered; it changes
    with every run, so it is recorded but not compared with the baseline."""
    ssh_target: str = "Tecan@10.5.1.156"
    remote_dir: str = r"C:\Users\Tecan\OFML"
    """Folder on the Fluent PC that holds the definition index, the baseline and the records."""
    local_dir: str = "~/.riken_fluent780"
    """Only for dry-run records and records whose copy to the Fluent PC failed."""


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
    for tag in ("LabwareName", "LabwareLable", "Labware", "DitiWaste", "WashWasteName"):
        # worklist statements write names as string expressions ("FCA Thru Deck Waste Chute_1")
        refs["labware"].update(v.strip().strip('"') for v in re.findall(rf"<{tag}>([^<]+)</{tag}>", t) if v.strip())
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
    if re.search(r'<Object Type="[^"]*\.Rga\.Scripting\.', t):  # CgaGetFingers / CgaDropFingers
        refs["arms"].add("RGA 1")
    return {k: sorted(v) for k, v in refs.items()}


# ---------------------------------------------------------------- the Fluent PC side
INDEX_PS1 = r"""
$ErrorActionPreference = 'Stop'
$db = '__DB__'; $dir = '__DIR__\definitions'; $idx = "$dir\index.json"
New-Item -ItemType Directory -Force $dir | Out-Null
$old = @{}
if (Test-Path $idx) { foreach ($e in (Get-Content $idx -Raw -Encoding UTF8 | ConvertFrom-Json)) { $old[$e.path] = $e } }
$out = New-Object System.Collections.ArrayList
foreach ($f in Get-ChildItem __FOLDERS__ -Recurse -File) {
  $rel = $f.FullName.Substring($db.Length + 1); $ticks = [string]$f.LastWriteTimeUtc.Ticks
  $e = $old[$rel]
  if (-not $e -or $e.size -ne $f.Length -or $e.ticks -ne $ticks) {
    $head = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($f.FullName))
    $m = [regex]::Match($head, '<ObjectName>([^<]*)</ObjectName>')
    $g = [regex]::Match($head, '<LocationGroupName>([^<]*)</LocationGroupName>')
    $e = [pscustomobject]@{ path = $rel; size = $f.Length; ticks = $ticks
      sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $f.FullName).Hash.ToLower()
      name = $m.Groups[1].Value.Trim(); location = $g.Groups[1].Value
      airfca = $head.Contains('<PipettingDeviceType>AirFca</PipettingDeviceType>') }
  }
  [void]$out.Add($e)
}
$json = ConvertTo-Json -InputObject @($out) -Compress -Depth 3
[System.IO.File]::WriteAllText($idx, $json, (New-Object System.Text.UTF8Encoding $false))
$json
"""


class FluentPc:
    """Access to the definitions and records on the Fluent PC (over SSH, or directly when ssh_target is "local")."""

    STORE_TIMEOUT = 60
    """Seconds for each SSH/scp call while storing a record (it is retried later if it fails)."""

    def __init__(self, config: DefinitionsConfig):
        self.config = config
        self.root = config.remote_dir.rstrip("\\")

    def _ssh(self, command: str, timeout: int = 300) -> bytes:
        out = remote.run(self.config.ssh_target, command, timeout)
        if out.returncode != 0:
            raise DefinitionCheckFailed(f"Command on the Fluent PC failed: {out.stderr.decode(errors='replace')[:300]}")
        return out.stdout

    def _scp(self, src: str, dest: str, recursive: bool = False, timeout: int = 600) -> None:
        out = remote.copy(src, dest, recursive, timeout, self.config.ssh_target)
        if out.returncode != 0:
            raise DefinitionCheckFailed(f"Copy to/from the Fluent PC failed: {out.stderr.decode(errors='replace')[:300]}")

    def _remote(self, path: str) -> str:
        return remote.spec(self.config.ssh_target, path)

    def run_ps(self, script: str, timeout: int = 600) -> bytes:
        """Copy a PowerShell script into OFML\\work and run it (avoids quoting problems)."""
        name = f"run_{os.getpid()}_{threading.get_ident()}.ps1"  # the background uploader may run at the same time
        self._ssh(f'powershell -NoProfile -Command "New-Item -ItemType Directory -Force \'{self.root}\\work\' | Out-Null"',
                  min(timeout, 300))
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as f:
            f.write(script)
            local = f.name
        try:
            self._scp(local, self._remote(self.root + "\\work\\" + name), timeout=min(timeout, 300))
        finally:
            os.unlink(local)
        return self._ssh(f'powershell -NoProfile -ExecutionPolicy Bypass -File "{self.root}\\work\\{name}"', timeout)

    def index(self) -> dict[str, dict]:
        folders = ",".join(f"'{REMOTE_DB}\\{f}'" for f in FOLDERS)
        script = INDEX_PS1.replace("__DB__", REMOTE_DB).replace("__DIR__", self.root).replace("__FOLDERS__", folders)
        entries = json.loads(self.run_ps(script).decode("utf-8-sig"))
        return {e["path"]: e for e in entries}

    def read(self, rel: str) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "f")
            self._scp(self._remote(f"{REMOTE_DB}\\{rel}"), dest)
            return pathlib.Path(dest).read_bytes()

    def read_baseline(self) -> dict[str, str]:
        out = self._ssh(f'powershell -NoProfile -Command "$p=\'{self.root}\\definitions\\baseline.json\'; '
                        'if (Test-Path $p) { Get-Content $p -Raw -Encoding UTF8 } else { \'{}\' }"')
        return json.loads(out.decode("utf-8-sig") or "{}")

    def write_baseline(self, baseline: dict[str, str]) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(baseline, f, ensure_ascii=False, indent=1)
            local = f.name
        try:
            self._ssh(f'powershell -NoProfile -Command "New-Item -ItemType Directory -Force \'{self.root}\\definitions\' | Out-Null"')
            self._scp(local, self._remote(self.root + r"\definitions\baseline.json"))
        finally:
            os.unlink(local)

    def read_file(self, path: str) -> str | None:
        """Text of a file on the Fluent PC, or None if it does not exist."""
        out = self._ssh(f'powershell -NoProfile -Command "if (Test-Path \'{path}\') {{ \'1\'; Get-Content \'{path}\' -Raw -Encoding UTF8 }} else {{ \'0\' }}"')
        text = out.decode("utf-8-sig").replace("\r", "")
        return text[2:] if text.startswith("1") else None

    def write_file(self, path: str, text: str) -> None:
        folder = path.rsplit("\\", 1)[0]
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write(text)
            local = f.name
        try:
            self._ssh(f'powershell -NoProfile -Command "New-Item -ItemType Directory -Force \'{folder}\' | Out-Null"')
            self._scp(local, self._remote(path))
        finally:
            os.unlink(local)

    def store_record(self, local: pathlib.Path, remote_rel: str, definitions: list) -> None:
        """Copy a local record folder (or file) to OFML\\records\\<remote_rel>, and keep each definition version used
        (pairs of relative path and SHA-256) in OFML\\definitions\\versions once."""
        target = f"{self.root}\\records\\{remote_rel}"
        parent = target.rsplit("\\", 1)[0]
        t = self.STORE_TIMEOUT
        self._ssh(f'powershell -NoProfile -Command "New-Item -ItemType Directory -Force \'{parent}\' | Out-Null"', t)
        self._scp(str(local), self._remote(parent) + "/", recursive=local.is_dir(), timeout=t)
        if definitions:
            # copy to a temporary name, check the hash, then keep it under the hash it really has
            lines = "\n".join(
                f"$v = '{self.root}\\definitions\\versions\\{sha}{pathlib.PureWindowsPath(rel).suffix}'; "
                f"if (-not (Test-Path $v)) {{ $tmp = $v + '.tmp'; Copy-Item -LiteralPath '{REMOTE_DB}\\{rel}' -Destination $tmp; "
                f"$h = (Get-FileHash -Algorithm SHA256 -LiteralPath $tmp).Hash.ToLower(); "
                f"if ($h -eq '{sha}') {{ Move-Item $tmp $v }} else {{ Move-Item -Force $tmp ($v -replace '{sha}', $h); "
                f"'changed while copying: {rel}' }} }}" for rel, sha in definitions)
            self.run_ps("$ErrorActionPreference = 'Stop'\n"
                        f"New-Item -ItemType Directory -Force '{self.root}\\definitions\\versions' | Out-Null\n" + lines + "\n",
                        timeout=t)


# ---------------------------------------------------------------- worktable
C = "{http://schemas.datacontract.org/2004/07/Tecan.VisionX.Worktable.Core}"
A = "{http://schemas.microsoft.com/2003/10/Serialization/Arrays}"


def parse_workspace(xml: bytes, site_location: dict[str, str]) -> tuple[dict[str, int], set[str]]:
    """Location groups of a worktable with their number of sites, and the labels of the labware placed on it.
    site_location maps a site template GUID to its location name."""
    root = ET.fromstring(xml)
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
                g = site_location.get(val.findtext(C + "BaseTemplateGuid") or "", "")
                if g:
                    groups[g] = groups.get(g, 0) + 1
                cc = val.find(C + "ConnectedComponent")
                if cc is not None and len(cc):
                    walk(cc)

    walk(next(e for e in root.iter() if e.tag == C + "Frame"))
    return groups, labels


def workspace_labware(xml: bytes, site_location: dict[str, str], component_name: dict[str, str]) -> dict[str, dict]:
    """Labware placed on a worktable: label -> {"type", "location", "site"} (site counted from 1 per location)."""
    root = ET.fromstring(xml)
    counts: dict[str, int] = {}
    out: dict[str, dict] = {}

    def walk(comp):
        arrs = comp.find(C + "Arrangements")
        for arr in (arrs.findall(C + "Arrangement") if arrs is not None else []):
            sites = arr.find(C + "Sites")
            for kv in (list(sites) if sites is not None else []):
                val = kv.find(A + "Value")
                g = site_location.get(val.findtext(C + "BaseTemplateGuid") or "", "")
                if g:
                    counts[g] = counts.get(g, 0) + 1
                cc = val.find(C + "ConnectedComponent")
                if cc is not None and len(cc):
                    lab = cc.find(C + "LabwareName/" + A + "KeyValueOfstringstring/" + A + "Value")
                    if lab is not None and lab.text and g:
                        out[lab.text] = {"type": component_name.get(cc.findtext(C + "CarrierOrLabwareTemplateGUID") or "", ""),
                                         "location": g, "site": counts[g]}
                    walk(cc)

    walk(next(e for e in root.iter() if e.tag == C + "Frame"))
    return out


def tip_values(xml: bytes) -> dict[str, str]:
    u = _unescape(xml.decode("utf-8-sig", errors="replace"))
    kv = dict(re.findall(r"<(?:\w+:)?Key>([^<]*)</(?:\w+:)?Key>\s*<(?:\w+:)?Value[^>]*>(?:\s*<[^>]+>)*([^<]{0,80})", u))
    keep = ("DiTiTypeGroup", "TipCapacity", "Filter", "MaxNettoVolumeForTransferPhase", "ZTravel", "ZStart",
            "ZMax", "ZBottom", "MaxImmersionDepth")
    return {k: kv[k].strip() for k in keep if k in kv}


# ---------------------------------------------------------------- check and record
class Preflight:
    """Checks the definitions an operation uses and keeps the record on the Fluent PC."""

    def __init__(self, config: DefinitionsConfig, dry_run: bool, pc: FluentPc | None = None, background: bool = True):
        self.config = config
        self.background = background
        """Send records to the Fluent PC in a background thread (False: send before returning, used by tests)."""
        self._upload_lock = threading.Lock()
        self._upload_wanted = threading.Event()
        self.dry_run = dry_run
        self.pc = pc or FluentPc(config)
        self.local = pathlib.Path(os.path.expanduser(config.local_dir))
        self.pending: dict[str, dict] = {}
        """Changes found by the last refused check: relative path -> {name, accepted, now}."""
        self.last_record = ""
        self._workspace_cache: tuple[str, tuple] | None = None

    # ---- where records go
    def location(self, remote_rel: str) -> str:
        """Human-readable place of a record."""
        if self.dry_run:
            return str(self.local / "dry-run" / remote_rel.replace("\\", "/"))
        return f"{self.config.remote_dir}\\records\\{remote_rel}"

    def store(self, local: pathlib.Path, remote_rel: str, definitions: list) -> None:
        """Move a finished local record to the Fluent PC. Never raises: on failure it waits in pending/."""
        if self.dry_run:
            dest = self.local / "dry-run" / remote_rel.replace("\\", "/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if local.is_dir():
                shutil.copytree(remote.long_path(local), remote.long_path(dest), dirs_exist_ok=True)
            else:
                shutil.copy2(remote.long_path(local), remote.long_path(dest))
            return
        stash = self.local / "pending" / remote_rel.replace("\\", "/")
        stash.parent.mkdir(parents=True, exist_ok=True)
        if local.is_dir():
            shutil.copytree(remote.long_path(local), remote.long_path(stash), dirs_exist_ok=True)
        else:
            shutil.copy2(remote.long_path(local), remote.long_path(stash))
        (stash.parent / (stash.name + ".pending.json")).write_text(
            json.dumps({"remote_rel": remote_rel, "definitions": definitions}))
        self.store_pending()

    def store_pending(self) -> None:
        """Send the waiting records to the Fluent PC (in the background unless background=False)."""
        if self.dry_run or not (self.local / "pending").exists():
            return
        if not self.background:
            self._upload_all()
            return
        self._upload_wanted.set()
        if self._upload_lock.acquire(blocking=False):
            threading.Thread(target=self._uploader, name="record-upload", daemon=True).start()

    def _uploader(self) -> None:
        try:
            while self._upload_wanted.is_set():
                self._upload_wanted.clear()
                self._upload_all()
        finally:
            self._upload_lock.release()

    def wait_for_uploads(self, timeout: float = 120) -> bool:
        """Wait until the background uploader is idle. True if it finished in time."""
        if self._upload_lock.acquire(timeout=timeout):
            self._upload_lock.release()
            return True
        return False

    def _upload_all(self) -> None:
        root = self.local / "pending"
        for meta in sorted(root.rglob("*.pending.json")):
            info = json.loads(meta.read_text())
            item = meta.with_name(meta.name[: -len(".pending.json")])
            try:
                self.pc.store_record(item, info["remote_rel"], info["definitions"])
            except Exception as exc:  # noqa: BLE001 - the operation itself is over; try again next time
                info["error"] = f"{datetime.datetime.now().isoformat(timespec='seconds')} {str(exc)[:300]}"
                meta.write_text(json.dumps(info))
                continue
            if item.is_dir():
                shutil.rmtree(remote.long_path(item), ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            meta.unlink()
        # leave nothing behind on this computer once everything is on the Fluent PC
        for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True) + [root]:
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
        if self.local.exists() and not any(self.local.iterdir()):
            self.local.rmdir()

    def forget_dry_runs(self) -> None:
        """Dry-run records are only kept until a real operation has run."""
        shutil.rmtree(remote.long_path(self.local / "dry-run"), ignore_errors=True)

    # ---- baseline
    def accept_pending_changes(self) -> list[str]:
        """Accept the changed definitions listed by the last refusal. Returns their names."""
        base = self.pc.read_baseline()
        names = []
        for rel, change in self.pending.items():
            base[rel] = change["now"]
            names.append(f"{change['name']} ({rel})")
        if not self.dry_run:
            self.pc.write_baseline(base)
        self.pending = {}
        return names

    # ---- the check
    def runtime_labware(self, index: dict[str, dict], pc=None) -> dict[str, dict]:
        """Labware registered on FluentControl's RuntimeWorkspace: label -> {"type", "location", "site"}."""
        pc = pc or self.pc
        hits = sorted(p for p, e in index.items() if p.lower().endswith(".xwsp") and e.get("name") == self.config.runtime_workspace)
        if not hits:
            return {}
        key = ("runtime", hits[0], index[hits[0]]["sha256"])
        if getattr(self, "_runtime_cache", (None,))[0] != key:
            sites = {pathlib.PureWindowsPath(p).stem: e.get("location", "") for p, e in index.items() if p.lower().endswith(".xsit")}
            comps = {pathlib.PureWindowsPath(p).stem: e.get("name", "") for p, e in index.items() if p.lower().endswith(".xcmp")}
            self._runtime_cache = (key, workspace_labware(pc.read(hits[0]), sites, comps))
        return self._runtime_cache[1]

    def _workspace(self, index: dict[str, dict], rel: str) -> tuple[dict[str, int], set[str]]:
        sha = index[rel]["sha256"]
        if self._workspace_cache and self._workspace_cache[0] == sha:
            return self._workspace_cache[1]
        sites = {pathlib.PureWindowsPath(p).stem: e.get("location", "")
                 for p, e in index.items() if p.lower().endswith(".xsit")}
        result = parse_workspace(self.pc.read(rel), sites)
        self._workspace_cache = (sha, result)
        return result

    def begin(self, steps: list[tuple[str, str]], what: str, session: str = "", extra: dict | None = None) -> "RunRecord":
        """Check everything the steps use and start the record. Raises DefinitionCheckFailed to refuse.

        session is the relative folder of an open run (StartRun); the record goes inside it."""
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", what)[:60].strip("_") or "operation"
        remote_rel = (session + "\\" if session else "runs\\") + f"{stamp}_{slug}"
        record = RunRecord(self, remote_rel)
        record.write_steps(steps)
        merged: dict[str, set] = {}
        for _, xml in steps:
            for kind, values in extract_references(xml).items():
                merged.setdefault(kind, set()).update(tuple(v) if isinstance(v, (list, tuple)) else v for v in values)
        refs = {k: sorted(v) for k, v in merged.items()}
        manifest: dict = {"operation": what, "time": stamp, "dry_run": self.dry_run, "references": refs,
                          "definitions": [], "warnings": [], "first_seen": [], **(extra or {})}
        self.last_record = self.location(remote_rel)
        if not self.config.enabled:
            manifest["warnings"].append("definition check disabled in the configuration")
            record.write_manifest(manifest)
            return record
        self.store_pending()
        try:
            index = self.pc.index()
            base = self.pc.read_baseline()
        except DefinitionCheckFailed as exc:
            if not self.dry_run:
                record.discard()
                raise
            manifest["warnings"].append(f"dry run: definitions not checked ({exc})")
            record.write_manifest(manifest)
            return record

        def find(name: str, ext: str) -> str | None:
            hits = sorted(p for p, e in index.items() if p.lower().endswith(ext) and e.get("name") == name)
            return hits[0] if hits else None

        problems: list[str] = []
        used: dict[str, str] = {}

        def need(name: str, ext: str, kind: str) -> str | None:
            rel = find(name, ext)
            if rel is None:
                problems.append(f"{kind} '{name}' is not defined in FluentControl")
            else:
                used[rel] = name
            return rel

        ws_rel = need(self.config.workspace, ".xwsp", "Worktable")
        groups, placed = self._workspace(index, ws_rel) if ws_rel else ({}, set())
        runtime = self.runtime_labware(index)
        placed = set(placed) | set(runtime)
        rt_rel = next((p for p, e in index.items() if p.lower().endswith(".xwsp") and e.get("name") == self.config.runtime_workspace), None)
        if rt_rel:
            manifest["runtime_workspace"] = {"file": rt_rel, "sha256": index[rt_rel]["sha256"],
                                             "labware": {k: v for k, v in sorted(runtime.items()) if k in refs.get("labware", [])}}
        for lc in refs.get("liquid_classes", []):
            rel = need(lc, ".xlqc", "Liquid class")
            if rel and not index[rel].get("airfca"):
                problems.append(f"Liquid class '{lc}' has no {ARM_LIQUID_CLASS_PART} part (the arm of this instrument)")
        for lc in refs.get("auxiliary_liquid_classes", []):
            rel = find(lc, ".xlqc")
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
        method_rel = find("OFML_ApiHost", ".xmth")
        if method_rel:
            used[method_rel] = "OFML_ApiHost"

        changes = {}
        first_seen = False
        for rel, name in sorted(used.items()):
            entry = index[rel]
            info = {"name": name, "file": rel, "sha256": entry["sha256"], "size": entry["size"],
                    "stored_as": f"definitions\\versions\\{entry['sha256']}{pathlib.PureWindowsPath(rel).suffix}"}
            if rel.lower().endswith(".xcmp") and name in refs.get("tips", []):
                info["values"] = tip_values(self.pc.read(rel))
            manifest["definitions"].append(info)
            if rel not in base:
                base[rel] = entry["sha256"]
                first_seen = True
                manifest["first_seen"].append(f"{name} ({rel})")
            elif base[rel] != entry["sha256"]:
                changes[rel] = {"name": name, "accepted": base[rel], "now": entry["sha256"]}
        record.definitions = [[rel, index[rel]["sha256"]] for rel in sorted(used)]
        if rt_rel:
            record.definitions.append([rt_rel, index[rt_rel]["sha256"]])
        manifest["problems"] = problems
        manifest["changed_since_accepted"] = changes
        record.write_manifest(manifest)
        where = self.location(remote_rel)
        if problems:
            record.finish({"status": "refused", "reason": problems})
            raise DefinitionCheckFailed("Refused before sending: " + "; ".join(problems) + f" (record: {where})")
        if changes:
            self.pending = changes
            record.finish({"status": "refused", "reason": "definitions changed", "changes": changes})
            names = ", ".join(f"{c['name']} ({r})" for r, c in changes.items())
            raise DefinitionCheckFailed(
                f"Refused before sending: these definitions changed since they were last accepted: {names}. "
                f"Check them, then call AcceptDefinitionChanges and send again (record: {where})")
        if first_seen and not self.dry_run:
            self.pc.write_baseline(base)
        return record


class RunRecord:
    """One record per operation, built in a temporary folder and moved to the Fluent PC when it finishes."""

    def __init__(self, preflight: Preflight, remote_rel: str):
        self.preflight = preflight
        self.remote_rel = remote_rel
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="ofml-record-"))
        self.folder = self._tmp / remote_rel.rsplit("\\", 1)[-1]
        self.folder.mkdir()
        self.definitions: list = []
        """[relative path, SHA-256] of every definition the operation used."""

    def write_steps(self, steps: list[tuple[str, str]]) -> None:
        d = self.folder / "steps"
        d.mkdir(exist_ok=True)
        for i, (label, xml) in enumerate(steps, 1):
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:60]
            (d / f"{i:02d}_{safe}.xml").write_text(xml, encoding="utf-8")

    def write_manifest(self, manifest: dict) -> None:
        (self.folder / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1, default=list), encoding="utf-8")

    def finish(self, result: dict) -> None:
        result = {"finished": datetime.datetime.now().isoformat(timespec="seconds"), **result}
        (self.folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            self.preflight.store(self.folder, self.remote_rel, self.definitions)
        finally:
            self.discard()

    def discard(self) -> None:
        shutil.rmtree(remote.long_path(self._tmp), ignore_errors=True)


# ---------------------------------------------------------------- liquid class settings for one tip type
EQ_FIELDS = ("AspiratePipettingSpeed", "DispensePipettingSpeed", "DelayAfterAspiration", "LeadingAirgapVolumeFormular",
             "TrailingAirgapVolumeFormular", "ExcessVolumeFormular", "ConditioningVolumeFormular", "CorrectionVolumeFormular")
DET_FIELDS = ("cLLDOnOff", "clldSensitivity", "submergeDepth", "aspirationTrackingOnOff", "dispenseTrackingOnOff",
              "zPosition", "zOffset", "retractPosition", "retractPositionOffset", "retractSpeed", "moveSpeedOutOfWell",
              "density", "viscosity")


def liquid_class_settings(xml: bytes, tip_group: str, part: str = ARM_LIQUID_CLASS_PART) -> dict | None:
    """Main values of a liquid class for one tip group (e.g. "FCA DiTi 200µl") and pipetting device. None when the
    liquid class has no equation set for that tip group (FluentControl would have nothing to compute volumes with)."""
    t = xml.decode("utf-8-sig", errors="replace")
    dev = f"<PipettingDeviceType>{part}</PipettingDeviceType>"
    u = html.unescape
    grp = f"<DiTiTypeGroup>{tip_group}</DiTiTypeGroup>"
    eq = [b for b in re.findall(r"<Tecan.Core.Pipetting.LiquidClassEquationSet>(.*?)</Tecan.Core.Pipetting.LiquidClassEquationSet>", t, re.S)
          if dev in b and grp in b]
    if not eq:
        return None
    out: dict = {"tip_group": tip_group, "device": part,
                 "equations": {k: u(v) for k, v in re.findall(r"<(\w+)>([^<]*)</\1>", eq[0]) if k in EQ_FIELDS}}
    for b in re.findall(r"<Tecan.Core.Pipetting.LiquidClassDetectionAndPositioningSet>(.*?)</Tecan.Core.Pipetting.LiquidClassDetectionAndPositioningSet>", t, re.S):
        if dev in b and grp in b:
            section = (re.search(r"<PipettingSectionName>(\w+)</PipettingSectionName>", b) or [None, "?"])[1]
            pairs = re.findall(r"<Value>([^<]*)</Value>\s*<VariableName>([^<]*)</VariableName>", b)
            out[section.lower()] = {name: u(value) for value, name in pairs if name in DET_FIELDS}
    return out
