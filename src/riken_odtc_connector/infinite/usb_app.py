"""Server assembly for a stand-alone Infinite 200 PRO (USB, PyLabRobot).

The features are the same as in the Fluent 780 Infinite server (TrayController / AbsorbanceReader).
FluentPlateHandling (RGA transfers) is not included.
"""

import collections.abc
import dataclasses

from unitelabs.cdk import Connector, ConnectorBaseConfig, SiLAServerConfig

from .features import AbsorbanceReader, TrayController
from .plr_reader import PyLabRobotInfiniteReader, UsbInfiniteConfig

__version__ = "0.1.0"


@dataclasses.dataclass
class InfiniteUsbConnectorConfig(ConnectorBaseConfig):
    """Configuration for a stand-alone Infinite 200 PRO connected over USB."""

    sila_server: SiLAServerConfig = dataclasses.field(
        default_factory=lambda: SiLAServerConfig(
            name="Tecan Infinite 200 PRO (USB)",
            type="PlateReader",
            description="Control a stand-alone Tecan Infinite 200 PRO plate reader over USB (PyLabRobot backend).",
            version=__version__,
            vendor_url="https://www.riken.jp/",
            port=50065,
        )
    )
    reader: UsbInfiniteConfig = dataclasses.field(default_factory=UsbInfiniteConfig)


async def create_usb_app(config: InfiniteUsbConnectorConfig) -> collections.abc.AsyncGenerator[Connector, None]:
    """Create the connector application."""

    app = Connector(config)
    reader = PyLabRobotInfiniteReader(config.reader)
    app.register(TrayController(reader))
    app.register(AbsorbanceReader(reader))
    yield app
    await reader.stop()
