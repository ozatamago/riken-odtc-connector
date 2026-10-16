"""The deck-check cameras of the Fluent 780 as SiLA commands, and images at the start and end of each open run.

The Fluent 780 has three LUCID PHX064S-C GigE cameras (3072 x 2048, colour) for FluentControl's Deck Check. They can
be read directly on the Fluent PC with the LUCID Arena SDK that FluentControl ships, without FluentControl and without
moving anything (found 2026-10-16). windows/camera_shot.ps1 (copied to C:\\Users\\Tecan\\OFML\\tools) takes one image
per camera and writes a PNG plus a small JPEG preview.

- Left (242701174): the left of the deck (tip racks); hidden by the arms when they are parked on the left
- Center (234500007): wide angle, the whole deck
- Right (242701126): the right of the deck (hotel, incubators, ODTC)

Images go to the Fluent PC next to the records: OFML\\records\\images\\<date>\\ for single images, and
OFML\\records\\runs\\<session>\\images\\ at StartRun ("start") and EndRun ("end"), so each run shows what was on the
deck before and after.
"""

import dataclasses
import datetime
import os
import subprocess
import tempfile
import threading

from unitelabs.cdk import sila
from unitelabs.cdk.config import Config


class CameraFailed(Exception):
    """No image could be taken. The description says why."""


@dataclasses.dataclass
class DeckCameraConfig(Config):
    """The deck-check cameras."""

    enabled: bool = True
    cameras: dict = dataclasses.field(
        default_factory=lambda: {"Left": "242701174", "Center": "234500007", "Right": "242701126"})
    """Camera name -> serial number."""
    script: str = r"C:\Users\Tecan\OFML\tools\camera_shot.ps1"
    capture_on_run: bool = True
    """Take images of all cameras at StartRun and EndRun."""


class DeckCameras:
    def __init__(self, config: DeckCameraConfig, ssh_target: str, records_dir: str, dry_run: bool):
        self.config = config
        self.ssh_target = ssh_target
        self.records_dir = records_dir.rstrip("\\")
        self.dry_run = dry_run
        self._lock = threading.Lock()

    def capture(self, names: list[str], out_dir: str, tag: str = "") -> list[dict]:
        """Take one image per named camera into out_dir on the Fluent PC. Returns one dict per camera."""
        unknown = [n for n in names if n not in self.config.cameras]
        if unknown:
            raise CameraFailed(f"Unknown camera {unknown}; cameras are {', '.join(self.config.cameras)}")
        if self.dry_run or not self.config.enabled:
            return [{"camera": n, "status": "skipped (dry run)" if self.dry_run else "disabled"} for n in names]
        spec = ",".join(f"{n}={self.config.cameras[n]}" for n in names)
        cmd = (f'powershell -NoProfile -ExecutionPolicy Bypass -File "{self.config.script}" '
               f'-Cameras "{spec}" -OutDir "{out_dir}"' + (f' -Tag "{tag}"' if tag else ""))
        with self._lock:  # one program at a time may open a camera
            out = subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", self.ssh_target, cmd],
                                 capture_output=True, timeout=120)
        if out.returncode != 0:
            raise CameraFailed(f"SSH to the Fluent PC failed: {out.stderr.decode(errors='replace')[:300]}")
        results = []
        for line in out.stdout.decode("utf-8", errors="replace").replace("\r", "").splitlines():
            parts = line.split(" ")
            if parts[0] == "OK" and len(parts) >= 5:
                results.append({"camera": parts[1], "status": "ok", "image": parts[2], "preview": parts[3],
                                "size": parts[4]})
            elif parts[0] == "ERROR":
                results.append({"camera": parts[1], "status": "error", "error": " ".join(parts[2:])})
        if not results:
            raise CameraFailed("The camera script printed nothing")
        return results

    def preview(self, path: str) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "p.jpg")
            out = subprocess.run(["scp", "-q", f"{self.ssh_target}:{path.replace(chr(92), '/')}", dest],
                                 capture_output=True, timeout=60)
            if out.returncode != 0:
                raise CameraFailed(f"Copy from the Fluent PC failed: {out.stderr.decode(errors='replace')[:300]}")
            with open(dest, "rb") as f:
                return f.read()

    def single_dir(self) -> str:
        return f"{self.records_dir}\\images\\{datetime.date.today():%Y%m%d}"


class DeckCamera(sila.Feature):
    """
    Images of the Fluent 780 deck from the three deck-check cameras (Left, Center, Right). Taking an image does not
    move anything and does not use FluentControl. Images are saved on the Fluent PC with the records.

    Center is a wide-angle view of the whole deck; Left and Right show each half more closely. The arms can hide part
    of the view (for example, Left is hidden when the arms are parked on the left).
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
        Take one image with one camera and save it on the Fluent PC.

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
        Take one image with each camera and save them on the Fluent PC.

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
