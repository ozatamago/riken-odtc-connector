"""SiLA 2 connectors for the RIKEN BDR Fluent 780, driven through FluentControl.

    ODTC:     uv run connector start --app riken_odtc_connector:create_app -cfg config.json
    Infinite: uv run connector start --app riken_odtc_connector.infinite:create_app -cfg config.infinite.json

UniteLabs CDK は、プロセス内に ConnectorBaseConfig の子クラスが 1 つだけあることを前提にする。
ODTC の設定クラスは、ODTC のサーバを起動するときだけ読み込む（Infinite のサーバと同居させないため）。
"""

__version__ = "0.1.0"


def __getattr__(name):
    if name in ("create_app", "OdtcConnectorConfig"):
        from . import odtc_app

        return getattr(odtc_app, name)
    raise AttributeError(name)
