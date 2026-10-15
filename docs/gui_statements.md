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
