"""Temperature control of the Fluent 780's heated/cooled devices (BioShake, Inheco shaking incubator, inhecoMTC,
Incubator1).

The XML sent has the same form as the macros in GUI scripts 01-07 (compared character by character with
tests/fixtures/gui_macros/). The meaning of the ExecutionSettings values was inferred from the driver documentation and
from how the GUI scripts use them (2026-10-15):

- BioShake_SetTemp           "<temperature>" (e.g. "4", "4.0"). Switched off with BioShake_TempOff
- InhecoIncubator_SetTemp    "<temperature>,0.0,0". The 2nd and 3rd values are unknown (scripts always use 0.0,0).
                             Sub_Cleanup sets it back to 25
- inhecoMTC_SetTemperature   "<slot>,<enable heating/cooling>,<temperature>,0". The settings dialog fields are
                             Slot / Enable Heater / Temperature. False switches it off (end of 03, and 06).
                             The 4th value is unknown (always 0)
- Incubator1_SetTemperature  "0,<temperature>". 05 sets it back to 25 at the end. The leading 0 appears to mean
                             "all positions" (2026-10-15: after one call the driver logged a setting for each of
                             the 6 positions)

Verified on the device on 2026-10-15: BioShake 25 °C -> TempOff, CPLC slot 1 25 °C -> off, Inheco 25 °C,
Incubator1 25 °C. Each returned in 8.6-10.1 s (it does not wait for the temperature to be reached), and the driver
logged SC_ERROR_NONE.

The allowed temperature ranges are limits this connector imposes for safety (not the device specifications).
"""

import dataclasses

from unitelabs.cdk import sila

from .fluent_backend import BackendError, FluentOdtcBackend
from .infinite.features import _with_progress
from .infinite.xml import _esc, wrap


def legacy_macro_xml(module: str, name: str, settings: str | None = None, execution_time: str = "PT2S") -> str:
    """One legacy driver macro line (same form as for the ODTC and Magellan).

    execution_time is only the duration estimate shown in the GUI; execution does not wait for it (the ODTC PreMethod
    completed in 466 s with PT2S). In the GUI scripts most use PT2S, BioShake_TempOff uses PT0S, and the
    InhecoIncubator_SetTemp in Sub_Cleanup uses PT1M40S.
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
    return legacy_macro_xml("BioShake", "BioShake_TempOff", execution_time="PT0S")  # same as 02_PCR


def inheco_incubator_set_temp_xml(temperature: float, execution_time: str = "PT2S") -> str:
    return legacy_macro_xml("InhecoIncubator", "InhecoIncubator_SetTemp", f"{_num(temperature)},0.0,0", execution_time)


def mtc_set_temperature_xml(slot: int, enabled: bool, temperature: float) -> str:
    return legacy_macro_xml("inhecoMTC", "inhecoMTC_SetTemperature",
                            f"{int(slot)},{'True' if enabled else 'False'},{_num(temperature, 1)},0")


def incubator1_set_temperature_xml(temperature: float) -> str:
    return legacy_macro_xml("Incubator1", "Incubator1_SetTemperature", f"0,{_num(temperature)}")


# ---- Moving operations (stage 3). Meaning of the values:
#   BioShake_ShakeOn          "<speed rpm>,5" (Sub_Skake). The GUI input offers speeds 1000/1200/.../2000 as choices.
#                             The manual ("start shaking with speed and acceleration") suggests 5 is the acceleration,
#                             but this is unconfirmed, so it is fixed
#   BioShake_ShakeOff         Stops shaking, returns to the home position and locks there (manual)
#   BioShake_ElmLock/Unlock   Closes / opens the plate clamp (Edge Locking Mechanism)
#   InhecoIncubator_ShakeOn   "14,1.0,14,1.0,180" (03). The meaning is unknown, so only this one setting is sent
#   Incubator1_StartShaker    "<frequency Hz>". The driver accepts 1.0-8.5 Hz (string in the driver binary). 05 uses 5
BIOSHAKE_SPEEDS = (1000, 1200, 1400, 1600, 1800, 2000)
INHECO_SHAKE_PRESET = "14,1.0,14,1.0,180"


def bioshake_macro_xml(name: str) -> str:
    if name not in ("BioShake_ElmLock", "BioShake_ElmUnlock", "BioShake_ShakeOff"):
        raise BackendError(f"{name} is not an allowed BioShake macro")
    return legacy_macro_xml("BioShake", name)


def bioshake_shake_on_xml(speed_rpm: int) -> str:
    return legacy_macro_xml("BioShake", "BioShake_ShakeOn", f"{int(speed_rpm)},5")


def inheco_incubator_macro_xml(name: str) -> str:
    if name not in ("InhecoIncubator_Close", "InhecoIncubator_ShakeOff"):
        raise BackendError(f"{name} is not an allowed Inheco incubator macro")
    return legacy_macro_xml("InhecoIncubator", name)


def inheco_incubator_shake_on_xml() -> str:
    return legacy_macro_xml("InhecoIncubator", "InhecoIncubator_ShakeOn", INHECO_SHAKE_PRESET)


def incubator1_macro_xml(name: str) -> str:
    if name not in ("Incubator1_CloseDoor", "Incubator1_StopShaker"):
        raise BackendError(f"{name} is not an allowed Incubator1 macro")
    return legacy_macro_xml("Incubator1", name)


def incubator1_start_shaker_xml(frequency_hz: float) -> str:
    return legacy_macro_xml("Incubator1", "Incubator1_StartShaker", _num(frequency_hz))


class TemperatureSettingFailed(Exception):
    """The temperature could not be set. The description explains why (out of range, FluentControl busy, ...)."""


def _check(temperature: float, low: float, high: float, device: str) -> None:
    if not low <= temperature <= high:
        raise TemperatureSettingFailed(
            f"{device} accepts {low:g} to {high:g} °C (a range this connector limits for safety)"
        )


async def _send(backend: FluentOdtcBackend, label: str, xml: str, status: sila.Status) -> None:
    try:
        await _with_progress(status, 1, lambda report: backend.run_steps([(label, xml)], report, label))
    except BackendError as exc:
        raise TemperatureSettingFailed(str(exc)) from None


class BioShakeController(sila.Feature):
    """
    Temperature of the BioShake Q1 (shaker with heating and cooling) on the Fluent 780.

    Setting the temperature does not move anything. LockPlate / UnlockPlate / ShakeOn / ShakeOff MOVE the device.
    ShakeOn keeps shaking until ShakeOff is sent.
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

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def lock_plate(self, *, status: sila.Status) -> None:
        """Close the plate clamp (Edge Locking Mechanism). MOVES: keep fingers away."""
        await _send(self.backend, "BioShake: ElmLock", bioshake_macro_xml("BioShake_ElmLock"), status)

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def unlock_plate(self, *, status: sila.Status) -> None:
        """Open the plate clamp (Edge Locking Mechanism). MOVES: keep fingers away."""
        await _send(self.backend, "BioShake: ElmUnlock", bioshake_macro_xml("BioShake_ElmUnlock"), status)

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def shake_on(self, speed: int, *, status: sila.Status) -> None:
        """
        Start shaking. It keeps shaking until ShakeOff. MOVES. Lock the plate first if one is on the shaker.

        Args:
          Speed: Shaking speed in rpm: 1000, 1200, 1400, 1600, 1800 or 2000 (the choices of the GUI scripts).
        """
        if speed not in BIOSHAKE_SPEEDS:
            raise TemperatureSettingFailed(f"Speed must be one of {', '.join(map(str, BIOSHAKE_SPEEDS))} rpm (the GUI choices)")
        await _send(self.backend, f"BioShake: ShakeOn {speed} rpm", bioshake_shake_on_xml(speed), status)

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def shake_off(self, *, status: sila.Status) -> None:
        """Stop shaking; the shaker returns to its home position and locks there. MOVES."""
        await _send(self.backend, "BioShake: ShakeOff", bioshake_macro_xml("BioShake_ShakeOff"), status)


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
        _check(temperature, self.LOW, self.HIGH, "The Inheco shaking incubator")
        await _send(self.backend, f"InhecoIncubator: SetTemp {temperature:g}",
                    inheco_incubator_set_temp_xml(temperature), status)

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def close(self, *, status: sila.Status) -> None:
        """Close the incubator drawer. MOVES: keep hands and plates out of the way."""
        await _send(self.backend, "InhecoIncubator: Close", inheco_incubator_macro_xml("InhecoIncubator_Close"), status)

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def shake_on(self, *, status: sila.Status) -> None:
        """
        Start shaking with the setting used by 03_Transformation ("14,1.0,14,1.0,180"; the meaning of the values is
        not documented, so only this setting is offered). It keeps shaking until ShakeOff. MOVES.
        """
        await _send(self.backend, "InhecoIncubator: ShakeOn", inheco_incubator_shake_on_xml(), status)

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def shake_off(self, *, status: sila.Status) -> None:
        """Stop shaking. MOVES."""
        await _send(self.backend, "InhecoIncubator: ShakeOff", inheco_incubator_macro_xml("InhecoIncubator_ShakeOff"), status)


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
            raise TemperatureSettingFailed("Slot must be 1 to 3")
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
        _check(temperature, self.LOW, self.HIGH, "The temperature position (inhecoMTC)")
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
        # Switching off still sends a temperature (same form as the GUI scripts): the last set value, else 25.0
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

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def close_door(self, *, status: sila.Status) -> None:
        """Close the Incubator1 door. MOVES: keep hands out of the way."""
        await _send(self.backend, "Incubator1: CloseDoor", incubator1_macro_xml("Incubator1_CloseDoor"), status)

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def start_shaker(self, frequency: float, *, status: sila.Status) -> None:
        """
        Start shaking. It keeps shaking until StopShaker. MOVES.

        Args:
          Frequency: Shaking frequency in Hz, 1.0 to 8.5 (the driver's range). 05 uses 5.
        """
        if not 1.0 <= frequency <= 8.5:
            raise TemperatureSettingFailed("Incubator1 shaking must be 1.0 to 8.5 Hz (the driver's range)")
        await _send(self.backend, f"Incubator1: StartShaker {frequency:g} Hz", incubator1_start_shaker_xml(frequency), status)

    @sila.ObservableCommand(errors=[TemperatureSettingFailed])
    async def stop_shaker(self, *, status: sila.Status) -> None:
        """Stop shaking. MOVES."""
        await _send(self.backend, "Incubator1: StopShaker", incubator1_macro_xml("Incubator1_StopShaker"), status)
