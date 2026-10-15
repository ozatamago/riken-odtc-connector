"""Fluent 780 の装置をまとめた 1 つの SiLA 2 サーバ「Tecan Fluent 780」。

    uv run connector start --app riken_odtc_connector.fluent780:create_app -cfg config.fluent780.json

FluentControl に命令を送る裏側（FluentOdtcBackend）を 1 つだけ作り、すべての Feature に渡す。
裏側は「同時に 1 つの操作だけ」を守るので、ODTC と Infinite の操作が自動的に順番待ちになり、
別のサーバが他人のランの窓口を終わらせてしまうこともない（2026-10-15、ODTC と Infinite のサーバを統合）。

Feature の名前と中身は、ODTC 用・Infinite 用のサーバと同じ。呼ぶ側は接続先を変えるだけでよい。
これから足す装置の Feature には、装置名を頭に付ける（例 BioShakeController）。名前の重なりを避けるため。
"""

import collections.abc
import dataclasses

from unitelabs.cdk import Connector, ConnectorBaseConfig, SiLAServerConfig, sila

from .door_controller import DoorController
from .fluent_backend import FluentBackendConfig, FluentOdtcBackend
from .infinite.features import AbsorbanceReader, FluentPlateHandling, TrayController
from .infinite.reader import FluentInfiniteReader, InfiniteConfig
from .method_controller import MethodController
from .rga_controller import LabwareTracker, PlateTransferConfig, RgaController
from .status_controller import StatusController
from .temperature_controller import TemperatureController

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
                "(door, methods, temperatures), Tecan Infinite 200 PRO (tray, absorbance, plate handling) and the RGA plate "
                "gripper (moving registered labware)."
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
    """全装置の Feature を、1 つの裏側を共有する形で作る。"""
    backend = FluentOdtcBackend(config.fluent)
    reader = FluentInfiniteReader(backend, config.infinite)
    tracker = LabwareTracker(config.plates)
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
        # 全体
        StatusController(backend),
    ]


async def create_app(config: Fluent780ConnectorConfig) -> collections.abc.AsyncGenerator[Connector, None]:
    """Create the connector application."""

    app = Connector(config)
    for feature in build_features(config):
        app.register(feature)
    yield app
