"""FluentControl の GUI スクリプト（.xscr）から装置のマクロを取り出し、テストの「正解」として保存する。

    uv run python tools/extract_gui_macros.py <xscr のフォルダ> tests/fixtures/gui_macros

マクロ名と ExecutionSettings の組み合わせごとに 1 つずつ、<Object> の XML をそのまま保存する（LineNumber だけ 0 にする）。
無効化された行（IsDisabledForExecution="true"）は除く。どのスクリプトの何行目から取ったかを index.tsv に書く。
"""

import pathlib
import re
import sys

WANT = {
    "01_SolutionPrimer_Ver1_0", "02_PCR_Ver1_0", "03_Transformation_Ver1_0", "04_Selection_Medium_Preparation_Ver1_0",
    "05_Subculture_and_OD_measure_Ver1_0", "06_Plasmid_Extraction_Ver1_0", "07_Golden_Gate_Assembly_Ver1_0",
    "Sub_ODTC", "SUB_Heat shock", "Sub_Skake", "Sub_KineticMeasurement", "Sub_Start ODTC Initialize Method", "Sub_Cleanup",
}
PAT = re.compile(
    r'<Object Type="Tecan\.VisionX\.ApplicationDriver\.(?:ApplicationDriverBase\.ApplicationDriverMacro|LegacyDriverMacro)">\s*'
    r'<(LegacyDriverMacro|ApplicationDriverMacro) [^>]*Name="([^"]+)" ModuleName="([^"]+)"[^>]*'
    r'IsDisabledForExecution="(\w+)" LineNumber="(\d+)">.*?</\1>\s*</Object>',
    re.S,
)


def main(src: pathlib.Path, out: pathlib.Path) -> None:
    seen: dict[tuple[str, str], str] = {}
    index = ["file\tmodule\tmacro\tsettings\tsource\tline"]
    for f in sorted(src.glob("*.xscr")):
        text = f.read_text(encoding="utf-8-sig")
        name = re.search(r"<ObjectName>([^<]*)", text).group(1)
        if name not in WANT:
            continue
        for m in PAT.finditer(text):
            kind, macro, module, disabled, line = m.groups()
            if disabled.lower() == "true":
                continue
            obj = m.group(0)
            es = re.search(r"<ExecutionSettings\s*/>|<ExecutionSettings>(.*?)</ExecutionSettings>", obj, re.S)
            settings = (es.group(1) or "") if es else ""
            key = (macro, settings)
            if key in seen:
                continue
            obj = re.sub(r'LineNumber="\d+"', 'LineNumber="0"', obj, count=1)
            obj = "\n".join(ln.strip() for ln in obj.splitlines())
            fname = f"{macro}_{len([k for k in seen if k[0] == macro]) + 1}.xml".replace(" ", "_")
            (out / fname).write_text(obj + "\n", encoding="utf-8")
            seen[key] = fname
            short = settings if len(settings) < 120 else settings[:117] + "..."
            index.append(f"{fname}\t{module}\t{macro}\t{short}\t{name}\t{line}")
    (out / "index.tsv").write_text("\n".join(index) + "\n", encoding="utf-8")
    print(f"{len(seen)} 個のマクロを {out} に保存")


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))
