"""CSV基準でoutput内の該当企業フォルダをZIPにまとめてsubmitフォルダに出力する。
Usage: make_submit_zip.py <companies.csv>"""
import sys, zipfile
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import edinet_downloader

csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("companies_2138_2600.csv")
output = Path("output")
submit = Path("submit")
submit.mkdir(exist_ok=True)

companies = edinet_downloader.read_companies(csv_path)
zip_path = submit / f"{csv_path.stem}.zip"

found = 0
missing = 0
file_count = 0

print(f"作成: {zip_path}")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for sec_code, comp in sorted(companies.items(), key=lambda x: x[1].output_code):
        matches = list(output.glob(f"{comp.output_code}_*"))
        if not matches:
            print(f"  スキップ（フォルダなし）: {comp.output_code} {comp.name}")
            missing += 1
            continue
        folder = matches[0]
        found += 1
        for f in sorted(folder.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(output))
                file_count += 1
        if found % 20 == 0:
            print(f"  {found}社処理済み...")

print(f"\n完了: {found}社 / {file_count}ファイル → {zip_path}")
print(f"ZIPサイズ: {zip_path.stat().st_size / 1024 / 1024:.1f} MB")
if missing:
    print(f"スキップ（フォルダなし）: {missing}社")
