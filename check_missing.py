"""有報・半期の抜けをチェックしてCSV出力する。Usage: check_missing.py <companies.csv>"""
import sys, csv
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import edinet_downloader

csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("companies_7333_7665.csv")
output = Path("output")
companies = edinet_downloader.read_companies(csv_path)
print(f"対象: {csv_path} ({len(companies)}社)")

rows = []
for sec_code, comp in sorted(companies.items(), key=lambda x: x[1].output_code):
    matches = list(output.glob(f"{comp.output_code}_*"))
    folder = matches[0] if matches else None
    if not folder:
        rows.append({
            "code": comp.output_code, "name": comp.name,
            "2025有報": "フォルダなし", "2025半期": "フォルダなし",
            "2026有報": "フォルダなし", "2026半期": "フォルダなし",
        })
        continue

    def count_pdfs(year, doc_type):
        d = folder / f"{year}{doc_type}"
        if not d.exists():
            return "なし"
        pdfs = list(d.glob("*.pdf"))
        return str(len(pdfs)) if pdfs else "0件"

    row = {
        "code": comp.output_code,
        "name": comp.name,
        "2025有報": count_pdfs("2025", "有価証券報告書"),
        "2025半期": count_pdfs("2025", "半期報告書"),
        "2026有報": count_pdfs("2026", "有価証券報告書"),
        "2026半期": count_pdfs("2026", "半期報告書"),
    }
    rows.append(row)

out_path = Path(f"missing_check_{csv_path.stem}.csv")
with out_path.open("w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=["code","name","2025有報","2025半期","2026有報","2026半期"])
    writer.writeheader()
    writer.writerows(rows)

# サマリー表示
missing = [r for r in rows if "なし" in r.values() or "0件" in r.values() or "フォルダなし" in r.values()]
print(f"総企業数: {len(rows)}")
print(f"何らかの抜けあり: {len(missing)}")
print()

for label, key in [("2025有報", "2025有報"), ("2025半期", "2025半期"), ("2026有報", "2026有報"), ("2026半期", "2026半期")]:
    cnt = sum(1 for r in rows if r[key] in ("なし", "0件", "フォルダなし"))
    print(f"{label} なし: {cnt}社")

print()
print("=== 抜けがある企業（有報のみ） ===")
for r in rows:
    missing_items = []
    for k in ["2025有報", "2026有報"]:
        if r[k] in ("なし", "0件", "フォルダなし"):
            missing_items.append(k)
    if missing_items:
        print(f"  {r['code']} {r['name']}: {', '.join(missing_items)}")

print(f"\n詳細: {out_path}")
