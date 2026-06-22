import sys, csv
sys.stdout.reconfigure(encoding='utf-8')
import edinet_downloader
from pathlib import Path

companies = edinet_downloader.read_companies(Path('companies.csv'))
codes = sorted(c.output_code for c in companies.values())
print(f'総数: {len(codes)}')

# 2138-2600付近のサンプル
sample = [c for c in codes if '2130' <= c <= '2610']
print('2130-2610 付近サンプル:')
for c in sample[:8]:
    print(f'  {c}')
print('  ...')
for c in sample[-8:]:
    print(f'  {c}')

# 抽出してCSV出力
target = {sec: comp for sec, comp in companies.items() if '2138' <= comp.output_code <= '2600'}
print(f'\n2138-2600 抽出: {len(target)}社')

out = Path('companies_2138_2600.csv')
with out.open('w', encoding='utf-8-sig', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(['証券コード', '企業名'])
    for comp in sorted(target.values(), key=lambda c: c.output_code):
        writer.writerow([comp.input_code, comp.name])
print(f'出力: {out}')
