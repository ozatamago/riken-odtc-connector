"""Tests for the stand-alone Infinite backend (USB, PyLabRobot). No reader is connected; the PyLabRobot backend is
replaced by a fake."""

import asyncio
import math

import pytest

from riken_odtc_connector.fluent_backend import BackendError
from riken_odtc_connector.infinite.plr_reader import PyLabRobotInfiniteReader, UsbInfiniteConfig


class FakeBackend:
    """Fake with only the methods of ExperimentalTecanInfinite200ProBackend (same names and arguments)."""

    def __init__(self):
        self.calls = []

    async def open(self):
        self.calls.append("open")

    async def close(self, plate):
        self.calls.append(("close", plate.name))

    async def read_absorbance(self, plate, wells, wavelength, flashes=25, bandwidth=None):
        self.calls.append(("read_absorbance", wavelength, flashes, len(wells)))
        data = [[round(0.01 * (r * 12 + c), 4) for c in range(12)] for r in range(8)]
        data[7][11] = None  # a well that returned no value
        return [{"wavelength": wavelength, "time": 0.0, "temperature": None, "data": data}]

    async def stop(self):
        self.calls.append("stop")


def make_reader():
    import pylabrobot.resources as resources

    reader = PyLabRobotInfiniteReader(UsbInfiniteConfig(dry_run=False))
    reader._backend = FakeBackend()
    reader._plate = getattr(resources, reader.config.plate_model)(name="plate")
    return reader


def run(coro):
    return asyncio.run(coro)


def test_tray_calls_open_and_close_with_the_plate():
    reader = make_reader()
    run(reader.open_tray(lambda _: None))
    run(reader.close_tray(lambda _: None))
    assert reader._backend.calls == ["open", ("close", "plate")]


def test_read_absorbance_maps_rows_and_columns():
    reader = make_reader()
    path, result = run(reader.read_absorbance(600, lambda _: None))
    assert path == ""
    assert result.labels == ["600nm"]
    g = result.values[0]
    assert len(g) == 96
    assert g["A1"] == 0.0 and g["A12"] == 0.11 and g["B1"] == 0.12 and g["H11"] == 0.94
    assert math.isnan(g["H12"])
    # wells=[] means all wells of the plate (plate.get_all_items() inside PyLabRobot)
    assert reader._backend.calls == [("read_absorbance", 600, 25, 0)]


@pytest.mark.parametrize("wavelength", [229, 1001])
def test_wavelength_out_of_range_is_refused(wavelength):
    with pytest.raises(BackendError):
        run(make_reader().read_absorbance(wavelength, lambda _: None))


def test_magellan_methods_are_not_available_over_usb():
    reader = make_reader()
    assert run(reader.list_methods()) == []
    with pytest.raises(BackendError):
        run(reader.measure_loaded("Abs600.mth", lambda _: None))


def test_dry_run_does_not_touch_usb():
    reader = PyLabRobotInfiniteReader(UsbInfiniteConfig(dry_run=True))
    _, result = run(reader.read_absorbance(450, lambda _: None))
    assert reader._backend is None
    assert all(math.isnan(v) for v in result.values[0].values())


def test_plate_model_exists_in_pylabrobot():
    import pylabrobot.resources as resources

    plate = getattr(resources, UsbInfiniteConfig().plate_model)(name="p")
    assert plate.num_items_x == 12 and plate.num_items_y == 8
