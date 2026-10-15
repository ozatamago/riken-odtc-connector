"""SiLA 2 connector for the Tecan Infinite 200 PRO in the RIKEN BDR Fluent 780 (driven through FluentControl).

    uv run connector start --app riken_odtc_connector.infinite:create_app -cfg config.infinite.json
"""

import collections.abc
import dataclasses

from unitelabs.cdk import Connector, ConnectorBaseConfig, SiLAServerConfig

from ..fluent_backend import FluentBackendConfig, FluentOdtcBackend
from .features import AbsorbanceReader, FluentPlateHandling, TrayController
from .reader import FluentInfiniteReader, InfiniteConfig

__version__ = "0.1.0"


@dataclasses.dataclass
class InfiniteConnectorConfig(ConnectorBaseConfig):
    """Configuration for the Infinite 200 PRO connector."""

    sila_server: SiLAServerConfig = dataclasses.field(
        default_factory=lambda: SiLAServerConfig(
            name="Tecan Infinite 200 PRO (Fluent 780)",
            type="PlateReader",
            description="Control the Tecan Infinite 200 PRO plate reader integrated in the Tecan Fluent 780.",
            version=__version__,
            vendor_url="https://www.riken.jp/",
            port=50063,
        )
    )
    fluent: FluentBackendConfig = dataclasses.field(default_factory=FluentBackendConfig)
    infinite: InfiniteConfig = dataclasses.field(default_factory=InfiniteConfig)


async def create_app(config: InfiniteConnectorConfig) -> collections.abc.AsyncGenerator[Connector, None]:
    """Create the connector application."""

    app = Connector(config)
    reader = FluentInfiniteReader(FluentOdtcBackend(config.fluent), config.infinite)
    app.register(TrayController(reader))
    app.register(AbsorbanceReader(reader))
    app.register(FluentPlateHandling(reader))
    yield app
