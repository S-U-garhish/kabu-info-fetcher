"""株主総会招集通知の取得状況をチェックする。Usage: check_shareholder.py <companies.csv> <year>"""
import sys, csv
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import edinet_downloader

csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("companies_7333_7665.csv")
year = sys.argv[2] if len(sys.argv) > 2 else "2025"
output = Path("output")
companies = edinet_downloader.read_companies(csv_path)
print(f"対象: {csv_path} ({len(companies)}社) / {year}年")

have = []
missing = []

for sec_code, comp in sorted(companies.items(), key=lambda x: x[1].output_code):
    matches = list(output.glob(f"{comp.output_code}_*"))
    folder = matches[0] if matches else None
    if not folder:
        missing.append((comp.output_code, comp.name, "フォルダなし"))
        continue

    # 定時・臨時どちらかのPDFがあればOK
    pdfs = list(folder.glob(f"{year}*株主総会招集通知/*.pdf"))
    if pdfs:
        have.append((comp.output_code, comp.name, len(pdfs)))
    else:
        missing.append((comp.output_code, comp.name, "招集通知なし"))

print(f"\n取得済み: {len(have)}社")
print(f"未取得:   {len(missing)}社")

if missing:
    print(f"\n=== {year}年 株主総会招集通知 未取得 ===")
    for code, name, reason in missing:
        print(f"  {code} {name}  ({reason})")

out_path = Path(f"missing_shareholder_{csv_path.stem}_{year}.csv")
with out_path.open("w", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["証券コード", "企業名", "状態"])
    for code, name, reason in missing:
        writer.writerow([code, name, reason])
print(f"\n未取得リスト: {out_path} ({len(missing)}件)")
