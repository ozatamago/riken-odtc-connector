"""Fluent 780 の温度制御つき装置（BioShake、Inheco の振とうインキュベータ、inhecoMTC、Incubator1）の温度の設定。

送る XML は、01〜07 の GUI スクリプトのマクロと同じ形（tests/fixtures/gui_macros/ と 1 文字ずつ比べる）。
ExecutionSettings の値の意味は、ドライバの資料と GUI スクリプトの使われ方から判断した（2026-10-15）:

- BioShake_SetTemp           "<温度>"（例 "4"、"4.0"）。BioShake_TempOff で切る
- InhecoIncubator_SetTemp    "<温度>,0.0,0"。2 番目と 3 番目の意味は不明（スクリプトはいつも 0.0,0）。Sub_Cleanup は 25 に戻す
- inhecoMTC_SetTemperature   "<スロット>,<加熱冷却を有効にするか>,<温度>,0"。設定画面の欄は Slot / Enable Heater / Temperature。
                             False で切る（03 の最後と 06）。4 番目の意味は不明（いつも 0）
- Incubator1_SetTemperature  "0,<温度>"。05 の最後で 25 に戻す。1 番目の 0 は「全部の位置」と見られる
                             （2026-10-15、1 回送るとドライバは 6 か所それぞれに設定したと記録した）

2026-10-15 に実機で確認: BioShake 25 °C → TempOff、CPLC スロット 1 25 °C → 切る、Inheco 25 °C、Incubator1 25 °C。
どれも 8.6〜10.1 秒で返り（温度に達するまでは待たない）、ドライバは SC_ERROR_NONE を記録した。

設定できる温度の範囲は、このコネクタが安全のために絞ったもの（装置の仕様ではない）。
"""

import dataclasses

from unitelabs.cdk import sila

from .fluent_backend import BackendError, FluentOdtcBackend
from .infinite.features import _with_progress
from .infinite.xml import _esc, wrap


def legacy_macro_xml(module: str, name: str, settings: str | None = None, execution_time: str = "PT2S") -> str:
    """レガシードライバのマクロ 1 行（ODTC、Magellan と同じ形）。

    execution_time は GUI に出す所要時間の目安で、実行は待たない（ODTC の PreMethod は PT2S のまま 466 秒で完了した）。
    GUI スクリプトでは多くが PT2S、BioShake_TempOff は PT0S、Sub_Cleanup の InhecoIncubator_SetTemp は PT1M40S。
    """
    es = "<ExecutionSettings />" if settings is None else f"<ExecutionSettings>{_esc(settings)}</ExecutionSettings>"
    return wrap('<Object Type="Tecan.VisionX.ApplicationDriver.LegacyDriverMacro">\n'
                f'  <LegacyDriverMacro Version="1" Name="{name}" ModuleName="{module}"'
                f' ExecutionTime="{execution_time}" IsBreakpoint="false"'
                ' IsDisabledForExecution="false" LineNumber="0">\n'
                f'    {es}\n'
                '  </LegacyDriverMacro>\n'
                '</Object>')


def _num(value: float, decimals: int | None = None) -> str:
    return f"{value:.{decimals}f}" if decimals is not None else f"{value:g}"


def bioshake_set_temp_xml(temperature: float) -> str:
    return legacy_macro_xml("BioShake", "BioShake_SetTemp", _num(temperature))


def bioshake_temp_off_xml() -> str:
    return legacy_macro_xml("BioShake", "BioShake_TempOff", execution_time="PT0S")  # 02_PCR と同じ


def inheco_incubator_set_temp_xml(temperature: float, execution_time: str = "PT2S") -> str:
    return legacy_macro_xml("InhecoIncubator", "InhecoIncubator_SetTemp", f"{_num(temperature)},0.0,0", execution_time)


def mtc_set_temperature_xml(slot: int, enabled: bool, temperature: float) -> str:
    return legacy_macro_xml("inhecoMTC", "inhecoMTC_SetTemperature",
                            f"{int(slot)},{'True' if enabled else 'False'},{_num(temperature, 1)},0")


def incubator1_set_temperature_xml(temperature: float) -> str:
    return legacy_macro_xml("Incubator1", "Incubator1_SetTemperature", f"0,{_num(temperature)}")


class TemperatureSettingFailed(Exception):
    """The temperature could not be set. The description explains why (out of range, FluentControl busy, ...)."""


def _check(temperature: float, low: float, high: float, device: str) -> None:
    if not low <= temperature <= high:
        raise TemperatureSettingFailed(
            f"{device} に設定できる温度は {low:g}〜{high:g} °C です（このコネクタが安全のために絞った範囲）"
        )


async def _send(backend: FluentOdtcBackend, label: str, xml: str, status: sila.Status) -> None:
    try:
        await _with_progress(status, 1, lambda report: backend.run_steps([(label, xml)], report, label))
    except BackendError as exc:
        raise TemperatureSettingFailed(str(exc)) from None


class BioShakeController(sila.Feature):
    """
    Temperature of the BioShake Q1 (shaker with heating and cooling) on the Fluent 780.

    Setting the temperature does not move anything. Shaking and the plate clamp are separate (not yet available).
    """

    LOW, HIGH = 4.0, 70.0

    def __init__(self, backend: FluentOdtcBackend):
        super().__init__(originator="jp.riken.bdr", category="fluent", version="1.0")
        self.backend = backend

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def set_temperature(self, temperature: float, *, status: sila.Status) -> None:
        """
        Set the target temperature of the BioShake. Returns once the command is sent (it does not wait until the
        temperature is reached).

        Args:
          Temperature: Target temperature in degrees Celsius (4 to 70 in this connector). 02_PCR uses 4.
        """
        _check(temperature, self.LOW, self.HIGH, "BioShake")
        await _send(self.backend, f"BioShake: SetTemp {temperature:g}", bioshake_set_temp_xml(temperature), status)

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def temperature_off(self, *, status: sila.Status) -> None:
        """Switch the BioShake's temperature control off."""
        await _send(self.backend, "BioShake: TempOff", bioshake_temp_off_xml(), status)


class ShakingIncubatorController(sila.Feature):
    """
    Temperature of the Inheco Incubator Shaker DWP on the Fluent 780.

    There is no "off" command in the GUI scripts; their cleanup sets 25 degrees Celsius.
    """

    LOW, HIGH = 20.0, 60.0

    def __init__(self, backend: FluentOdtcBackend):
        super().__init__(originator="jp.riken.bdr", category="fluent", version="1.0")
        self.backend = backend

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def set_temperature(self, temperature: float, *, status: sila.Status) -> None:
        """
        Set the target temperature of the incubator. Returns once the command is sent.

        Args:
          Temperature: Target temperature in degrees Celsius (20 to 60 in this connector). 03 uses 37, cleanup 25.
        """
        _check(temperature, self.LOW, self.HIGH, "Inheco の振とうインキュベータ")
        await _send(self.backend, f"InhecoIncubator: SetTemp {temperature:g}",
                    inheco_incubator_set_temp_xml(temperature), status)


class TemperaturePositionController(sila.Feature):
    """
    Temperature of the three Inheco CPLC positions (inhecoMTC, slots 1 to 3) on the Fluent 780.

    03_Transformation uses slot 1 at 4 degrees Celsius and slots 2 and 3 at 37, and switches all three off at the end.
    """

    LOW, HIGH = 4.0, 70.0

    def __init__(self, backend: FluentOdtcBackend):
        super().__init__(originator="jp.riken.bdr", category="fluent", version="1.0")
        self.backend = backend
        self._last: dict[int, float] = {}

    @staticmethod
    def _slot(slot: int) -> int:
        if slot not in (1, 2, 3):
            raise TemperatureSettingFailed("スロットは 1〜3 です")
        return slot

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def set_temperature(self, slot: int, temperature: float, *, status: sila.Status) -> None:
        """
        Switch heating and cooling on for one position and set its target temperature. Returns once sent.

        Args:
          Slot: Position 1, 2 or 3.
          Temperature: Target temperature in degrees Celsius (4 to 70 in this connector).
        """
        self._slot(slot)
        _check(temperature, self.LOW, self.HIGH, "温度制御の台（inhecoMTC）")
        await _send(self.backend, f"inhecoMTC: slot {slot} on {temperature:.1f}",
                    mtc_set_temperature_xml(slot, True, temperature), status)
        self._last[slot] = temperature

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def turn_off(self, slot: int, *, status: sila.Status) -> None:
        """
        Switch heating and cooling off for one position.

        Args:
          Slot: Position 1, 2 or 3.
        """
        self._slot(slot)
        # 切るときも温度の欄は送る（GUI スクリプトと同じ形）。直前に設定した値、なければ 25.0
        temperature = self._last.get(slot, 25.0)
        await _send(self.backend, f"inhecoMTC: slot {slot} off", mtc_set_temperature_xml(slot, False, temperature), status)


class Incubator1Controller(sila.Feature):
    """
    Temperature of the shaking incubator "Incubator1" (6 Nest Incubator Shaking) on the Fluent 780.

    05_Subculture_and_OD_measure sets 37 at the start and 25 at the end.
    """

    LOW, HIGH = 20.0, 50.0

    def __init__(self, backend: FluentOdtcBackend):
        super().__init__(originator="jp.riken.bdr", category="fluent", version="1.0")
        self.backend = backend

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def set_temperature(self, temperature: float, *, status: sila.Status) -> None:
        """
        Set the target temperature of Incubator1. Returns once the command is sent.

        Args:
          Temperature: Target temperature in degrees Celsius (20 to 50 in this connector).
        """
        _check(temperature, self.LOW, self.HIGH, "Incubator1")
        await _send(self.backend, f"Incubator1: SetTemperature {temperature:g}",
                    incubator1_set_temperature_xml(temperature), status)
