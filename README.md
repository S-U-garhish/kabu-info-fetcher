# EDINET 開示書類一括取得

指定した企業について、2025年以降の有価証券報告書・半期報告書・株主総会招集通知を取得するスクリプトです。通常書類はEDINET API v2、招集通知の不足分は東証上場会社情報サービスから補完します。

```powershell
python -m pip install -r .\requirements.txt
```

`pypdf` は、EDINETの添付ZIP内で `a_0618.pdf` のような無意味な名前になっているPDFの本文を読み、招集通知かどうか判定するために使用します。

## 重要な仕様訂正

同梱の `EDINET.md` は、2026年6月版のEDINET API仕様と次の点が異なります。

- 現行エンドポイントは `https://api.edinet-fsa.go.jp/api/v2`
- 書類取得の `type=1` は提出本文書・XBRL等のZIP、`type=2` はPDF、`type=5` はCSVのZIP
- 書類種別コード `030` は有価証券届出書、`040` は訂正有価証券届出書
- 株主総会招集通知（定時・臨時）を表す書類種別コードとして `030` / `040` は使用できない
- `130` は**訂正**有価証券報告書（半期報告書ではない）
- `140` は存在しない（四半期報告書は2024年度の開示制度改革で廃止）
- `160` が半期報告書の正しいコード

そのため、このスクリプトは誤った書類を保存しないよう、正しく確認できる `120`（有価証券報告書）と `160`（半期報告書）のみをデフォルト対象にします。

## 1. APIキー

EDINETでAPIキーを発行し、PowerShellで環境変数を設定します。

```powershell
$env:EDINET_API_KEY = "発行されたAPIキー"
```

## 2. 対象企業CSV

CSVはヘッダーなし・ありのどちらでも構いません。UTF-8（BOM可）またはCP932を読み込めます。

```csv
証券コード,企業名
7203,トヨタ自動車
6758,ソニーグループ
```

4桁コードとEDINET形式の5桁コード（末尾 `0`）の両方を受け付けます。

全上場企業のひな型は、金融庁の公式EDINETコードリストから生成できます。

```powershell
python .\edinet_downloader.py companies --output .\companies.csv
```

生成後、必要な企業だけをCSVに残してください。全社のまま実行すると非常に長い処理になります。

## 3. 実行

まず短い期間で確認する例:

```powershell
python .\edinet_downloader.py download .\companies.csv `
  --start 2025-06-01 --end 2025-06-07 --dry-run
```

2025年1月1日から本日まで取得:

```powershell
python .\edinet_downloader.py download .\companies.csv --output .\output
```

PDFだけ取得:

```powershell
python .\edinet_downloader.py download .\companies.csv --formats pdf
```

有価証券報告書のみ取得（半期報告書を除く）:

```powershell
python .\edinet_downloader.py download .\companies.csv --doc-types 120
```

対象書類種別は `--doc-types` で明示指定できます（`120`=有価証券報告書、`160`=半期報告書）。デフォルトは両方です。

## 保存構造

```text
output/
└── 7203_トヨタ自動車/
    ├── 2025有価証券報告書/
    │   ├── 7203_有価証券報告書..._S100XXXX.pdf
    │   ├── 7203_有価証券報告書..._S100XXXX_xbrl/
    │   └── 7203_有価証券報告書..._S100XXXX_csv/
    ├── 2025半期報告書/
    │   ├── 7203_半期報告書..._S100YYYY.pdf
    │   ├── 7203_半期報告書..._S100YYYY_xbrl/
    │   └── 7203_半期報告書..._S100YYYY_csv/
    └── 2025定時株主総会招集通知/
        ├── 7203_定時株主総会招集通知_EDINET_....pdf
        └── 7203_定時株主総会招集通知_JPX_....pdf
```

同じ年度・書類種別に複数提出があっても上書きしないよう、ファイル名に `docID` を含めます。ZIPは元のフォルダー構造を保って展開します。

## 再実行・レート制限

- 書類一覧APIは1〜3秒、書類取得APIは3〜10秒のランダム間隔
- 429または通信エラー時は60秒、120秒、300秒で再試行
- 日付ごとの一覧JSONを `output/.edinet/lists/` にキャッシュ
- 取得済みファイルはスキップ
- 失敗分は `output/.edinet/retry-queue.json` に記録
- 一覧を取り直す場合は `--refresh-lists` を指定
- 添付ZIPを再検査する場合は `--refresh-attachments` を指定
- EDINET添付ZIPは解析前に `output/.edinet/attachment-zips/` へ退避
- JPXの企業別検索結果は `output/.edinet/jpx/` に逐次キャッシュ
- JPXキャッシュを取り直す場合は `--refresh-jpx` を指定

添付文書の検査中にPDF解析やZIP展開で失敗しても、ダウンロード済みZIPは残ります。
次回実行では保存済みZIPから再検査するため、EDINETへの再ダウンロードは発生しません。
ただし、通信失敗などでZIPを受信できなかった場合は保存されません。

JPXキャッシュは、過去期間のバックフィルでは再実行時も再利用します。本日を含む期間を
検索する場合は、同じ日に取得したキャッシュだけを使用し、日付が変われば更新します。

## 株主総会招集通知

処理順は次のとおりです。

1. 有価証券報告書の `attachDocFlag=1` について、EDINET APIの `type=3` を取得
2. ZIP内PDFの先頭3ページを読み、「招集通知」「招集ご通知」「株主総会資料」等で判定
3. EDINETで1件も見つからなかった企業を、JPXから1社ずつ低頻度で補完

監査用CSVも出力します。

- `output/shareholder-notices.csv`: EDINET・JPXで取得した全招集通知
- `output/jpx-required.csv`: EDINETでは見つからず、JPXが必要だった書類
- `output/shareholder-notice-status.csv`: 全対象企業の取得状態

JPXへのアクセスを止めて、補完が必要な企業だけ確認する場合:

```powershell
python .\edinet_downloader.py download .\companies.csv --no-jpx
```

招集通知処理そのものを行わない場合:

```powershell
python .\edinet_downloader.py download .\companies.csv --no-shareholder-docs
```

初回の2025年以降バックフィルは、夜間に並列数1のまま実行する想定です。

## 途中成果を報告・引き継ぐ

ダウンローダーを動かしたままでも、その時点で完成しているファイルだけをZIP化できます。
書き込み途中の `.part` と秘密情報を含む `.env` は除外されます。

```powershell
python .\package_handoff.py --output .\output --package-dir .\handoff
```

以下が生成されます。

- `edinet_scripts_*.zip`: スクリプト、README、依存関係、企業CSV、仕様書
- `edinet_results_*.zip`: その時点の成果物
- `manifest_*.csv`: どのファイルをどのZIPへ格納したか
- `progress_summary_*.txt`: 企業フォルダー数、ファイル数、容量、処理済み日数

成果物が大きい場合は、既定で非圧縮サイズ約4GBごとの独立ZIPに分割されます。

```powershell
python .\package_handoff.py --max-archive-gb 2
```

別PCでも処理を再開できるよう、API一覧キャッシュ、添付ZIP、JPXキャッシュ等も梱包する場合:

```powershell
python .\package_handoff.py --include-state
```

急いでおり圧縮時間を省きたい場合:

```powershell
python .\package_handoff.py --store
```

`--store` はZIP内で圧縮しないため高速ですが、ファイルサイズは大きくなります。
