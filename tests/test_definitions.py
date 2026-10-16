"""Definition check and run records (definitions.py), and holding a run open. Nothing is sent to the Fluent."""

import asyncio
import json
import pathlib

import pytest

from riken_odtc_connector.definitions import (
    DefinitionCheckFailed,
    DefinitionsConfig,
    Preflight,
    extract_references,
)
from riken_odtc_connector.fluent_backend import BackendError, FluentBackendConfig, FluentOdtcBackend

FIX = pathlib.Path(__file__).parent / "fixtures"


def test_names_are_taken_from_gui_statements():
    rga = extract_references((FIX / "gui_macros" / "RGA1_TransferLabware_1.xml").read_text())
    assert rga["locations"] == [("Regrip_Pos", "1")] and rga["arms"] == ["RGA 1"]
    asp = extract_references((FIX / "gui_statements" / "LihaAspirateScriptCommandDataV5_20.xml").read_text())
    assert asp["liquid_classes"] == ["Water Free Single"] and asp["arms"] == ["Air FCA 1"]
    add = extract_references((FIX / "gui_statements" / "AddLabwareDataV1_1.xml").read_text())
    assert add["labware_types"] == ["96 Well Skirted PCR_Custom_Template"]
    assert add["locations"] == [("BioShake_Q1_Adapter_Site_PCR", "1")]
    rd = extract_references((FIX / "gui_statements" / "LihaReagentDistributionSmartCommand_4.xml").read_text())
    assert rd["tips"] == ["FCA, 1000ul SBS"] and rd["liquid_classes"] == ["Water Free Multi"]
    assert rd["auxiliary_liquid_classes"] == ["Empty Tip", "Standard Wash"] and rd["arms"] == ["Air FCA 1"]


# ---- a tiny stand-in for the FluentControl database
C = "http://schemas.datacontract.org/2004/07/Tecan.VisionX.Worktable.Core"
A = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"


def vx(name: str, body: str) -> str:
    return f"<VxData><Payload><ObjectName>{name}</ObjectName><PayloadData>{body}</PayloadData></Payload></VxData>"


def site(template: str, child: str = "") -> str:
    return (f"<a:KeyValueOfintSite><a:Key>0</a:Key><a:Value><BaseTemplateGuid>{template}</BaseTemplateGuid>"
            f"{child}</a:Value></a:KeyValueOfintSite>")


def placed(label: str) -> str:
    return (f"<ConnectedComponent><LabwareName><a:KeyValueOfstringstring><a:Key>default</a:Key>"
            f"<a:Value>{label}</a:Value></a:KeyValueOfstringstring></LabwareName></ConnectedComponent>")


class FakePc:
    """Stands in for the Fluent PC: the definition files, the baseline and the stored records."""

    def __init__(self, files: dict[str, str], records: pathlib.Path):
        self.files = {rel: text.encode() for rel, text in files.items()}
        self.baseline: dict[str, str] = {}
        self.records = records

    def index(self):
        import hashlib

        out = {}
        for rel, data in self.files.items():
            text = data.decode()
            loc = text.split("<LocationGroupName>")[1].split("<")[0] if "<LocationGroupName>" in text else ""
            out[rel] = {"path": rel, "size": len(data), "ticks": "0", "sha256": hashlib.sha256(data).hexdigest(),
                        "name": text.split("<ObjectName>")[1].split("<")[0], "location": loc,
                        "airfca": "<PipettingDeviceType>AirFca</PipettingDeviceType>" in text}
        return out

    def read(self, rel):
        return self.files[rel]

    def read_baseline(self):
        return dict(self.baseline)

    def write_baseline(self, baseline):
        self.baseline = dict(baseline)

    def store_record(self, local, remote_rel, definitions):
        import shutil

        dest = self.records / remote_rel.replace("\\", "/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if local.is_dir():
            shutil.copytree(local, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(local, dest)
        for rel, sha in definitions:
            v = self.records.parent / "versions" / f"{sha}{pathlib.PurePosixPath(rel.replace(chr(92), '/')).suffix}"
            v.parent.mkdir(parents=True, exist_ok=True)
            if not v.exists():
                v.write_bytes(self.files[rel])


def database() -> dict[str, str]:
    return {
        r"SystemSpecific\LiquidClasses\lc1.xlqc": vx("Water Free Single", "<PipettingDeviceType>AirFca</PipettingDeviceType>"),
        r"SystemSpecific\LiquidClasses\lc2.xlqc": vx("Standard Wash", "<PipettingDeviceType>Fca</PipettingDeviceType>"),
        r"SystemSpecific\Worktable\Components\tip200.xcmp": vx("FCA, 200ul SBS", "<Key>DiTiTypeGroup</Key><Value>FCA DiTi 200µl</Value>"),
        r"SystemSpecific\Worktable\Sites\s-regrip.xsit": vx("s-regrip", "<LocationGroupName>Regrip_Pos</LocationGroupName>"),
        r"SystemSpecific\Worktable\Sites\s-nest.xsit": vx("s-nest", "<LocationGroupName>Nest7mm_Pos</LocationGroupName>"),
        r"SystemSpecific\DriverConfiguration\rga.xdrv": vx("RGA 1", ""),
        r"SystemSpecific\Configuration\fca.xcfg": vx("Fca", ""),
        r"SystemSpecific\Worktable\Workspaces\ws.xwsp": vx("WT_TEST", (
            f'<Workspace xmlns="{C}" xmlns:a="{A}"><Frame><Arrangements>'
            f'<Arrangement><Sites>{site("s-regrip")}</Sites></Arrangement>'
            f'<Arrangement><Sites>{site("s-nest", placed("Plate[001]"))}{site("s-nest")}</Sites></Arrangement>'
            "</Arrangements></Frame></Workspace>")),
    }


def rga_xml(location: str, site_no: int, labware: str = "Plate[001]") -> str:
    inner = (f"<TransferLabwareCommandParameters><Labware>{labware}</Labware><Location>{location}</Location>"
             f"<Site>{site_no}</Site></TransferLabwareCommandParameters>")
    esc = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")
    return (f'<ScriptGroup><Objects><Object Type="Tecan.VisionX.ApplicationDriver.ApplicationDriverBase.ApplicationDriverMacro">'
            f'<ApplicationDriverMacro Version="1" Name="RGA1_TransferLabware" ModuleName="RGA 1">'
            f"<ExecutionSettings>{esc}</ExecutionSettings></ApplicationDriverMacro></Object></Objects></ScriptGroup>")


def aspirate_xml(liquid_class: str) -> str:
    return ('<Object Type="Tecan.Core.Instrument.Devices.LiHa.Scripting.LihaAspirateScriptCommandDataV5">'
            f"<LiquidClassNameBySelection>{liquid_class}</LiquidClassNameBySelection>"
            "<LabwareName>Plate[001]</LabwareName></Object>")


@pytest.fixture
def pc(tmp_path):
    return FakePc(database(), tmp_path / "fluent_pc_records")


@pytest.fixture
def preflight(pc):
    return Preflight(DefinitionsConfig(workspace="WT_TEST"), dry_run=False, pc=pc, background=False)


def test_ok_command_is_recorded_on_the_fluent_pc_only(preflight, pc, tmp_path):
    record = preflight.begin([("RGA move", rga_xml("Nest7mm_Pos", 2)), ("Aspirate", aspirate_xml("Water Free Single"))], "test")
    record.finish({"status": "ok"})
    [stored] = list((pc.records / "runs").iterdir())
    manifest = json.loads((stored / "manifest.json").read_text())
    names = {d["name"] for d in manifest["definitions"]}
    assert {"WT_TEST", "Water Free Single", "RGA 1", "Fca"} <= names
    assert manifest["problems"] == [] and manifest["first_seen"] and pc.baseline
    assert (stored / "steps" / "01_RGA_move.xml").exists() and (stored / "result.json").exists()
    assert not (stored / "definitions").exists()  # versions are kept once, outside the record
    lc = next(d for d in manifest["definitions"] if d["name"] == "Water Free Single")
    version = pc.records.parent / "versions" / f"{lc['sha256']}.xlqc"
    assert version.read_bytes() == pc.files[lc["file"]] and lc["stored_as"].endswith(version.name)
    assert not record._tmp.exists() and not (tmp_path / ".riken_fluent780").exists()  # nothing left locally


def test_failed_copy_waits_locally_and_is_sent_later(preflight, pc, tmp_path):
    real = pc.store_record
    pc.store_record = lambda *a: (_ for _ in ()).throw(OSError("network down"))
    preflight.begin([("x", rga_xml("Regrip_Pos", 1))], "first").finish({"status": "ok"})
    assert list((tmp_path / ".riken_fluent780" / "pending").rglob("*.pending.json"))
    pc.store_record = real
    preflight.begin([("x", rga_xml("Regrip_Pos", 1))], "second").finish({"status": "ok"})
    assert len(list((pc.records / "runs").iterdir())) == 2
    assert not list((tmp_path / ".riken_fluent780" / "pending").rglob("*.pending.json"))


@pytest.mark.parametrize(("steps", "message"), [
    ([("x", rga_xml("Nest7mm_Pos", 3))], r"Nest7mm_Pos\[3\] does not exist"),
    ([("x", rga_xml("Nowhere_Pos", 1))], "Location 'Nowhere_Pos' is not on worktable"),
    ([("x", aspirate_xml("No Such Class"))], "Liquid class 'No Such Class' is not defined"),
    ([("x", aspirate_xml("Standard Wash"))], "has no AirFca part"),
])
def test_missing_or_unfit_definitions_refuse(preflight, steps, message):
    with pytest.raises(DefinitionCheckFailed, match=message):
        preflight.begin(steps, "test")


def test_changed_definition_refuses_until_accepted(preflight, pc):
    steps = [("Aspirate", aspirate_xml("Water Free Single"))]
    preflight.begin(steps, "first")  # accepted on first sight
    rel = r"SystemSpecific\LiquidClasses\lc1.xlqc"
    pc.files[rel] += b"<!-- edited -->"
    with pytest.raises(DefinitionCheckFailed, match="changed since they were last accepted: Water Free Single"):
        preflight.begin(steps, "second")
    assert preflight.accept_pending_changes() == [f"Water Free Single ({rel})"]
    preflight.begin(steps, "third")


def test_labware_not_on_the_template_is_a_warning(preflight):
    record = preflight.begin([("x", rga_xml("Regrip_Pos", 1, "Added[001]"))], "test")
    manifest = json.loads((record.folder / "manifest.json").read_text())
    assert any("Added[001]" in w for w in manifest["warnings"])


def test_open_run_is_shared_and_recorded_in_one_folder(tmp_path):
    backend = FluentOdtcBackend(FluentBackendConfig())  # dry run; the Fluent PC is offline -> names recorded only

    async def scenario():
        folder = pathlib.Path(await backend.start_run())
        assert backend.run_open
        with pytest.raises(BackendError, match="already open"):
            await backend.start_run()
        await backend.run_macro("SiLA-ODTC_OpenDoor")
        await backend.run_macro("SiLA-ODTC_CloseDoor")
        assert await backend.end_run() == 2
        assert not backend.run_open
        assert folder.is_relative_to(tmp_path / ".riken_fluent780" / "dry-run")
        records = sorted(p.name for p in folder.iterdir() if p.is_dir())
        assert len(records) == 2 and "OpenDoor" in records[0]
        assert json.loads((folder / "session.json").read_text())["commands"] == 2
        with pytest.raises(BackendError, match="No run is open"):
            await backend.end_run()

    asyncio.run(scenario())


def test_dry_run_records_are_removed_after_a_real_operation(tmp_path):
    dry = Preflight(DefinitionsConfig(), dry_run=True)
    dry.begin([("x", rga_xml("Regrip_Pos", 1))], "rehearsal").finish({"status": "ok"})
    assert (tmp_path / ".riken_fluent780" / "dry-run").exists()
    Preflight(DefinitionsConfig(), dry_run=False).forget_dry_runs()
    assert not (tmp_path / ".riken_fluent780" / "dry-run").exists()


def test_each_definition_version_is_stored_once(preflight, pc):
    for i in range(3):
        preflight.begin([("Aspirate", aspirate_xml("Water Free Single"))], f"op{i}").finish({"status": "ok"})
    rel = r"SystemSpecific\LiquidClasses\lc1.xlqc"
    pc.files[rel] += b"<!-- edited -->"
    preflight.pending = {}
    with pytest.raises(DefinitionCheckFailed):
        preflight.begin([("Aspirate", aspirate_xml("Water Free Single"))], "after edit")
    preflight.accept_pending_changes()
    preflight.begin([("Aspirate", aspirate_xml("Water Free Single"))], "accepted").finish({"status": "ok"})
    assert len(list((pc.records.parent / "versions").glob("*.xlqc"))) == 2  # before and after the edit


def test_a_hanging_fluent_pc_does_not_hold_up_the_command(pc, tmp_path):
    import threading
    import time

    release = threading.Event()
    real = pc.store_record
    pc.store_record = lambda *a: (release.wait(10), real(*a))
    p = Preflight(DefinitionsConfig(workspace="WT_TEST"), dry_run=False, pc=pc)  # background upload
    t0 = time.monotonic()
    p.begin([("x", rga_xml("Regrip_Pos", 1))], "slow").finish({"status": "ok"})
    assert time.monotonic() - t0 < 2  # returned while the upload still hangs
    assert list((tmp_path / ".riken_fluent780" / "pending").rglob("*.pending.json"))
    release.set()
    assert p.wait_for_uploads(10)
    assert len(list((pc.records / "runs").iterdir())) == 1
    assert not list((tmp_path / ".riken_fluent780" / "pending").rglob("*.pending.json"))
