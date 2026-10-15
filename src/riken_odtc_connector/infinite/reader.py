"""Fluent 780 の Infinite 200 PRO を、FluentControl（Magellan のマクロと RGA）経由で動かす裏側。

測定の結果は FluentControl の API に返らないので、Magellan が書く ASC ファイルを SSH で読む。
搬入・測定・搬出は FluentControl のラン 1 本で済ませる（ランを新しく始めるとラボウェアの位置を忘れる。2026-08-13 確認）。
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime
import logging
import time

from unitelabs.cdk.config import Config

from ..fluent_backend import BackendError, FluentOdtcBackend
from .asc import AscResult, parse_asc
from .xml import add_labware_xml, magellan_xml, remove_labware_xml, rga_xml

logger = logging.getLogger(__name__)

TEMP_LABEL = "SiLA_ReaderPlate[001]"
"""MeasurePlate が一時的に FluentControl に登録するラボウェアの名前。ワークテーブルの既存の名前と重ならないもの。"""


@dataclasses.dataclass
class InfiniteConfig(Config):
    """Fluent 780 の中の Infinite 200 PRO の設定。値は 2026-08-13 に実機で通したもの。"""

    reader_location: str = "InfiniteM200_New"
    reader_site: int = 1
    regrip_location: str = "Regrip_Pos"
    """平底プレート用の持ち替え位置（7mm Regrip Nest）。PCR プレートは今の設定ではリーダに入れられない。"""
    regrip_site: int = 1
    output_dir: str = r"C:\Users\Public\Documents\Tecan\Magellan Pro\asc"
    """Magellan_Measure の出力先。ここに「riken_<日時>」という名前で書かせ、できた .asc をこの下から探す。"""
    method_dir: str = r"C:\Users\Public\Documents\Tecan\Magellan Pro\mth"


class FluentInfiniteReader:
    def __init__(self, backend: FluentOdtcBackend, config: InfiniteConfig):
        self.backend = backend
        self.config = config

    # ---- 測定ファイル（Magellan のメソッド）
    async def list_methods(self) -> list[str]:
        """method_dir の下の .mth を、method_dir からの相対パスで返す（例 "Abs600.mth"、"Azumi\\x.mth"）。"""
        d = self.config.method_dir
        out = await asyncio.to_thread(
            self.backend._ssh_ps,
            f"-Command \"Get-ChildItem -LiteralPath '{d}' -Recurse -Filter *.mth | "
            f"ForEach-Object {{ $_.FullName.Substring({len(d) + 1}) }}\"",
        )
        return sorted(line.strip() for line in out.splitlines() if line.strip())

    async def check_method(self, method: str) -> None:
        if method not in await self.list_methods():
            raise BackendError(f"測定ファイル {method!r} が {self.config.method_dir} にありません")

    # ---- 命令の組み立て
    def _measure_name(self) -> str:
        return "riken_" + datetime.datetime.now().strftime("%y%m%d_%H%M%S")

    def measure_steps(self, method: str, name: str) -> list[tuple[str, str]]:
        settings = f"{self.config.output_dir}\\{name},{method},0,0,0"
        return [(f"Magellan_Measure {method}", magellan_xml("Magellan_Measure", settings))]

    def load_measure_unload_steps(self, labware_type: str, location: str, site: int, method: str,
                                  name: str) -> list[tuple[str, str]]:
        c, lbl = self.config, TEMP_LABEL
        return [
            (f"AddLabware {labware_type} at {location}[{site}]", add_labware_xml(labware_type, lbl, location, site)),
            ("Magellan_Open", magellan_xml("Magellan_Open")),
            (f"RGA -> {c.regrip_location}[{c.regrip_site}]",
             rga_xml(lbl, c.regrip_location, str(c.regrip_site), fixed_site=True)),
            (f"RGA -> {c.reader_location}[{c.reader_site}]",
             rga_xml(lbl, c.reader_location, str(c.reader_site), fixed_site=True)),
            ("Magellan_Close", magellan_xml("Magellan_Close")),
            *self.measure_steps(method, name),
            ("Magellan_Open", magellan_xml("Magellan_Open")),
            (f"RGA -> {c.regrip_location}[{c.regrip_site}]",
             rga_xml(lbl, c.regrip_location, str(c.regrip_site), fixed_site=True)),
            (f"RGA -> {location}[{site}]", rga_xml(lbl, location, str(site), fixed_site=True)),
            ("Magellan_Close", magellan_xml("Magellan_Close")),
            (f"RemoveLabware {lbl}", remove_labware_xml(lbl)),
        ]

    def worktable_plate_steps(self, labware: str, method: str, name: str) -> list[tuple[str, str]]:
        """ワークテーブルに登録済みのプレートを名前で運び、測って、元の位置（ベース）へ戻す。

        2026-08-13 に Tecan のサーバの TransferLabware / TransferLabwareBackToBase で通した経路と同じ。
        ここでは同じ動きを RGA のマクロ（XML）で送る。ベースへ戻すマクロは 10-09 に ODTC_Lid で実機確認済み。
        """
        c = self.config
        return [
            ("Magellan_Open", magellan_xml("Magellan_Open")),
            (f"RGA {labware} -> {c.regrip_location}[{c.regrip_site}]",
             rga_xml(labware, c.regrip_location, str(c.regrip_site), fixed_site=True)),
            (f"RGA {labware} -> {c.reader_location}[{c.reader_site}]",
             rga_xml(labware, c.reader_location, str(c.reader_site), fixed_site=True)),
            ("Magellan_Close", magellan_xml("Magellan_Close")),
            *self.measure_steps(method, name),
            ("Magellan_Open", magellan_xml("Magellan_Open")),
            (f"RGA {labware} -> {c.regrip_location}[{c.regrip_site}]",
             rga_xml(labware, c.regrip_location, str(c.regrip_site), fixed_site=True)),
            (f"RGA {labware} -> base", rga_xml(labware, move_to_base=True)),
            ("Magellan_Close", magellan_xml("Magellan_Close")),
        ]

    # ---- 結果の取り出し
    def _fetch_asc(self, name: str, since: float) -> tuple[str, AscResult]:
        d = self.config.output_dir
        script = (
            f"$f = Get-ChildItem -LiteralPath '{d}' -Recurse -Filter '{name}*.asc' | "
            "Sort-Object LastWriteTime -Descending | Select-Object -First 1; "
            "if ($f) { $f.FullName; [Convert]::ToBase64String([IO.File]::ReadAllBytes($f.FullName)) } else { 'NOTFOUND' }"
        )
        deadline = time.monotonic() + 60
        while True:
            out = self.backend._ssh_ps(f'-Command "{script}"').strip().splitlines()
            if out and out[0] != "NOTFOUND" and len(out) >= 2:
                text = base64.b64decode(out[1]).decode("latin-1")
                return out[0], parse_asc(text)
            if time.monotonic() > deadline:
                raise BackendError(f"測定結果 {name}*.asc が {d} の下に見つかりませんでした")
            time.sleep(3)

    # ---- Feature から呼ぶ入口
    async def run(self, steps: list[tuple[str, str]], name: str | None, report) -> tuple[str, AscResult | None]:
        started = time.time()
        await self.backend.run_steps(steps, report)
        if name is None or self.backend.config.dry_run:
            return "", None
        return await asyncio.to_thread(self._fetch_asc, name, started)
