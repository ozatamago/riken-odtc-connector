"""SiLA 2 connectors for the RIKEN BDR Fluent 780, driven through FluentControl.

    ODTC:     uv run connector start --app riken_odtc_connector:create_app -cfg config.json
    Infinite: uv run connector start --app riken_odtc_connector.infinite:create_app -cfg config.infinite.json

The UniteLabs CDK assumes there is only one subclass of ConnectorBaseConfig in the process.
The ODTC config class is imported only when the ODTC server starts (so it does not coexist with the Infinite server).
"""

__version__ = "0.1.0"


def __getattr__(name):
    if name in ("create_app", "OdtcConnectorConfig"):
        from . import odtc_app

        return getattr(odtc_app, name)
    raise AttributeError(name)
