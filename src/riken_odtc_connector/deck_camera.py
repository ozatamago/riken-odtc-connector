"""The deck-check cameras of the Fluent 780 as SiLA commands, and images at the start and end of each open run.

The Fluent 780 has three LUCID PHX064S-C GigE cameras (3072 x 2048, colour) for FluentControl's Deck Check. They can
be read directly on the Fluent PC with the LUCID Arena SDK that FluentControl ships, without FluentControl and without
moving anything (found 2026-10-16).

- Left (242701174): the left of the deck (tip racks); hidden by the arms when they are parked on the left
- Center (234500007): wide angle, the whole deck
- Right (242701126): the right of the deck (hotel, incubators, ODTC)

Still images: windows/camera_shot.ps1 (in C:\\Users\\Tecan\\OFML\\tools) takes one image per camera and writes a PNG
plus a small JPEG preview. They go to OFML\\records\\images\\<date>\\ for single images, and
OFML\\records\\runs\\<session>\\images\\ at StartRun ("start") and EndRun ("end").

Video: windows/camera_videos.ps1 records any combination of cameras at once until StopRecording, about two frames per
second per camera with all three (the cameras share one 1 Gbit/s link, so they are triggered one after another). While
it records, it is the only program that opens the cameras: still images (TakeImage, and the images at StartRun and
EndRun) are then taken by the recorder, without stopping the video. StopRecording turns each camera's frames into
video.mp4 next to them on the Fluent PC and deletes the frames. Videos go to <session>\\videos\\<time>\\ while a run is
open, otherwise to OFML\\records\\videos\\<date>\\<time>\\.
"""

import dataclasses
import datetime
import hashlib
import json
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile

from unitelabs.cdk import sila
from unitelabs.cdk.config import Config


class CameraFailed(Exception):
    """No image or video could be taken. The description says why."""


@dataclasses.dataclass
class DeckCameraConfig(Config):
    """The deck-check cameras."""

    enabled: bool = True
    cameras: dict = dataclasses.field(
        default_factory=lambda: {"Left": "242701174", "Center": "234500007", "Right": "242701126"})
    """Camera name -> serial number."""
    script: str = r"C:\Users\Tecan\OFML\tools\camera_shot.ps1"
    video_script: str = r"C:\Users\Tecan\OFML\tools\camera_videos.ps1"
    recorder_state: str = r"C:\Users\Tecan\OFML\state\recorder.json"
    """Exists while the recorder runs (written and removed by camera_videos.ps1)."""
    work_dir: str = r"C:\Users\Tecan\OFML\work"
    max_recording_s: float = 1800
    """A recording stops by itself after this long (also if this server is gone)."""
    capture_on_run: bool = True
    """Take images of all cameras at StartRun and EndRun."""


def parse_shot_lines(text: str) -> list[dict]:
    """The output of camera_shot.ps1 (and the recorder's answers): one dict per camera."""
    results = []
    for line in text.replace("\r", "").splitlines():
        parts = line.split(" ")
        if parts[0] == "OK" and len(parts) >= 5:
            results.append({"camera": parts[1], "status": "ok", "image": parts[2], "preview": parts[3], "size": parts[4]})
        elif parts[0] == "ERROR" and len(parts) >= 2:
            results.append({"camera": parts[1], "status": "error", "error": " ".join(parts[2:])})
    return results


def frames_per_second(frames_txt: str) -> float:
    """Average frame rate from frames.txt ("HH:MM:SS.fff <camera ns>" per frame)."""
    stamps = [datetime.datetime.strptime(x.split()[0], "%H:%M:%S.%f") for x in frames_txt.splitlines() if x.strip()]
    if len(stamps) < 2:
        return 1.0
    seconds = (stamps[-1] - stamps[0]).total_seconds()
    return round((len(stamps) - 1) / seconds, 3) if seconds > 0 else 1.0


class DeckCameras:
    def __init__(self, config: DeckCameraConfig, ssh_target: str, records_dir: str, dry_run: bool):
        self.config = config
        self.ssh_target = ssh_target
        self.records_dir = records_dir.rstrip("\\")
        self.dry_run = dry_run
        self.session_dir = lambda: ""
        """Folder of the open run's record (set by the server); videos then go into it."""
        self._lock = threading.Lock()
        self._recorder: subprocess.Popen | None = None
        self._recorder_output: list[str] = []

    # ---- talking to the Fluent PC
    def _ssh(self, command: str, timeout: float = 120) -> str:
        out = subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", self.ssh_target, command],
                             capture_output=True, timeout=timeout)
        if out.returncode != 0:
            raise CameraFailed(f"SSH to the Fluent PC failed: {out.stderr.decode(errors='replace')[:300]}")
        return out.stdout.decode("utf-8", errors="replace").replace("\r", "")

    def _ps(self, command: str, timeout: float = 120) -> str:
        """Run PowerShell commands on the Fluent PC. Use single quotes only inside command."""
        return self._ssh(f'powershell -NoProfile -Command "{command}"', timeout)

    def _put(self, text: str, remote_path: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = pathlib.Path(tmp) / "f"
            f.write_text(text, encoding="utf-8")
            out = subprocess.run(["scp", "-q", str(f), f"{self.ssh_target}:{remote_path.replace(chr(92), '/')}"],
                                 capture_output=True, timeout=60)
            if out.returncode != 0:
                raise CameraFailed(f"Copy to the Fluent PC failed: {out.stderr.decode(errors='replace')[:300]}")

    def _get(self, remote_path: str, local: pathlib.Path) -> None:
        out = subprocess.run(["scp", "-q", f"{self.ssh_target}:{remote_path.replace(chr(92), '/')}", str(local)],
                             capture_output=True, timeout=300)
        if out.returncode != 0:
            raise CameraFailed(f"Copy from the Fluent PC failed: {out.stderr.decode(errors='replace')[:300]}")

    def _check_names(self, names: list[str]) -> None:
        unknown = [n for n in names if n not in self.config.cameras]
        if unknown or not names:
            raise CameraFailed(f"Unknown camera {unknown}; cameras are {', '.join(self.config.cameras)}")

    # ---- still images
    def capture(self, names: list[str], out_dir: str, tag: str = "") -> list[dict]:
        """Take one image per named camera into out_dir on the Fluent PC. Returns one dict per camera. While the
        recorder runs, the recorder takes them (the video goes on)."""
        self._check_names(names)
        if self.dry_run or not self.config.enabled:
            return [{"camera": n, "status": "skipped (dry run)" if self.dry_run else "disabled"} for n in names]
        with self._lock:  # one program at a time may open a camera
            rec = self.recorder()
            if rec:
                return self._request_stills(rec, names, out_dir, tag)
            spec = ",".join(f"{n}={self.config.cameras[n]}" for n in names)
            text = self._ssh(f'powershell -NoProfile -ExecutionPolicy Bypass -File "{self.config.script}" '
                             f'-Cameras "{spec}" -OutDir "{out_dir}"' + (f' -Tag "{tag}"' if tag else ""))
        results = parse_shot_lines(text)
        if not results:
            raise CameraFailed("The camera script printed nothing")
        return results

    def _request_stills(self, rec: dict, names: list[str], out_dir: str, tag: str) -> list[dict]:
        req = f"{rec['out_dir']}\\requests\\{uuid.uuid4().hex[:12]}"
        self._put(f"{','.join(names)}|{out_dir}|{tag}", req + ".tmp")
        # Renamed only when complete, so the recorder never reads half a request
        text = self._ps(f"Rename-Item '{req}.tmp' '{req.rsplit(chr(92), 1)[1]}.req'; "
                        f"$t = [Diagnostics.Stopwatch]::StartNew(); "
                        f"while (-not (Test-Path '{req}.done') -and $t.Elapsed.TotalSeconds -lt 30) {{ Start-Sleep -Milliseconds 100 }}; "
                        f"if (Test-Path '{req}.done') {{ Get-Content '{req}.done'; Remove-Item '{req}.done' }} "
                        f"else {{ Remove-Item '{req}.req' -ErrorAction SilentlyContinue; 'TIMEOUT' }}")
        if "TIMEOUT" in text:
            raise CameraFailed("The recorder did not answer within 30 s")
        results = parse_shot_lines(text)
        if not results:
            raise CameraFailed(f"The recorder's answer was empty: {text[:200]}")
        return results

    def preview(self, path: str) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            dest = pathlib.Path(tmp) / "p.jpg"
            self._get(path, dest)
            return dest.read_bytes()

    def single_dir(self) -> str:
        return f"{self.records_dir}\\images\\{datetime.date.today():%Y%m%d}"

    # ---- video
    def recorder(self) -> dict | None:
        """The running recorder ({pid, out_dir, record, cameras, started}) or None. Read from the Fluent PC, so a
        recording started before this server restarted is found too."""
        if self.dry_run or not self.config.enabled:
            return None
        f = self.config.recorder_state
        text = self._ps(f"if (Test-Path '{f}') {{ $s = Get-Content '{f}' -Raw; "
                        f"if (Get-Process -Id ($s | ConvertFrom-Json).pid -ErrorAction SilentlyContinue) {{ $s }} }}")
        text = text.strip().lstrip("\ufeff")
        if not text:
            return None
        rec = json.loads(text)
        if isinstance(rec.get("record"), str):  # PowerShell writes a one-item list as a plain string
            rec["record"] = [rec["record"]]
        return rec

    def video_dir(self) -> str:
        stamp = datetime.datetime.now()
        session = self.session_dir()
        if session:
            return f"{session}\\videos\\{stamp:%Y%m%d-%H%M%S}"
        return f"{self.records_dir}\\videos\\{stamp:%Y%m%d}\\{stamp:%H%M%S}"

    def start_recording(self, names: list[str], fps: float = 0, out_dir: str = "") -> str:
        """Start recording the named cameras on the Fluent PC and return the folder. Returns once frames come in."""
        self._check_names(names)
        if fps < 0:
            raise CameraFailed("FramesPerSecond must be 0 (as fast as possible) or more")
        out_dir = out_dir or self.video_dir()
        if self.dry_run or not self.config.enabled:
            return f"{out_dir} (not recorded: {'dry run' if self.dry_run else 'cameras disabled'})"
        with self._lock:
            rec = self.recorder()
            if rec:
                raise CameraFailed(f"Already recording {', '.join(rec['record'])} into {rec['out_dir']}; stop it first")
            spec = ",".join(f"{n}={s}" for n, s in self.config.cameras.items())
            cmd = (f'powershell -NoProfile -ExecutionPolicy Bypass -File "{self.config.video_script}" -Cameras "{spec}" '
                   f'-Record "{",".join(names)}" -OutDir "{out_dir}" -Fps {fps:g} -MaxSeconds {self.config.max_recording_s:g} '
                   f'-StateFile "{self.config.recorder_state}"')
            # The recorder keeps running on the Fluent PC even if this SSH connection goes (tested 2026-10-16)
            proc = subprocess.Popen(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", self.ssh_target, cmd],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            lines: list[str] = []

            def read():
                for raw in proc.stdout:
                    lines.append(raw.decode("utf-8", errors="replace").rstrip())

            threading.Thread(target=read, daemon=True).start()
            deadline = time.monotonic() + 60
            while not any(x.startswith("started") for x in lines):
                if proc.poll() is not None or time.monotonic() > deadline:
                    time.sleep(0.5)
                    raise CameraFailed("The recorder did not start: " + " / ".join(lines)[-500:])
                time.sleep(0.2)
            self._recorder, self._recorder_output = proc, lines
        return out_dir

    def stop_recording(self) -> list[dict]:
        """Stop the recorder, make video.mp4 per camera on the Fluent PC and delete the frames."""
        if self.dry_run or not self.config.enabled:
            return []
        rec = self.recorder()
        if not rec:
            raise CameraFailed("Not recording")
        out = rec["out_dir"]
        text = self._ps(f"New-Item -ItemType File -Force '{out}\\stop' | Out-Null; "
                        f"$t = [Diagnostics.Stopwatch]::StartNew(); "
                        f"while ((Get-Process -Id {int(rec['pid'])} -ErrorAction SilentlyContinue) -and $t.Elapsed.TotalSeconds -lt 120) {{ Start-Sleep -Milliseconds 200 }}; "
                        f"if (Get-Process -Id {int(rec['pid'])} -ErrorAction SilentlyContinue) {{ 'STILL RUNNING' }} "
                        f"else {{ Remove-Item '{out}\\stop'; Remove-Item '{out}\\requests' -Recurse -ErrorAction SilentlyContinue }}", timeout=180)
        if "STILL RUNNING" in text:
            raise CameraFailed("The recorder did not stop within 120 s")
        if self._recorder is not None:
            try:
                self._recorder.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._recorder.kill()
            self._put("\n".join(self._recorder_output) + "\n", f"{out}\\recorder.log")
            self._recorder = None
        return [self.make_mp4(f"{out}\\{name}", name) for name in rec["record"]]

    def make_mp4(self, folder: str, camera: str = "") -> dict:
        """frame_*.jpg in folder on the Fluent PC -> folder\\video.mp4; the frames are deleted once the copy is checked."""
        import imageio_ffmpeg

        zip_remote = f"{self.config.work_dir}\\frames_{uuid.uuid4().hex[:8]}.zip"
        count = self._ps(f"$f = @(Get-ChildItem '{folder}' -Filter frame_*.jpg); if ($f.Count) "
                         f"{{ Compress-Archive -Path '{folder}\\frame_*.jpg' -DestinationPath '{zip_remote}' -Force }}; $f.Count",
                         timeout=600).strip().splitlines()[-1]
        if count == "0":
            return {"camera": camera, "status": "no frames", "folder": folder}
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="ofml-video-"))
        try:
            self._get(zip_remote, tmp / "f.zip")
            self._ps(f"Remove-Item '{zip_remote}'")
            self._get(f"{folder}\\frames.txt", tmp / "frames.txt")
            with zipfile.ZipFile(tmp / "f.zip") as z:
                z.extractall(tmp / "frames")
            fps = frames_per_second((tmp / "frames.txt").read_text())
            mp4 = tmp / "video.mp4"
            out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-y", "-framerate", f"{fps}",
                                  "-i", str(tmp / "frames" / "frame_%04d.jpg"), "-vf", "scale=1280:-2",
                                  "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "25", str(mp4)],
                                 capture_output=True, timeout=1800)
            if out.returncode != 0:
                raise CameraFailed(f"ffmpeg failed: {out.stderr.decode(errors='replace')[:300]}")
            self._get_back(mp4, f"{folder}\\video.mp4")
            self._ps(f"Remove-Item '{folder}\\frame_*.jpg'")
            return {"camera": camera, "status": "ok", "video": f"{folder}\\video.mp4", "frames": int(count), "fps": fps,
                    "bytes": mp4.stat().st_size}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)  # nothing of it is kept on this computer

    def _get_back(self, local: pathlib.Path, remote: str) -> None:
        out = subprocess.run(["scp", "-q", str(local), f"{self.ssh_target}:{remote.replace(chr(92), '/')}"],
                             capture_output=True, timeout=600)
        if out.returncode != 0:
            raise CameraFailed(f"Copy to the Fluent PC failed: {out.stderr.decode(errors='replace')[:300]}")
        want = hashlib.sha256(local.read_bytes()).hexdigest().upper()
        got = self._ps(f"(Get-FileHash '{remote}').Hash").strip()
        if got != want:
            raise CameraFailed(f"The copy of {remote} on the Fluent PC differs; frames kept")


class DeckCamera(sila.Feature):
    """
    Images and video of the Fluent 780 deck from the three deck-check cameras (Left, Center, Right). Nothing moves and
    FluentControl is not used. Everything is saved on the Fluent PC with the records.

    Center is a wide-angle view of the whole deck; Left and Right show each half more closely. The arms can hide part
    of the view (for example, Left is hidden when the arms are parked on the left).

    Video runs in the background (StartRecording ... StopRecording), also while other commands move the instrument.
    Images can be taken while recording.
    """

    def __init__(self, cameras: DeckCameras):
        super().__init__(originator="jp.riken.bdr", category="fluent", version="1.0")
        self.cameras = cameras

    @sila.UnobservableProperty()
    async def get_cameras(self) -> list[str]:
        """The camera names, e.g. "Center"."""
        return list(self.cameras.config.cameras)

    @sila.ObservableCommand(errors=[CameraFailed])
    async def take_image(self, camera: str, *, status: sila.Status) -> tuple[str, bytes]:
        """
        Take one image with one camera and save it on the Fluent PC. Works while recording too.

        Args:
          Camera: "Left", "Center" or "Right".

        Returns:
          ImagePath: Where the full-size PNG (3072 x 2048) is saved on the Fluent PC.
          Preview: A smaller JPEG (1280 px wide) of the same image.
        """
        import asyncio

        try:
            [r] = await asyncio.to_thread(self.cameras.capture, [camera], self.cameras.single_dir())
            if r["status"] != "ok":
                raise CameraFailed(r.get("error") or r["status"])
            return r["image"], await asyncio.to_thread(self.cameras.preview, r["preview"])
        except CameraFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CameraFailed(f"{type(exc).__name__}: {exc}") from None

    @sila.ObservableCommand(errors=[CameraFailed])
    async def take_all_images(self, *, status: sila.Status) -> list[str]:
        """
        Take one image with each camera and save them on the Fluent PC. Works while recording too.

        Returns:
          ImagePaths: Where the PNGs are saved, or an error per camera that failed.
        """
        import asyncio

        try:
            results = await asyncio.to_thread(self.cameras.capture, list(self.cameras.config.cameras),
                                              self.cameras.single_dir())
        except Exception as exc:  # noqa: BLE001
            raise CameraFailed(str(exc)) from None
        return [r.get("image") or f"{r['camera']}: {r.get('error') or r['status']}" for r in results]

    @sila.ObservableCommand(errors=[CameraFailed])
    async def start_recording(self, cameras: list[str], frames_per_second: float, *, status: sila.Status) -> str:
        """
        Start recording video with the given cameras and return at once; the video goes on in the background until
        StopRecording (at most 30 minutes). Other commands, including ones that move the instrument, can run meanwhile.

        Args:
          Cameras: Cameras to record, e.g. ["Left", "Center", "Right"] or ["Center"].
          FramesPerSecond: Frames per second per camera, or 0 for as many as possible (about 2 with three cameras,
            3 with two; the cameras share one network link and are read one after another).

        Returns:
          Folder: Where the video is saved on the Fluent PC (inside the open run's record if a run is open).
        """
        import asyncio

        try:
            return await asyncio.to_thread(self.cameras.start_recording, list(cameras), float(frames_per_second))
        except CameraFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CameraFailed(f"{type(exc).__name__}: {exc}") from None

    @sila.ObservableCommand(errors=[CameraFailed])
    async def stop_recording(self, *, status: sila.Status) -> list[str]:
        """
        Stop the video and save one MP4 per camera on the Fluent PC (this takes a little while: the frames are joined
        into a video).

        Returns:
          Videos: Where each camera's video.mp4 is saved, or why a camera has none.
        """
        import asyncio

        try:
            results = await asyncio.to_thread(self.cameras.stop_recording)
        except CameraFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CameraFailed(f"{type(exc).__name__}: {exc}") from None
        return [r.get("video") or f"{r['camera']}: {r['status']}" for r in results]

    @sila.UnobservableProperty()
    async def get_recording(self) -> str:
        """What is being recorded (cameras, start time and folder on the Fluent PC), or empty when not recording."""
        import asyncio

        rec = await asyncio.to_thread(self.cameras.recorder)
        return f"{', '.join(rec['record'])} since {rec['started']} into {rec['out_dir']}" if rec else ""
