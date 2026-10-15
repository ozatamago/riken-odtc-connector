"""Tecan Infinite 200 PRO 用の SiLA 2 サーバ（Feature の定義は共通、裏側が 2 通り）。

    Fluent 780 の中のリーダ（FluentControl 経由）:
        uv run connector start --app riken_odtc_connector.infinite:create_app -cfg config.infinite.json
    単体のリーダ（USB、PyLabRobot）:
        uv run connector start --app riken_odtc_connector.infinite:create_usb_app -cfg config.infinite_usb.json

UniteLabs CDK はプロセス内に設定クラスが 1 つだけあることを前提にするので、起動する方の組み立てだけを読み込む。
"""


def __getattr__(name):
    if name in ("create_app", "InfiniteConnectorConfig"):
        from . import fluent_app

        return getattr(fluent_app, name)
    if name in ("create_usb_app", "InfiniteUsbConnectorConfig"):
        from . import usb_app

        return getattr(usb_app, name)
    raise AttributeError(name)
