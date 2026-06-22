"""中身があるXBRL/CSVディレクトリに .complete マーカーを補完する。
対応するPDFファイルが存在するものだけを対象にする。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

output = Path("output")
added = skipped_empty = skipped_no_pdf = already = 0

for suffix in ("_xbrl", "_csv"):
    for d in output.rglob(f"*{suffix}"):
        if not d.is_dir():
            continue
        complete = d / ".complete"
        if complete.exists():
            already += 1
            continue
        # 中身がなければスキップ
        if not any(d.iterdir()):
            skipped_empty += 1
            continue
        # 対応PDFが存在するか確認
        base = d.name[: -len(suffix)]
        pdf = d.parent / f"{base}.pdf"
        if not pdf.exists() or pdf.stat().st_size == 0:
            skipped_no_pdf += 1
            continue
        complete.write_bytes(b"ok\n")
        added += 1

print(f"追加: {added} 件")
print(f"スキップ（空ディレクトリ）: {skipped_empty} 件")
print(f"スキップ（PDF未存在）: {skipped_no_pdf} 件")
print(f"既存のためスキップ: {already} 件")
