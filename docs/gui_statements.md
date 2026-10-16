# FluentControl statements used by the 01-07 GUI scripts (stage 4 reference)

Extracted with `tools/extract_gui_statements.py` into `tests/fixtures/gui_statements/` (72 distinct statements;
`index.tsv` gives the script, line and main arguments of each). Device macros are in `tests/fixtures/gui_macros/`.

`tests/fixtures/liha_verified/` holds the pipetting XML that ran on the instrument in August 2026 (water, 1 and 8
channels). `tests/test_gui_statements.py` checks that it has exactly the same structure as the GUI statements, so the
stage-4 commands only need to fill in values.

## Statements and where their arguments are

| Statement | Count | Arguments (XML element) |
|---|---|---|
| `LihaGetTipsScriptCommandDataV3` | 5 | tip type `DitiType/AvailableID` (`TOOLTYPE:LiHa.TecanDiTi/TOOLNAME:FCA, 200ul SBS`; 10ul SBS, 200ul SBS and 1000ul Filtered SBS are used), channels `SelectedTipsIndexes` (0-7) or `TipMask`, `AirgapVolume` / `AirgapSpeed`, `UseNextPosition` |
| `LihaDropTipsScriptCommandDataV2` | 3 | waste `LabwareName` (always `FCA Thru Deck Waste Chute_1`), channels |
| `LihaAspirateScriptCommandDataV5` | 20 | `LabwareName`, wells `SelectedWellsString` + `SerializedWellIndexes`, one volume per channel `Volumes`, liquid class `LiquidClassNameBySelection`, channels, `TipSpacing`, `OffsetX/Y` |
| `LihaDispenseScriptCommandDataV6` | 13 | as Aspirate, plus `WellOffset` |
| `LihaMixScriptCommandDataV4` | 3 | as Aspirate, plus `Cycles` |
| `LihaDetectLiquidScriptCommandDataV3` | 2 | `LabwareName`, wells, channels, `SensitivityEx`, `DetectionSpeed` |
| `LihaSetClldErrorHandlingScriptCommandDataV4` | 2 | error actions for liquid level detection (06 only) |
| `LihaInlineFilterCheckScriptCommandDataV1` | 1 | (02 only) |
| `LihaReagentDistributionSmartCommand` | 6 | source `LabwareName`, `TargetVolumePerWell`, liquid class, `DynamicDiTiVolumeExpression` (e.g. `"20:FCA DiTi 50µl Filtered;80:FCA DiTi 200µl;"`), `MaxNumberOfDiTiReuse`, `MaxNumberOfMultiDispense`, `Direction`, wash settings |
| `LihaSampleTransferSmartCommand` | 2 | (06 only) |
| `LihaTransferIndividualVolumesSmartCommand` | 2 | (04) |
| `AddLabwareDataV1` | 4 | `LabwareType`, `LabwareLable` (sic), `Location`, `Position`, `Rotation`, `HasLid` |
| `RemoveLabwareDataV1` | 2 | `LabwareName` |
| `CgaGetFingersScriptCommandDataV1` | 4 | finger set `LabwareName` (`Eccentric[001]` or `Centric[001]`) |
| `CgaDropFingersScriptCommandDataV1` | 3 | `Backs` (`BackToSource`) |

## Well selection

`SelectedWellsString` is what the GUI shows; `SerializedWellIndexes` is what is executed. Forms seen:

| SelectedWellsString | SerializedWellIndexes | Meaning |
|---|---|---|
| `1 * A1` | `0;` | one channel, well A1 |
| `8 * A1` | `0;0;0;0;0;0;0;0;` | all 8 channels into the same well (trough) |
| `8 * A3` (4-row tube rack) | `8;8;8;8;8;8;8;8;` | all 8 channels into tube A3 |
| `A1 - H1` | `0>1>7;` | range `start>step>end`: column 1, one well per channel |
| `A6 - D6` (4-row tube rack) | `20>1>23;` | range |
| `2 * A1, 2 * B1, 2 * C1, 2 * D1` | `0;0;1;1;2;2;3;3;` | two channels per tube |

Well indexes are column-major from 0: index = (column - 1) x rows + (row - 1), so A1 = 0 and H1 = 7 on a 96-well plate,
and A3 = 8, A6 = 20 on the 4-row Alpaqua tube rack. `>` is written `&gt;` in the XML. `TipSpacing` depends on the
labware (9 mm for 96-well, 18.03 mm for the Alpaqua tube rack, 0 for troughs in the GUI scripts).

## Values that are expressions in the GUI scripts

The GUI scripts often use variables (`Vol`, `VolCell`, `LP`, `tipsNeeded(...)`) in volumes, `WellOffset` and `TipMask`.
The SiLA commands send literal values instead; the August 2026 XML shows literal values are accepted.

## Known constraints (from the instrument)

- A second Aspirate with the same tips fails; wrap each aspiration in GetTips ... DropTips (multi-dispense is fine).
- FCA 10 µl tips do not reach the bottom of a PCR plate on the 7 mm nest; use 50 µl or larger there.
- Labware positions reset when a new run starts, so a pipetting sequence must stay within one run.

## Arguments of each pipetting statement

All fields found in the 01-07 statements and the August 2026 XML (values seen in brackets). Every statement also has
the common fields `LiquidClassVariablesNames` / `LiquidClassVariablesValues` (always empty in 01-07; they look like a
per-statement override of liquid-class variables, untested) and the device alias.

| Statement | Fields |
|---|---|
| GetTips | tip type `DitiType`, channels (`SelectedTipsIndexes` / `TipMask`), `TipSpacing`, `AirgapVolume` [10], `AirgapSpeed` [70], `UseNextPosition` |
| DropTips | waste `LabwareName`, channels, `SkipIfNothingMounted` |
| Aspirate | `LabwareName`, wells (`SelectedWellsString`, `SerializedWellIndexes`, `WellOffset`), `Compartment`, `Volumes` (one per channel), liquid class (`LiquidClassSelectionMode` [SingleByName], `LiquidClassNameBySelection`, `LiquidClassNames` per channel, `LiquidClassNameByExpression`), channels, `TipSpacing`, `OffsetX/Y`, `IsSwitchContainerSourceEnabled`, `FlowRates` (always empty) |
| Dispense | as Aspirate, plus `DispenseDelays` (empty), `SkipZOnlyMoveToPipettingPosition` |
| Mix | as Aspirate, plus `Cycles` [1, 5, 10] |
| EmptyTips | `LabwareName`, wells, liquid class [Empty Tip], channels |
| DetectLiquid | `LabwareName`, wells, channels, `SensitivityEx` [2], `DetectionSpeed` [60] |
| SetClldErrorHandling | per error (`ErrorCode`, `VariableName`): `Action` [AspirateAvailable, IgnoreAndContinue, UseMicroscriptSettings, ...], `SecondAction` [UserPrompt] |
| InlineFilterCheck | channels (`SelectedTips` bit mask [255]) |
| ReagentDistribution / SampleTransfer / TransferIndividualVolumes | source and target labware (name, position, well geometry), volume per well, liquid class, tips per well, `DynamicDiTiVolumeExpression` (tip type by volume), `MaxNumberOfDiTiReuse`, `MaxNumberOfMultiDispense`, `Direction`, waste, wash settings (fixed tips only), sample/replicate count, `Pooling`; TransferIndividualVolumes reads the volumes from a file (`PipettingInstructionFilePath`) |

## Liquid classes (instrument: Air FCA)

The pipetting arm of this instrument is **Air FCA 1** (air displacement), so only the `AirFca` part of a liquid class
applies. A liquid class (`.xlqc`, 61 on the instrument PC) contains, per pipetting device (Fca, AirFca, Mca96, Mca384):

1. ~90-100 variables (speeds, accelerations, liquid level detection, tracking, pressure limits, error actions, ...)
2. a micro-script: the sequence of tip moves, air gaps, liquid level detection, aspirate/dispense, delays and retracts
3. per tip type: equation sets that give speeds, delays, air gaps, excess / conditioning volumes and an accuracy
   correction as functions of `volume`
4. per tip type and per aspirate/dispense: detection and positioning (cLLD on/off and sensitivity, submerge depth,
   tracking, Z reference such as `Z_MAX` / `KnownPosition` / `Z_DISPENSE` and offset, retract position, density,
   viscosity, pressure monitoring)

The classes used in 01-07 were compared completely (all fields of the AirFca part, for all 19 tip groups; detailed tables for the 10 µl, 200 µl, 200 µl Filtered, 1000 µl and 1000 µl Filtered tips used by 01-07; corrected 2026-10-16, previously listed 50 µl Filtered by mistake,
200 µl and 1000 µl Filtered tips) against Water Free Single. Highlights for the 200 µl tip:

| Liquid class | Main differences from Water Free Single |
|---|---|
| Water Free Multi | no leading air gap, 25 % excess volume, dispense 800, multi-dispense on |
| Water Contact Wet Single | liquid level detection and tracking while dispensing (dispense into liquid) |
| Water Dry Contact Single, CM_Water Z Max Single | dispense at `Z_MAX` + 0.4 mm |
| CM_8% PEG8000 Medium | aspirate speed `volume`, dispense `volume/2`, 1 s delay, small air gaps, cLLD while dispensing |
| Zymo buffers (Elution, Endo Wash, Lysis, Neutralization, Sample transfer) | aspirate at a known position -0.5 mm instead of liquid surface tracking, cLLD sensitivity 1 or 20, retract to `Z_TRAVEL`, fixed air gaps 3 / 7 µl |
| ... w post mixing | the micro-script adds a loop that slowly aspirates and dispenses 10 µl before aspirating |
| Zymo Ethanol Free Single | density 0.81, viscosity 1.2, different speed and air-gap equations, one pre-wetting step |
| Zymo Beads Single / Suspension | fixed air gaps 7 / 10 µl or simplified micro-script, retract to `Z_TRAVEL` / `Z_DISPENSE` |
| Standard Wash | has no AirFca part (fixed-tip wash; not used on this instrument) |
