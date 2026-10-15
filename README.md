# riken-odtc-connector

理研 BDR の Tecan Fluent 780 に組み込まれた **Inheco ODTC 96**（サーマルサイクラー）を、SiLA 2 で操作するコネクタ。
[UniteLabs CDK](https://docs.unitelabs.io/connector-development/) で作り、FluentControl を経由して ODTC を動かす。

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
| `StatusController` | `Ready` / `Busy` / `FluentControlState` / `ActiveDialogs`（P） | 今すぐ命令を受けられるか | |

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
