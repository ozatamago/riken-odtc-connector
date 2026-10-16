"""A single SiLA 2 server, "Tecan Fluent 780", that combines the devices of the Fluent 780.

    uv run connector start --app riken_odtc_connector.fluent780:create_app -cfg config.fluent780.json

Only one backend that sends commands to FluentControl (FluentOdtcBackend) is created and passed to every Feature.
The backend allows only one operation at a time, so ODTC and Infinite operations queue up automatically, and one
server can no longer finish the channel of another server's run (2026-10-15, ODTC and Infinite servers merged).

Feature names and contents are the same as in the separate ODTC and Infinite servers; clients only need to change the
address. Features for devices added from now on are prefixed with the device name (e.g. BioShakeController) to avoid
name clashes.
"""

import collections.abc
import dataclasses

from unitelabs.cdk import Connector, ConnectorBaseConfig, SiLAServerConfig, sila

from .door_controller import DoorController
from .definitions import FluentPc
from .fluent_backend import FluentBackendConfig, FluentOdtcBackend
from .infinite.features import AbsorbanceReader, FluentPlateHandling, TrayController
from .infinite.reader import FluentInfiniteReader, InfiniteConfig
from .method_controller import MethodController
from .rga_controller import LabwareTracker, PlateTransferConfig, RgaController
from .run_controller import RunController
from .status_controller import StatusController
from .temperature_controller import TemperatureController
from .temperature_devices import (
    BioShakeController,
    Incubator1Controller,
    ShakingIncubatorController,
    TemperaturePositionController,
)

__version__ = "0.1.0"


@dataclasses.dataclass
class Fluent780ConnectorConfig(ConnectorBaseConfig):
    """Configuration for the combined Fluent 780 connector."""

    sila_server: SiLAServerConfig = dataclasses.field(
        default_factory=lambda: SiLAServerConfig(
            name="Tecan Fluent 780",
            type="Workcell",
            description=(
                "Devices of the RIKEN BDR Tecan Fluent 780, driven through FluentControl: Inheco ODTC 96 "
                "(door, methods, temperatures), Tecan Infinite 200 PRO (tray, absorbance, plate handling), the RGA plate "
                "gripper (moving registered labware), and BioShake, Inheco incubator, CPLC positions and Incubator1 "
                "(temperatures; BioShake clamp and shaking, Inheco incubator drawer and shaking, Incubator1 door and shaker)."
            ),
            version=__version__,
            vendor_url="https://www.riken.jp/",
            port=50060,
        )
    )
    fluent: FluentBackendConfig = dataclasses.field(default_factory=FluentBackendConfig)
    infinite: InfiniteConfig = dataclasses.field(default_factory=InfiniteConfig)
    plates: PlateTransferConfig = dataclasses.field(default_factory=PlateTransferConfig)


def build_features(config: Fluent780ConnectorConfig) -> list[sila.Feature]:
    """Build the Features of all devices, sharing one backend."""
    backend = FluentOdtcBackend(config.fluent)
    reader = FluentInfiniteReader(backend, config.infinite)
    tracker = LabwareTracker(config.plates, FluentPc(config.fluent.definitions), persist=not config.fluent.dry_run)
    return [
        # RGA
        RgaController(backend, tracker),
        # ODTC
        DoorController(backend),
        MethodController(backend),
        TemperatureController(backend),
        # Infinite 200 PRO
        TrayController(reader),
        AbsorbanceReader(reader),
        FluentPlateHandling(reader, tracker),
        # Devices with temperature control (used in 01-07)
        BioShakeController(backend),
        ShakingIncubatorController(backend),
        TemperaturePositionController(backend),
        Incubator1Controller(backend),
        # Overall
        RunController(backend),
        StatusController(backend),
    ]


async def create_app(config: Fluent780ConnectorConfig) -> collections.abc.AsyncGenerator[Connector, None]:
    """Create the connector application."""

    app = Connector(config)
    for feature in build_features(config):
        app.register(feature)
    yield app
