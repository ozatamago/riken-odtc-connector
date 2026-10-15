"""ODTC の操作を、Fluent PC 上の UniteLabs FluentControl コネクタ経由で実行する裏側。

流れ（1 操作 = FluentControl のラン 1 本）
------------------------------------------
1. 状態を確認する。EditMode か RunModeRunFinished 以外なら何もせずに断る
   （他の人のランや、ダイアログで止まっているランを邪魔しない）
2. （RunModeRunFinished は空いている状態。FluentControl がメソッドを閉じ済みなので、そのまま次へ）
3. PrepareMethod("OFML_ApiHost") → RunMethod
4. 実行チャネルが開くのを待つ（OFML_ApiHost が外からの命令を受け付ける状態）
5. ExecuteCommand(ODTC マクロの XML)
6. FinishCommand → ランの終了（RunModeRunFinished）を待つ

送る XML は 2026-10-09 の qPCR 水デモ（qpcr_water_demo.py の odtc()）で実機に通したものと同一。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time

from unitelabs.cdk.config import Config

logger = logging.getLogger(__name__)

API_HOST_METHOD = "OFML_ApiHost"
IDLE_STATES = {"EditMode", "RunModeRunFinished"}
STOP_STATES = {"RunModePaused", "RunModeStopOnError", "Unknown"}

# ODTC で実機に通した実績のあるマクロだけを許可する。
# SetParameters は装置を落とす（装置側の不具合で確定）、StopMethod は unknown で拒否されるので入れない。
ALLOWED_MACROS = {"SiLA-ODTC_OpenDoor", "SiLA-ODTC_CloseDoor", "SiLA-ODTC_ExecuteMethod", "SiLA-ODTC_ReadActualTemperature"}
# ExecuteMethod の引数（メソッド名）を必須にするマクロ
MACROS_WITH_SETTINGS = {"SiLA-ODTC_ExecuteMethod"}


def discover_port(server_uuid: str, timeout_s: float = 8.0) -> int | None:
    """mDNS（_sila._tcp）を一覧して、指定 UUID の SiLA2 サーバのポートを返す。見つからなければ None。"""
    from zeroconf import ServiceBrowser, Zeroconf

    wanted = f"{server_uuid}._sila._tcp.local."
    found: dict[str, int] = {}

    class _Listener:
        def add_service(self, zc, type_, name):
            if name.lower() == wanted.lower():
                info = zc.get_service_info(type_, name, 3000)
                if info and info.port:
                    found["port"] = info.port

        def update_service(self, zc, type_, name):
            self.add_service(zc, type_, name)

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, "_sila._tcp.local.", _Listener())
        deadline = time.monotonic() + timeout_s
        while "port" not in found and time.monotonic() < deadline:
            time.sleep(0.2)
    finally:
        zc.close()
    return found.get("port")


class BackendError(Exception):
    """裏側（FluentControl）での失敗。message は人が読める日本語にする。"""


def odtc_macro_xml(name: str, settings: str | None = None) -> str:
    """ODTC のレガシードライバマクロ 1 行を、FluentControl のスクリプト片（ScriptGroup）にする。

    settings は ExecutionSettings に入る値（ExecuteMethod ならメソッド名）。XML 用に 1 段エスケープする。
    """
    if name not in ALLOWED_MACROS:
        raise BackendError(f"{name} は許可していないマクロです")
    if (name in MACROS_WITH_SETTINGS) != (settings is not None):
        raise BackendError(f"{name} の引数の指定が正しくありません")
    if settings is None:
        body = "        <ExecutionSettings />\n"
    else:
        esc = settings.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        body = f"        <ExecutionSettings>{esc}</ExecutionSettings>\n"
    return (
        "<ScriptGroup>\n"
        "  <Objects>\n"
        '    <Object Type="Tecan.VisionX.ApplicationDriver.LegacyDriverMacro">\n'
        f'      <LegacyDriverMacro Version="1" Name="{name}" ModuleName="SiLA-ODTC" ExecutionTime="PT2S" '
        'IsBreakpoint="false" IsDisabledForExecution="false" LineNumber="0">\n'
        + body
        + "      </LegacyDriverMacro>\n"
        "    </Object>\n"
        "  </Objects>\n"
        "  <Name></Name>\n"
        "  <IsBreakpoint>False</IsBreakpoint>\n"
        "  <IsDisabledForExecution>False</IsDisabledForExecution>\n"
        "  <LineNumber>0</LineNumber>\n"
        "</ScriptGroup>"
    )


@dataclasses.dataclass
class FluentBackendConfig(Config):
    """UniteLabs FluentControl コネクタの接続先。"""

    host: str = "10.5.1.156"
    port: int = 51822
    """UniteLabs コネクタのポート。設定が port 0 なので、あちらを再起動すると変わる。
    つながらないときは mDNS で unitelabs_uuid のサーバを探してポートだけ取り直す（住所は host を使う。
    あちらは mDNS で 127.0.0.1 と 169.254.* しか知らせないため）。"""
    unitelabs_uuid: str = "6f944f98-9763-4b26-9467-681cf42c9e18"
    dry_run: bool = True
    """True のあいだは XML を組み立ててログに出すだけで、Fluent には何も送らない。"""
    dry_run_seconds: float = 0.0
    """ドライランで、送ったふりをして待つ秒数（進捗の届き方を確かめる用）。"""
    ssh_target: str = "Tecan@10.5.1.156"
    """温度を FluentControl のログから読むための SSH の接続先（鍵認証で入れること）。"""
    sensor_script: str = r"C:\Users\Tecan\OFML_screen\odtc_last_sensor_values.ps1"
    """Fluent PC に置いた読み取りスクリプト（windows/odtc_last_sensor_values.ps1）の場所。"""
    channel_timeout_s: float = 300.0
    finish_timeout_s: float = 180.0
    api_channel: str = "ofml"
    """OFML_ApiHost が開く窓口の名前。この名前の窓口が開いてから命令を送る。"""


class FluentOdtcBackend:
    def __init__(self, config: FluentBackendConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._client = None
        self.current_operation = ""
        """今実行中の操作の説明（例 "ODTC: SiLA-ODTC_OpenDoor"）。空なら何もしていない。"""

    # ---- 同期の SiLA2 クライアント（別スレッドで呼ぶ）
    def _c(self):
        if self._client is None:
            from sila2.client import SilaClient

            try:
                self._client = SilaClient(self.config.host, self.config.port, insecure=True)
            except Exception as first:  # noqa: BLE001 - つながらなければ mDNS でポートを探し直す
                port = discover_port(self.config.unitelabs_uuid)
                if port is None or port == self.config.port:
                    raise BackendError(f"UniteLabs コネクタ（{self.config.host}:{self.config.port}）につながりません: {first}") from None
                logger.info("UniteLabs コネクタのポートが %s から %s に変わっていました", self.config.port, port)
                self.config.port = port
                self._client = SilaClient(self.config.host, port, insecure=True)
        return self._client

    def _state(self) -> str:
        prop = self._c().RuntimeController.State
        if hasattr(prop, "get"):
            return str(prop.get())
        sub = prop.subscribe()
        try:
            return str(next(iter(sub)))
        finally:
            sub.cancel()

    def _our_channel_open(self) -> bool:
        c = self._c()
        try:
            return c.ExecutionChannelController.Channel.get() == self.config.api_channel and bool(
                c.ExecutionChannelController.IsAlive.get()
            )
        except Exception:  # noqa: BLE001 - 窓口がまだ無いと例外になる
            return False

    def _active_dialogs(self) -> list[str]:
        try:
            return [f"{d.DialogIdentifier}: {d.Description.strip()}" for d in self._c().RunController.ActiveDialogs.get()]
        except Exception:  # noqa: BLE001 - 読めなくても本処理は続ける
            return []

    def _wait(self, predicate, timeout: float, what: str, report) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self._state()
            if state in STOP_STATES:
                dialogs = self._active_dialogs()
                raise BackendError(f"{what}の途中で FluentControl が {state} になりました。{' / '.join(dialogs)}")
            if predicate(state):
                return state
            report(state)
            time.sleep(2)
        raise BackendError(f"{what}が {timeout:.0f} 秒以内に終わりませんでした（状態 {self._state()}）")

    def _run_macro(self, name: str, settings: str | None, report) -> float:
        return self._run_steps([(name, odtc_macro_xml(name, settings))], report)[0]

    def _run_steps(self, steps: list[tuple[str, str]], report) -> list[float]:
        """XML の命令を、FluentControl のラン 1 本の中で順に送る。各命令にかかった秒数を返す。

        途中の命令が失敗したら、残りは送らずにランを終わらせる。report には、各命令の前に
        その label が渡る（進捗の表示用）。
        """
        if self.config.dry_run:
            for label, xml in steps:
                logger.info("dry-run: %s を送るところでした\n%s", label, xml)
                report("dry-run")
                report(label)
            time.sleep(self.config.dry_run_seconds)
            return [0.0 for _ in steps]

        c = self._c()
        if not c.FluentController.IsAttached.get():
            raise BackendError("UniteLabs コネクタが FluentControl につながっていません")
        state = self._state()
        # State は UniteLabs コネクタが受け取った最後の状態変化の通知。2026-10-15 に「古い値」と誤認したが、
        # 実際は起動時のダイアログで FluentControl が StartUp 段階に止まっていた。IsReady では判断しない
        if state not in IDLE_STATES:
            dialogs = self._active_dialogs()
            raise BackendError(
                f"FluentControl が {state} なので始めません（他のランやダイアログを確認してください）"
                + (f"。表示中のダイアログ: {' / '.join(dialogs)}" if dialogs else "")
            )

        report("PrepareMethod")
        c.RuntimeController.PrepareMethod(API_HOST_METHOD)
        c.RuntimeController.RunMethod()
        deadline = time.monotonic() + self.config.channel_timeout_s
        # UniteLabs コネクタは「最後に開いた窓口」を返す。別のクライアントが開いた窓口の記録が残っていると
        # IsAlive が最初から True になる（2026-10-15、並行試験の main が残った）ので、窓口の名前でも確かめる
        while not self._our_channel_open():
            state = self._state()
            if state in STOP_STATES:
                raise BackendError(f"ランの開始で {state} になりました。{' / '.join(self._active_dialogs())}")
            if state == "RunModeQueryVariables":
                c.RuntimeController.CloseQueryAtStartupDialog(True)
            if time.monotonic() > deadline:
                raise BackendError(f"実行チャネルが開きませんでした（状態 {state}）。{' / '.join(self._active_dialogs())}")
            report(f"waiting channel ({state})")
            time.sleep(2)

        durations: list[float] = []
        try:
            for label, xml in steps:
                report(label)
                t0 = time.monotonic()
                c.ExecutionChannelController.ExecuteCommand(xml)
                durations.append(time.monotonic() - t0)
        finally:
            # 失敗しても必ずランを終わらせ、EditMode に戻す
            try:
                c.ExecutionChannelController.FinishCommand()
                # ランが終わると FluentControl は自分でメソッドを閉じて RunModeRunFinished になる。
                # CloseMethod は "No method loaded to be closed." で断られるので送らない（2026-10-15 に確認）
                self._wait(lambda s: s in IDLE_STATES, self.config.finish_timeout_s, "ランの終了", report)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ランの後始末で失敗: %s", exc)
        return durations

    # ---- Feature から呼ぶ入口
    async def run_macro(self, name: str, settings: str | None = None, report=lambda _: None) -> float:
        """ODTC マクロを 1 本実行し、マクロ自体にかかった秒数を返す。同時には 1 本だけ。"""
        async with self._exclusive(f"ODTC: {name}" + (f" {settings}" if settings else "")):
            return await asyncio.to_thread(self._guarded, self._run_macro, name, settings, report)

    async def run_steps(self, steps: list[tuple[str, str]], report=lambda _: None, what: str = "") -> list[float]:
        """複数の XML の命令を、ラン 1 本の中で順に送る（搬入・測定・搬出を 1 本で済ませる用）。"""
        what = what or (steps[0][0] if len(steps) == 1 else f"{steps[0][0]} ほか {len(steps) - 1} 命令")
        async with self._exclusive(what):
            return await asyncio.to_thread(self._guarded, self._run_steps, steps, report)

    @contextlib.asynccontextmanager
    async def _exclusive(self, what: str):
        """FluentControl に命令を送る操作は、このサーバの中で同時に 1 つだけ。断るときは何が実行中かを伝える。"""
        if self._lock.locked():
            raise BackendError(f"別の操作が実行中です（{self.current_operation}）")
        async with self._lock:
            self.current_operation = what
            try:
                yield
            finally:
                self.current_operation = ""

    # ---- 温度（ReadActualTemperature の結果は FluentControl に返らず、ドライバのログにだけ残る）
    def _ssh_ps(self, args: str) -> str:
        import subprocess

        cmd = ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", self.config.ssh_target,
               f"powershell -NoProfile -ExecutionPolicy Bypass {args}"]
        out = subprocess.run(cmd, capture_output=True, timeout=120)
        if out.returncode != 0:
            raise BackendError(f"Fluent PC への SSH に失敗しました: {out.stderr.decode(errors='replace')[:200]}")
        return out.stdout.decode("utf-8", errors="replace").replace("\r", "")

    def _read_sensor_values(self, report) -> dict[str, float | str]:
        import html
        import re

        if self.config.dry_run:
            self._run_macro("SiLA-ODTC_ReadActualTemperature", None, report)
            return {"timestamp": "dry-run"}
        since = self._ssh_ps('-Command "Get-Date -Format \'yyyy-MM-dd HH:mm:ss.fff\'"').strip()
        self._run_macro("SiLA-ODTC_ReadActualTemperature", None, report)
        for _ in range(10):  # ログへの書き込みが少し遅れることがある
            out = self._ssh_ps(f'-File "{self.config.sensor_script}" -Since "{since}"')
            if out.startswith("LOGTIME"):
                text = out
                for _ in range(4):
                    text = html.unescape(text)
                m = re.search(r'<SensorValues timestamp="([^"]+)">(.*?)</SensorValues>', text, re.S)
                if m:
                    values: dict[str, float | str] = {"timestamp": m.group(1)}
                    values.update({k: int(v) / 100 for k, v in re.findall(r"<(\w+)>(-?\d+)</\1>", m.group(2))})
                    return values
            time.sleep(2)
        raise BackendError("温度の記録が FluentControl のログに見つかりませんでした")

    async def read_sensor_values(self, report=lambda _: None) -> dict[str, float | str]:
        """ReadActualTemperature を送り、結果を FluentControl のログから読む。値は degC。"""
        async with self._exclusive("ODTC: 温度の読み取り"):
            return await asyncio.to_thread(self._guarded, self._read_sensor_values, report)

    def _guarded(self, fn, *args):
        """裏側の呼び出しで想定外の例外が出たら、接続を捨てて（次回作り直す）BackendError にする。"""
        try:
            return fn(*args)
        except BackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._client = None
            raise BackendError(f"UniteLabs コネクタとの通信に失敗しました: {type(exc).__name__}: {str(exc)[:300]}") from None

    # ---- 状態（装置は動かさない）
    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def _status(self) -> tuple[str, list[str]]:
        return self._state(), self._active_dialogs()

    async def fluent_status(self) -> tuple[str, list[str]]:
        """FluentControl の状態（UniteLabs コネクタが受け取った最後の通知）と、表示中のダイアログ。"""
        return await asyncio.to_thread(self._guarded, self._status)
