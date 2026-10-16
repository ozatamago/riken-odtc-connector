"""Deck cameras: still images go through the recorder while it records; recording start/stop and the MP4."""

import json

import pytest

from riken_odtc_connector.deck_camera import (
    CameraFailed,
    DeckCameraConfig,
    DeckCameras,
    frames_per_second,
    parse_shot_lines,
)


class FakePc:
    """Stands in for the Fluent PC: records the commands, answers like the scripts would."""

    def __init__(self, recorder: dict | None = None):
        self.recorder = recorder
        self.commands: list[str] = []
        self.put: dict[str, str] = {}

    def ps(self, command, timeout=120):
        self.commands.append(command)
        if "ConvertFrom-Json).pid" in command:  # recorder()
            return json.dumps(self.recorder) if self.recorder else ""
        if ".done" in command:  # a still-image request
            names, folder, tag = next(v for k, v in self.put.items() if k.endswith(".tmp")).split("|")
            return "\n".join(f"OK {n} {folder}\\x_{n}_{tag}.png {folder}\\x_{n}_{tag}_preview.jpg 3072x2048"
                             for n in names.split(","))
        return ""

    def ssh(self, command, timeout=120):
        self.commands.append(command)
        return "OK Center C:\\a.png C:\\a_preview.jpg 3072x2048\n"


def make(pc: FakePc, monkeypatch) -> DeckCameras:
    cams = DeckCameras(DeckCameraConfig(), "Tecan@pc", r"C:\Users\Tecan\OFML\records", dry_run=False)
    monkeypatch.setattr(cams, "_ps", pc.ps)
    monkeypatch.setattr(cams, "_ssh", pc.ssh)
    monkeypatch.setattr(cams, "_put", lambda text, path: pc.put.__setitem__(path, text))
    return cams


def test_parse_shot_lines_and_frame_rate():
    r = parse_shot_lines("OK Left C:\\a.png C:\\a_p.jpg 3072x2048\r\nERROR Right camera 1 not found\n")
    assert r[0] == {"camera": "Left", "status": "ok", "image": "C:\\a.png", "preview": "C:\\a_p.jpg", "size": "3072x2048"}
    assert r[1] == {"camera": "Right", "status": "error", "error": "camera 1 not found"}
    assert frames_per_second("12:00:00.000 1\n12:00:00.500 2\n12:00:01.000 3\n") == 2.0


def test_still_images_without_recorder_use_the_shot_script(monkeypatch):
    pc = FakePc()
    [r] = make(pc, monkeypatch).capture(["Center"], r"C:\out", "start")
    assert r["status"] == "ok" and "camera_shot.ps1" in pc.commands[-1] and '-Tag "start"' in pc.commands[-1]


def test_still_images_while_recording_are_asked_from_the_recorder(monkeypatch):
    pc = FakePc({"pid": 42, "out_dir": r"C:\rec", "record": "Left", "cameras": ["Left", "Center", "Right"],
                 "started": "2026-10-16T13:00:00"})
    cams = make(pc, monkeypatch)
    results = cams.capture(["Center", "Left"], r"C:\run\images", "end")
    [(path, text)] = pc.put.items()
    assert path.startswith("C:\\rec\\requests\\") and path.endswith(".tmp")
    assert text == "Center,Left|C:\\run\\images|end"
    assert [r["camera"] for r in results] == ["Center", "Left"] and all(r["status"] == "ok" for r in results)
    assert not any("camera_shot.ps1" in c for c in pc.commands)  # the cameras are not opened a second time
    assert cams.recorder()["record"] == ["Left"]  # a one-item list written by PowerShell as a string


def test_recording_is_refused_twice_and_for_unknown_cameras(monkeypatch):
    pc = FakePc({"pid": 42, "out_dir": r"C:\rec", "record": ["Left"], "cameras": [], "started": ""})
    cams = make(pc, monkeypatch)
    with pytest.raises(CameraFailed, match="Already recording"):
        cams.start_recording(["Center"])
    with pytest.raises(CameraFailed, match="Unknown camera"):
        cams.start_recording(["Top"])
    with pytest.raises(CameraFailed, match="Not recording"):
        make(FakePc(), monkeypatch).stop_recording()


def test_videos_go_into_the_open_run(monkeypatch):
    cams = make(FakePc(), monkeypatch)
    assert cams.video_dir().startswith("C:\\Users\\Tecan\\OFML\\records\\videos\\")
    cams.session_dir = lambda: r"C:\Users\Tecan\OFML\records\runs\x_session"
    assert cams.video_dir().startswith("C:\\Users\\Tecan\\OFML\\records\\runs\\x_session\\videos\\")


def test_dry_run_records_nothing():
    cams = DeckCameras(DeckCameraConfig(), "Tecan@pc", r"C:\records", dry_run=True)
    assert "not recorded: dry run" in cams.start_recording(["Left", "Right"])
    assert cams.stop_recording() == [] and cams.recorder() is None
