"""submitZIPを再作成: _xbrl/ と _csv/ ディレクトリを個別ZIPに変換して梱包する。
Usage: rezip_submit.py <companies.csv>
既存ZIPは .orig.zip にリネームしてバックアップ。"""
import sys, zipfile, io
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import edinet_downloader

csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("companies_2138_2600.csv")
output = Path("output")
submit = Path("submit")

companies = edinet_downloader.read_companies(csv_path)
zip_path = submit / f"{csv_path.stem}.zip"
backup_path = submit / f"{csv_path.stem}.orig.zip"

# 既存ZIPをバックアップ
if zip_path.exists():
    zip_path.rename(backup_path)
    print(f"バックアップ: {backup_path}")

SKIP_NAMES = {".complete"}
REZIP_SUFFIXES = ("_xbrl", "_csv")

found = file_count = rezipped = 0

print(f"作成: {zip_path}")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for sec_code, comp in sorted(companies.items(), key=lambda x: x[1].output_code):
        matches = list(output.glob(f"{comp.output_code}_*"))
        if not matches:
            continue
        folder = matches[0]
        found += 1

        # フォルダ内のファイル・ディレクトリを処理
        for item in sorted(folder.rglob("*")):
            if not item.is_file():
                continue
            if item.name in SKIP_NAMES:
                continue

            # _xbrl/ または _csv/ 配下のファイルはスキップ（後でまとめてzip化）
            skip = False
            for part in item.relative_to(folder).parts[:-1]:
                if any(part.endswith(s) for s in REZIP_SUFFIXES):
                    skip = True
                    break
            if skip:
                continue

            zf.write(item, item.relative_to(output))
            file_count += 1

        # _xbrl/ と _csv/ を個別ZIPとして梱包
        for suffix in REZIP_SUFFIXES:
            for xdir in sorted(folder.rglob(f"*{suffix}")):
                if not xdir.is_dir():
                    continue
                inner_files = [f for f in xdir.rglob("*") if f.is_file() and f.name not in SKIP_NAMES]
                if not inner_files:
                    continue
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as inner:
                    for f in sorted(inner_files):
                        inner.write(f, f.relative_to(xdir))
                zip_entry = xdir.relative_to(output).with_suffix(".zip")
                zf.writestr(str(zip_entry), buf.getvalue())
                rezipped += 1
                file_count += 1

        if found % 20 == 0:
            print(f"  {found}社処理済み...")

print(f"\n完了: {found}社 / ファイル{file_count}件（うち再zip{rezipped}件）→ {zip_path}")
print(f"ZIPサイズ: {zip_path.stat().st_size / 1024 / 1024:.1f} MB")
