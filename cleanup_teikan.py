import sys, csv, shutil
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path

csv_path = Path('notice_check.csv')
deleted = 0
skipped = 0

with csv_path.open(encoding='utf-8-sig', newline='') as f:
    for row in csv.DictReader(f):
        if row['reason'] != '先頭500字に別書類キーワード: 定款':
            continue
        path = Path(row['path'])
        if '_teikan' not in path.name and not path.name.startswith('teikan'):
            continue
        if not path.exists():
            skipped += 1
            continue
        print(f"削除: {path}")
        path.unlink()
        deleted += 1

        parent = path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            print(f"  空フォルダ削除: {parent}")

print(f"\n削除: {deleted} 件 / スキップ: {skipped} 件")
