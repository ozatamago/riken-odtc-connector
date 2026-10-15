"""SiLA 2 servers for the Tecan Infinite 200 PRO (shared feature definitions, two backends).

    Reader inside the Fluent 780 (through FluentControl):
        uv run connector start --app riken_odtc_connector.infinite:create_app -cfg config.infinite.json
    Stand-alone reader (USB, PyLabRobot):
        uv run connector start --app riken_odtc_connector.infinite:create_usb_app -cfg config.infinite_usb.json

The UniteLabs CDK assumes a single config class per process, so only the app being started is imported.
"""


def __getattr__(name):
    if name in ("create_app", "InfiniteConnectorConfig"):
        from . import fluent_app

        return getattr(fluent_app, name)
    if name in ("create_usb_app", "InfiniteUsbConnectorConfig"):
        from . import usb_app

        return getattr(usb_app, name)
    raise AttributeError(name)
