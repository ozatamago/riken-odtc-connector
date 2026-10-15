"""ODTC コネクタのサーバの組み立て（設定と create_app）。"""

import collections.abc
import dataclasses

from unitelabs.cdk import Connector, ConnectorBaseConfig, SiLAServerConfig

from .door_controller import DoorController
from .fluent_backend import FluentBackendConfig, FluentOdtcBackend
from .method_controller import MethodController
from .status_controller import StatusController
from .temperature_controller import TemperatureController

__version__ = "0.1.0"


@dataclasses.dataclass
class OdtcConnectorConfig(ConnectorBaseConfig):
    """Configuration for the ODTC connector."""

    sila_server: SiLAServerConfig = dataclasses.field(
        default_factory=lambda: SiLAServerConfig(
            name="Inheco ODTC (Fluent 780)",
            type="ThermalCycler",
            description="Control the Inheco ODTC 96 thermal cycler integrated in the Tecan Fluent 780.",
            version=__version__,
            vendor_url="https://www.riken.jp/",
            port=50061,
        )
    )
    fluent: FluentBackendConfig = dataclasses.field(default_factory=FluentBackendConfig)


async def create_app(config: OdtcConnectorConfig) -> collections.abc.AsyncGenerator[Connector, None]:
    """Create the connector application."""

    app = Connector(config)
    backend = FluentOdtcBackend(config.fluent)  # 扉もメソッドも同じ裏側を使い、同時には 1 操作だけ
    app.register(DoorController(backend))
    app.register(MethodController(backend))
    app.register(TemperatureController(backend))
    app.register(StatusController(backend))
    yield app
