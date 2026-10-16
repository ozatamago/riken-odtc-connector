# riken-odtc-connector

A SiLA 2 connector for the devices built into the Tecan Fluent 780 at RIKEN BDR. Built with the [UniteLabs CDK](https://docs.unitelabs.io/connector-development/); it drives the devices through FluentControl.

## Start here: the combined server "Tecan Fluent 780" (since 2026-10-15)

```bash
cp config.fluent780.example.json config.fluent780.json   # set fluent.dry_run to false to drive the real instrument
uv run connector start --app riken_odtc_connector.fluent780:create_app -cfg config.fluent780.json
```

This puts the ODTC and Infinite 200 PRO Features (sections below) on a single server (port 50060), with a single back end that sends commands to FluentControl.
It enforces **one operation at a time**, so an operation on another device is rejected with "Another operation is running (...)".
`StatusController.CurrentOperation` shows what is currently running.

The per-device servers (ODTC on 50061, Infinite on 50063) are still available, but **do not run them at the same time as this server**
(they do not know about each other's operations and could end someone else's run through the UniteLabs connector's "last opened channel").

Features for newly added devices are prefixed with the device name (e.g. `BioShakeController`). The ODTC's `TemperatureController` keeps its original name.

### RGA (`RgaController`, this server only)

| Command / Property | Description |
|---|---|
| `LabwarePositions` (P) | Labware that may be moved, and the current position the connector remembers for each |
| `MoveLabware(Labware, Location, Site)` | Move to the given position (e.g. `Regrip_Pos`, 1 = the regrip position before loading into the reader) |
| `ReturnLabware(Labware)` | Return to the home position (the initial worktable position) |
| `RecordLabwarePosition(Labware, Location, Site)` | Tell the connector the current position after moving labware by hand (the instrument does not move) |

- FluentControl resets labware to its initial position at the start of every run, so the connector remembers the current position
  (`~/.riken_fluent780/labware_positions.json`) and, at the start of the next run, syncs FluentControl with `RemoveLabware` → `AddLabware` before moving.
- Only labware listed in the `plates.labware` setting (name, type, home position) may be moved (default `96 Well Flat_Custom[001]`, home position `Nest7mm_Pos[6]`).
- **Verified on the instrument from the UniteLabs App on 2026-10-15**: `Nest7mm_Pos[6]` → `Regrip_Pos[1]` → back to `Nest7mm_Pos[6]` in a separate run.
- When entering string values in the UniteLabs App, do not add quotes (the quotes become part of the value).

### Temperature-controlled and shaking devices (this server only)

| Feature | Command | Range (as restricted by the connector) |
|---|---|---|
| `BioShakeController` | `SetTemperature(Temperature)` / `TemperatureOff` | 4–70 °C |
| | `LockPlate` / `UnlockPlate` | |
| | `ShakeOn(Speed)` / `ShakeOff` | 1000, 1200, 1400, 1600, 1800 or 2000 rpm |
| `ShakingIncubatorController` (Inheco Incubator Shaker DWP) | `SetTemperature(Temperature)` | 20–60 °C |
| | `Close` | |
| | `ShakeOn` / `ShakeOff` | fixed preset `14,1.0,14,1.0,180` |
| `TemperaturePositionController` (the 3 CPLC positions, inhecoMTC) | `SetTemperature(Slot, Temperature)` / `TurnOff(Slot)` | 4–70 °C, slots 1–3 |
| `Incubator1Controller` (6 Nest Incubator Shaking) | `SetTemperature(Temperature)` (all positions) | 20–50 °C |
| | `CloseDoor` | |
| | `StartShaker(Frequency)` / `StopShaker` | 1.0–8.5 Hz |

- Commands return as soon as they are sent (they do not wait for the temperature to be reached). On 2026-10-15 each took about 9 s on the instrument, and the driver logged SC_ERROR_NONE.
- BioShake lock/unlock/shake, Inheco close/shake and Incubator1 door/shaker (5 Hz) were verified on the instrument on 2026-10-15 (no plate loaded; each shake ran for about 5 s).
- The basis for each argument's meaning, and the values whose meaning is unknown (fixed to the same values as the GUI scripts), are documented at the top of `temperature_devices.py`.
- Tecan's cleanup (Sub_Cleanup) returns the Inheco incubator and Incubator1 to 25 °C, sends TempOff to the BioShake, and turns the CPLC off.

Device macro templates extracted from the GUI scripts (83 of them) are in `tests/fixtures/gui_macros/` (sources in `index.tsv`; extracted with `tools/extract_gui_macros.py`).

---

## ODTC

```
SiLA 2 client (UniteLabs App, Python, etc.)
   │  jp.riken.bdr/odtc/*
   ▼
This connector (resident on the Mac at 10.5.1.213:50061, advertised via mDNS)
   │  SiLA 2: PrepareMethod / RunMethod / ExecuteCommand(XML)
   ▼
UniteLabs FluentControl connector (Fluent PC, Session 1)
   │  VisionX COM API
   ▼
FluentControl (method OFML_ApiHost) → SiLA-ODTC driver → ODTC
```

Each operation starts one FluentControl run (`OFML_ApiHost`), sends one ODTC macro, and ends the run.
Only one ODTC operation can run at a time.

## Features and Commands

| Feature | Command / Property | Description | Verified on the instrument (2026-10-15) |
|---|---|---|---|
| `DoorController` | `OpenDoor` / `CloseDoor` | Open/close the door (drawer) | door motion 9–10 s, total 18–48 s |
| `MethodController` | `Methods` (P) | The 5 PreMethods and 7 Methods registered on the device, with estimated durations | reads the copy taken on 2026-08-10 |
| | `RunPreMethod(PreMethod)` | Preheat. 7 min 45 s regardless of target temperature | `ini25` 466.3 s, 467.5 s |
| | `RunMethod(Method)` | Temperature program. Requires the matching PreMethod immediately before | `test` 16.0 s |
| | `LastPreMethod` (P) | The last PreMethod this connector completed | |
| `TemperatureController` | `ReadTemperatures` | Block, lid and other temperatures (°C) | about 12 s |
| `StatusController` | `Ready` / `Busy` / `CurrentOperation` / `FluentControlState` / `ActiveDialogs` (P) | Whether a command can be accepted right now, and what is running | |

(P) marks a Property. All others are Observable Commands that report progress and remaining time every 2 seconds.

### Safeguards

- **Only macros that have succeeded on the instrument are sent** (`OpenDoor`, `CloseDoor`, `ExecuteMethod`, `ReadActualTemperature`).
  `SetParameters` crashes the ODTC (confirmed as a device-side bug), and `StopMethod` is rejected by FluentControl, so neither can be sent.
- **Tests check that the XML sent is identical, character for character, to the files that succeeded on the instrument** (`tests/fixtures/`).
- **A Method is accepted only if a PreMethod with the matching start block temperature was completed immediately before.** Each PreMethod is consumed by one Method.
  Pairs: `ini4`–HeatShock, `ini25`–test, `ini37`–EnzymeReaction, `ini42`–For * insert, `ini95`–PCR.
- If FluentControl is not idle (another run in progress, waiting on a dialog), the request is rejected without doing anything.

### Known limitations

- The method list is a copy taken from the device on 2026-08-10. Methods added to the device after that do not appear.
- `LastPreMethod` is lost when the connector restarts. It also does not know about PreMethods run from the FluentControl GUI.
- FluentControl does not return temperature values through the API, so **they are read over SSH from the driver log on the Fluent PC**
  (place `windows/odtc_last_sensor_values.ps1` in `C:\Users\Tecan\OFML\tools\`). The PC running the connector must be able to
  `ssh Tecan@10.5.1.156` with key authentication. The ODTC clock is in local time, but timestamps end in `Z`.
- While a Method such as PCR is running, neither temperatures nor the door can be operated (FluentControl runs only one run at a time).
- **There is no authentication or encryption.** Anyone on the lab network can open and close the door.

## Running

```bash
uv sync
cp config.example.json config.json   # adjust hostname etc. if needed
uv run connector start --app riken_odtc_connector:create_app -cfg config.json
```

- With `fluent.dry_run: true`, the XML is built and logged but nothing is sent to the Fluent. `dry_run_seconds` adds a simulated wait.
- **Set `sila_server.hostname` to the LAN address (e.g. `10.5.1.213`).** With `0.0.0.0`, mDNS also advertises `127.0.0.1`,
  and the UniteLabs App on the Fluent PC ends up connecting to itself. If you do not want mDNS advertisement, set `discovery` to `null`.
- After adding Commands, make the UniteLabs App reload the Features
  (`PATCH http://10.5.1.156:3004/api/servers/<UUID>/features/update`, or reload the page).

## Quirks of the back end (UniteLabs FluentControl connector)

- **After restarting FluentControl, send `FluentController.GetRuntime` to the UniteLabs connector.** Otherwise it keeps pointing at the old FluentControl instance.
- On startup FluentControl stops at the "research use only" dialog (`VX_APPFR_019_001`). It can be closed with `RuntimeController.RespondToDialog(id, "OK")`.
- When a run finishes, FluentControl closes the method itself and enters `RunModeRunFinished`. This is an idle state; `CloseMethod` is not needed (and is rejected if sent).
- The UniteLabs connector is configured with port 0, so its port changes on every restart. When this connector cannot connect, it rediscovers only the port via mDNS
  (the other side advertises only `127.0.0.1` and `169.254.*` via mDNS, so the address comes from the `fluent.host` setting).

## Tests

```bash
uv run pytest
```

The tests connect to neither the devices nor the Fluent. They check that the XML matches what succeeded on the instrument, that dangerous macros are rejected, that the method list loads,
and the PreMethod pairing.

## Infinite 200 PRO (the reader inside the Fluent 780)

The same package contains a separate server for the Infinite 200 PRO ("Tecan Infinite 200 PRO (Fluent 780)", port 50063).
The back end (FluentControl runs via the UniteLabs connector) is shared with the ODTC.

```bash
cp config.infinite.example.json config.infinite.json   # set fluent.dry_run to false to drive the real instrument
uv run connector start --app riken_odtc_connector.infinite:create_app -cfg config.infinite.json
```

| Feature | Command / Property | Description |
|---|---|---|
| `TrayController` | `OpenTray` / `CloseTray` | Open/close the tray (`Magellan_Open` / `Magellan_Close`) |
| `AbsorbanceReader` | `Methods` (P) | Measurement files in `Magellan Pro\mth` on the Fluent PC |
| | `MeasureLoadedPlate(Method)` | Measure the plate already inside and return the values as labels × 96 wells |
| | `ReadAbsorbance(Wavelength)` | Return the OD of 96 wells at one wavelength. The Fluent version supports only wavelengths listed in the `infinite.wavelength_methods` setting |
| `FluentPlateHandling` | `MeasureWorktablePlate(Labware, Method)` | Move a plate registered on the worktable (by name) into the reader, measure it, and return it to its original position. One run |
| | `MeasurePlate(LabwareType, Location, Site, Method)` | Temporarily register an unregistered plate and do the same (**not verified on the instrument**) |

- Loading, measurement and unloading are done in a single run because FluentControl forgets labware positions when a new run starts (2026-08-13).
  `MeasurePlate` registers the plate under the temporary name `SiLA_ReaderPlate[001]` and deletes it afterwards. The source position must be empty in FluentControl's records.
- The regrip position is `Regrip_Pos[1]`, for flat-bottom plates. **PCR plates cannot be loaded into the reader with the current setup** (2026-10-09).
- Results are read over SSH from Magellan's ASC files (latin-1). Both the grid format and the one-well-per-line format (row order A1, A2, ...) are supported.
- `Abs600.mth` has 37 °C incubation enabled.
- **Verified on the instrument on 2026-10-15**: `OpenTray` / `CloseTray` (12 s each), `MeasureWorktablePlate("96 Well Flat_Custom[001]", "OFML_Abs405.mth")` (311 s total, of which 254 s measuring, 3 labels × 96 wells). Loading into and unloading from the reader worked with RGA macros (XML).
- The ODTC server and the Infinite server each enforce "one operation at a time" independently and do not know about each other's operations. Do not send commands to both at the same time.

## Infinite 200 PRO (standalone, USB, PyLabRobot)

A server for an Infinite 200 PRO that is not built into a Fluent, connected by USB to a Mac or Linux PC ("Tecan Infinite 200 PRO (USB)", port 50065).
**The `TrayController` and `AbsorbanceReader` definitions are identical to the Fluent version**, so callers can use the same Commands. There is no `FluentPlateHandling`.

```bash
cp config.infinite_usb.example.json config.infinite_usb.json   # set reader.dry_run to false to drive the real instrument
uv run connector start --app riken_odtc_connector.infinite:create_usb_app -cfg config.infinite_usb.json
```

| Command | Behaviour in the USB version |
|---|---|
| `OpenTray` / `CloseTray` | PyLabRobot `open()` / `close(plate)` |
| `ReadAbsorbance(Wavelength)` | PyLabRobot `read_absorbance(plate, all wells, wavelength)`. 230–1000 nm |
| `MeasureLoadedPlate(Method)` / `Methods` | Not available (no Magellan). `Methods` is empty |

- The back end is PyLabRobot's `ExperimentalTecanInfinite200ProBackend` (experimental, M series only), pinned to commit `183cabd` on GitHub main (as of 2026-10-15). It is not in PyPI release 0.2.2.
- The default plate definition is `cor_96_wellplate_360uL_Fb` (Costar 96 Flat Transparent).
- **It cannot be used for the Infinite in the Fluent PC.** On the Fluent PC the reader is attached to Tecan's proprietary USB driver (`Tecanat_Usb`) and cannot be opened from pyusb.
- **Not verified on an instrument** (no standalone Infinite is connected). On 2026-10-15 it was checked with tests that replace the PyLabRobot back end with a fake, and with a dry run.
  It was confirmed that, with no reader attached, it fails with "USB device not found".
