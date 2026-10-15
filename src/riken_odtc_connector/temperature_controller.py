import dataclasses

from unitelabs.cdk import sila

from .door_controller import OdtcOperationFailed
from .fluent_backend import BackendError, FluentOdtcBackend


@dataclasses.dataclass
class OdtcTemperatures(sila.CustomDataType):
    """
    Temperature sensor values of the ODTC, in degrees Celsius.

    Args:
      Timestamp: Device time of the reading as reported by the ODTC (its clock runs on local time although the
        value ends with "Z").
      Block: Block (mount) temperature.
      BlockMonitor: Second block sensor.
      Lid: Heated lid temperature.
      LidMonitor: Second lid sensor.
      Ambient: Ambient temperature inside the device.
      Pcb: Electronics board temperature.
      Heatsink: Heatsink temperature.
      HeatsinkTec: Heatsink temperature at the thermoelectric element.
    """

    timestamp: str
    block: float
    block_monitor: float
    lid: float
    lid_monitor: float
    ambient: float
    pcb: float
    heatsink: float
    heatsink_tec: float


class TemperatureController(sila.Feature):
    """
    Read the actual temperatures of the Inheco ODTC 96.

    FluentControl does not hand the ODTC's answer back to its API, so the connector sends ReadActualTemperature
    and then reads the answer from the FluentControl driver log on the Fluent PC over SSH. A reading takes about
    15 seconds and cannot run while another ODTC operation (door, PreMethod, Method) is running.
    """

    def __init__(self, backend: FluentOdtcBackend):
        super().__init__(originator="jp.riken.bdr", category="odtc", version="1.0")
        self.backend = backend

    @sila.ObservableCommand(errors=[OdtcOperationFailed])
    async def read_temperatures(self, *, status: sila.Status) -> OdtcTemperatures:
        """
        Read the ODTC's temperature sensors once. Nothing moves and nothing is heated.

        Returns:
          Temperatures: The sensor values in degrees Celsius (all 0 in dry-run mode).
        """
        status.update(progress=0.0)
        try:
            v = await self.backend.read_sensor_values()
        except BackendError as exc:
            raise OdtcOperationFailed(str(exc)) from None
        status.update(progress=1.0)
        return OdtcTemperatures(
            timestamp=str(v.get("timestamp", "")),
            block=float(v.get("Mount", 0.0)),
            block_monitor=float(v.get("Mount_Monitor", 0.0)),
            lid=float(v.get("Lid", 0.0)),
            lid_monitor=float(v.get("Lid_Monitor", 0.0)),
            ambient=float(v.get("Ambient", 0.0)),
            pcb=float(v.get("PCB", 0.0)),
            heatsink=float(v.get("Heatsink", 0.0)),
            heatsink_tec=float(v.get("Heatsink_TEC", 0.0)),
        )
