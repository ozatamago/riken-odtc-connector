# riken-odtc-connector

理研 BDR の Tecan Fluent 780 に組み込まれた装置を、SiLA 2 で操作するコネクタ。[UniteLabs CDK](https://docs.unitelabs.io/connector-development/) で作り、FluentControl を経由して動かす。

## まず使うもの: 1 つにまとめたサーバ「Tecan Fluent 780」（2026-10-15〜）

```bash
cp config.fluent780.example.json config.fluent780.json   # 実機で動かすときは fluent.dry_run を false に
uv run connector start --app riken_odtc_connector.fluent780:create_app -cfg config.fluent780.json
```

ODTC と Infinite 200 PRO の Feature（下の各節）を 1 つのサーバ（ポート 50060）に並べ、FluentControl に命令を送る裏側を 1 つだけにしたもの。
**同時に 1 つの操作だけ**を守るので、別の装置の操作は「別の操作が実行中です（…）」で断られる。
`StatusController.CurrentOperation` で、今何をしているかが分かる。

装置ごとのサーバ（ODTC 用 50061、Infinite 用 50063）も残してあるが、**このサーバと同時に動かさないこと**（互いの操作を知らず、
UniteLabs コネクタの「最後に開いた窓口」を通して他人のランを終わらせてしまうおそれがある）。

これから足す装置の Feature には、装置名を頭に付ける（例 `BioShakeController`）。ODTC の `TemperatureController` は名前を変えずに残している。

### RGA（`RgaController`、このサーバだけ）

| Command / Property | 中身 |
|---|---|
| `LabwarePositions`（P） | 運んでよいラボウェアと、コネクタが覚えている今の位置 |
| `MoveLabware(Labware, Location, Site)` | 指定の位置へ運ぶ（例 `Regrip_Pos`, 1 = リーダに入れる前の持ち替え位置） |
| `ReturnLabware(Labware)` | 元の位置（ワークテーブルの初期位置）へ戻す |
| `RecordLabwarePosition(Labware, Location, Site)` | 手で動かしたときに今の位置を教える（装置は動かない） |

- FluentControl はランのたびにラボウェアの位置を初期位置に戻して覚え直すので、コネクタが今の位置を覚え
  （`~/.riken_fluent780/labware_positions.json`）、次のランの最初に `RemoveLabware` → `AddLabware` で記憶を合わせてから運ぶ。
- 運んでよいのは設定 `plates.labware` に名前・型・元の位置を書いたものだけ（既定は `96 Well Flat_Custom[001]`、元の位置 `Nest7mm_Pos[6]`）。
- **2026-10-15 に UniteLabs App から実機で確認**: `Nest7mm_Pos[6]` → `Regrip_Pos[1]` → 別のランで `Nest7mm_Pos[6]` へ戻す。
- UniteLabs App で文字列の値を入れるときは、引用符を付けない（付けると引用符まで値になる）。

GUI スクリプトから取り出した装置のマクロの雛形（83 個）は `tests/fixtures/gui_macros/`（出どころは `index.tsv`、取り出しは `tools/extract_gui_macros.py`）。

---

## ODTC

```
SiLA 2 クライアント（UniteLabs App、Python など）
   │  jp.riken.bdr/odtc/*
   ▼
このコネクタ（Mac 10.5.1.213:50061 で常駐、mDNS で公開）
   │  SiLA 2：PrepareMethod / RunMethod / ExecuteCommand(XML)
   ▼
UniteLabs の FluentControl コネクタ（Fluent PC、Session 1）
   │  VisionX COM API
   ▼
FluentControl（メソッド OFML_ApiHost）→ SiLA-ODTC ドライバ → ODTC
```

1 つの操作ごとに FluentControl のラン（`OFML_ApiHost`）を 1 本開始し、ODTC のマクロを 1 つ送って終わらせる。
同時に動かせる ODTC の操作は 1 つだけ。

## Feature と Command

| Feature | Command / Property | 中身 | 実機での確認（2026-10-15） |
|---|---|---|---|
| `DoorController` | `OpenDoor` / `CloseDoor` | 扉（引き出し）の開閉 | 扉の動作 9〜10 秒、全体 18〜48 秒 |
| `MethodController` | `Methods`（P） | 装置に登録された PreMethod 5 本・Method 7 本の一覧と所要時間の見積り | 2026-08-10 の控えを読む |
| | `RunPreMethod(PreMethod)` | 予備加熱。目標温度によらず 7 分 45 秒 | `ini25` 466.3 秒、467.5 秒 |
| | `RunMethod(Method)` | 温度プログラム。直前に対応する PreMethod が要る | `test` 16.0 秒 |
| | `LastPreMethod`（P） | このコネクタが最後に済ませた PreMethod | |
| `TemperatureController` | `ReadTemperatures` | ブロック・蓋などの温度（°C） | 約 12 秒 |
| `StatusController` | `Ready` / `Busy` / `CurrentOperation` / `FluentControlState` / `ActiveDialogs`（P） | 今すぐ命令を受けられるか、今何をしているか | |

（P）は Property。それ以外は Observable Command で、進捗と残り時間を 2 秒ごとに返す。

### 守っていること

- **送るマクロは、実機で通した実績のあるものだけ**（`OpenDoor`、`CloseDoor`、`ExecuteMethod`、`ReadActualTemperature`）。
  `SetParameters` は ODTC を落とす（装置側の不具合で確定）、`StopMethod` は FluentControl に拒否されるので送れない。
- **送る XML は、実機で成功したときのファイルと 1 文字も違わない**ことをテストで確かめている（`tests/fixtures/`）。
- **Method は、開始ブロック温度が一致する PreMethod を直前に済ませたときだけ受け付ける**。PreMethod は Method ごとに 1 回で使い切り。
  対応: `ini4`–HeatShock、`ini25`–test、`ini37`–EnzymeReaction、`ini42`–For * insert、`ini95`–PCR。
- FluentControl が空いていない（他のラン中、ダイアログ待ち）ときは、何もせずに断る。

### 分かっている制約

- メソッドの一覧は 2026-08-10 に装置から写した控え。それ以降に装置へ足したメソッドは出ない。
- `LastPreMethod` はコネクタを再起動すると消える。FluentControl の GUI から流した PreMethod も知らない。
- 温度は、FluentControl が値を API に返さないので、**Fluent PC のドライバのログから SSH で読む**
  （`windows/odtc_last_sensor_values.ps1` を `C:\Users\Tecan\OFML_screen\` に置く）。コネクタを動かす PC から
  `ssh Tecan@10.5.1.156` が鍵認証で通ること。ODTC の時計は現地時刻なのに末尾が `Z` になっている。
- PCR などの Method の実行中は、温度も扉も操作できない（FluentControl のランは 1 本だけ）。
- **認証も暗号化もない。** 研究室のネットワークにいる人は誰でも扉を開け閉めできる。

## 起動

```bash
uv sync
cp config.example.json config.json   # 必要なら hostname などを直す
uv run connector start --app riken_odtc_connector:create_app -cfg config.json
```

- `fluent.dry_run: true` にすると、XML を組み立ててログに出すだけで Fluent には何も送らない。`dry_run_seconds` で送ったふりの待ち時間を足せる。
- `sila_server.hostname` は **LAN の住所（例 `10.5.1.213`）にする**。`0.0.0.0` にすると mDNS が `127.0.0.1` も知らせ、
  Fluent PC の UniteLabs App が自分自身につなぎに行ってしまう。mDNS で公開しないなら `discovery` を `null` にする。
- UniteLabs App に Command を足したあとは、App 側で Feature を読み直させる
  （`PATCH http://10.5.1.156:3004/api/servers/<UUID>/features/update`、またはページの再読み込み）。

## 裏側（UniteLabs の FluentControl コネクタ）の癖

- **FluentControl を再起動したら、UniteLabs のコネクタに `FluentController.GetRuntime` を送る。** 送らないと古い FluentControl を見たまま。
- FluentControl は起動時に「研究用のみ」のダイアログ（`VX_APPFR_019_001`）で止まる。`RuntimeController.RespondToDialog(id, "OK")` で閉じられる。
- ランが終わると FluentControl は自分でメソッドを閉じて `RunModeRunFinished` になる。これは空いている状態で、`CloseMethod` は不要（送ると断られる）。
- UniteLabs のコネクタは設定が port 0 なので、再起動するとポートが変わる。このコネクタは、つながらないときに mDNS でポートだけ探し直す
  （あちらは mDNS で `127.0.0.1` と `169.254.*` しか知らせないので、住所は設定の `fluent.host` を使う）。

## テスト

```bash
uv run pytest
```

装置にも Fluent にもつながない。XML が実機で成功したものと一致すること、危ないマクロを断ること、メソッド一覧の読み込みと
PreMethod の対応を確かめる。

## Infinite 200 PRO（Fluent 780 の中のリーダ）

同じパッケージに、Infinite 200 PRO 用の別のサーバ（「Tecan Infinite 200 PRO (Fluent 780)」、ポート 50063）がある。
裏側（FluentControl のラン、UniteLabs コネクタ経由）は ODTC と共通。

```bash
cp config.infinite.example.json config.infinite.json   # 実機で動かすときは fluent.dry_run を false に
uv run connector start --app riken_odtc_connector.infinite:create_app -cfg config.infinite.json
```

| Feature | Command / Property | 中身 |
|---|---|---|
| `TrayController` | `OpenTray` / `CloseTray` | トレイの開閉（`Magellan_Open` / `Magellan_Close`） |
| `AbsorbanceReader` | `Methods`（P） | Fluent PC の `Magellan Pro\mth` にある測定ファイル |
| | `MeasureLoadedPlate(Method)` | 中にあるプレートを測り、ラベル × 96 ウェルの値を返す |
| | `ReadAbsorbance(Wavelength)` | 1 波長で 96 ウェルの OD を返す。Fluent 版は設定 `infinite.wavelength_methods` に書いた波長だけ |
| `FluentPlateHandling` | `MeasureWorktablePlate(Labware, Method)` | ワークテーブルに登録済みのプレートを名前で運び、測って、元の位置へ戻す。ラン 1 本 |
| | `MeasurePlate(LabwareType, Location, Site, Method)` | 未登録のプレートを一時的に登録して同じことをする（**実機では未確認**） |

- 搬入・測定・搬出を 1 本のランで済ませるのは、ランを新しく始めると FluentControl がラボウェアの位置を忘れるため（2026-08-13）。
  `MeasurePlate` は、一時的な名前 `SiLA_ReaderPlate[001]` でプレートを登録し、終わったら消す。元の位置は FluentControl の記憶の上で空いていること。
- 持ち替え位置は平底プレート用の `Regrip_Pos[1]`。**PCR プレートは今の設定ではリーダに入れられない**（2026-10-09）。
- 結果は Magellan の ASC（latin-1）を SSH で読む。格子形式と 1 行 1 ウェル形式（行順 A1, A2, …）の両方に対応。
- `Abs600.mth` は 37 °C の保温が有効になっている。
- **2026-10-15 に実機で確認**: `OpenTray` / `CloseTray`（各 12 秒）、`MeasureWorktablePlate("96 Well Flat_Custom[001]", "OFML_Abs405.mth")`（全体 311 秒、うち測定 254 秒、3 ラベル × 96 ウェル）。RGA のマクロ（XML）でリーダへの搬入・搬出ができた。
- ODTC のサーバと Infinite のサーバは、それぞれ別に「1 操作ずつ」を守るだけで、互いの操作は知らない。両方から同時に命令を出さないこと。

## Infinite 200 PRO（単体、USB、PyLabRobot）

Fluent に組み込まれていない Infinite 200 PRO を、Mac や Linux の PC に USB でつないで使うサーバ（「Tecan Infinite 200 PRO (USB)」、ポート 50065）。
**`TrayController` と `AbsorbanceReader` の定義は Fluent 版と同じ**なので、呼ぶ側は同じ Command で使える。`FluentPlateHandling` は持たない。

```bash
cp config.infinite_usb.example.json config.infinite_usb.json   # 実機で動かすときは reader.dry_run を false に
uv run connector start --app riken_odtc_connector.infinite:create_usb_app -cfg config.infinite_usb.json
```

| Command | USB 版での動き |
|---|---|
| `OpenTray` / `CloseTray` | PyLabRobot の `open()` / `close(plate)` |
| `ReadAbsorbance(Wavelength)` | PyLabRobot の `read_absorbance(plate, 全ウェル, 波長)`。230〜1000 nm |
| `MeasureLoadedPlate(Method)` / `Methods` | 使えない（Magellan が無い）。`Methods` は空 |

- 裏側は PyLabRobot の `ExperimentalTecanInfinite200ProBackend`（実験的、M シリーズのみ）。GitHub の main のコミット `183cabd`（2026-10-15 時点）に固定。PyPI の 0.2.2 には入っていない。
- プレートの定義は既定で `cor_96_wellplate_360uL_Fb`（Costar 96 Flat Transparent）。
- **Fluent PC の Infinite には使えない。** Fluent PC ではリーダが Tecan 独自の USB ドライバ（`Tecanat_Usb`）につながっていて、pyusb から開けない。
- **実機では未確認**（単体の Infinite がつながっていない）。2026-10-15 は、PyLabRobot のバックエンドを偽物に差し替えたテストと、ドライランで確かめた。
  リーダなしで USB につなぐと「USB device not found」で断ることは確認した。
