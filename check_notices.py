#!/usr/bin/env python3
"""Download済み招集通知PDFの内容を検査し、分類ミスの疑いがあるファイルをCSVに出力する。"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

sys.stderr.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

NOTICE_KEYWORDS = {"招集通知", "招集ご通知", "電子提供措置事項", "交付書面省略事項", "株主総会資料", "参考書類"}
RESOLUTION_KEYWORDS = {"決議ご通知", "決議通知"}
MISMATCH_KEYWORDS = {
    "定款": "定款",
    "議事録": "議事録",
    "有価証券報告書": "有価証券報告書",
    "半期報告書": "半期報告書",
    "目論見書": "目論見書",
}
CHECK_PAGES = 5


def extract_text(path: Path, max_pages: int) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(path.read_bytes()))
        return "\n".join(
            reader.pages[i].extract_text() or ""
            for i in range(min(max_pages, len(reader.pages)))
        )
    except Exception as exc:
        return f"__READ_ERROR__: {exc}"


def check_pdf(path: Path) -> tuple[str, str]:
    """(status, reason) を返す。status: ok / suspicious / error"""
    text = extract_text(path, CHECK_PAGES)
    if text.startswith("__READ_ERROR__"):
        return "error", text

    normalized = "".join(text.split())

    # 決議通知は除外対象なのに通り抜けた
    for kw in RESOLUTION_KEYWORDS:
        if kw in normalized:
            return "suspicious", f"除外キーワード検出: {kw}"

    # 招集通知の必須キーワードが1つもない
    if not any(kw in normalized for kw in NOTICE_KEYWORDS):
        # 株主総会という言葉すらなければ完全に別物
        if "株主総会" not in normalized:
            return "suspicious", "招集通知キーワードなし・株主総会なし"
        return "suspicious", "招集通知キーワードなし（株主総会はあり）"

    # 別書類のキーワードが先頭に強く出ている
    first_500 = "".join(text[:500].split())
    for kw, label in MISMATCH_KEYWORDS.items():
        if kw in first_500:
            return "suspicious", f"先頭500字に別書類キーワード: {label}"

    return "ok", ""


def find_notice_pdfs(output: Path) -> list[Path]:
    pdfs = []
    for folder in output.rglob("*"):
        if not folder.is_dir():
            continue
        if "招集通知" not in folder.name:
            continue
        for pdf in folder.glob("*.pdf"):
            pdfs.append(pdf)
    return sorted(pdfs)


def main() -> int:
    output = Path("output")
    if not output.is_dir():
        print("output/ ディレクトリが見つかりません", file=sys.stderr)
        return 2

    pdfs = find_notice_pdfs(output)
    if not pdfs:
        print("招集通知PDFが見つかりません")
        return 0

    print(f"検査対象: {len(pdfs)} 件", file=sys.stderr)

    report_path = Path("notice_check.csv")
    suspicious: list[dict] = []

    with report_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["status", "reason", "path"])
        writer.writeheader()
        for i, pdf in enumerate(pdfs, 1):
            if i % 50 == 0:
                print(f"  {i}/{len(pdfs)} ...", file=sys.stderr)
            status, reason = check_pdf(pdf)
            if status != "ok":
                row = {"status": status, "reason": reason, "path": str(pdf)}
                writer.writerow(row)
                suspicious.append(row)

    print(f"\n疑義あり: {len(suspicious)} 件 → {report_path}")
    for row in suspicious:
        print(f"  [{row['status']}] {row['reason']}")
        print(f"    {row['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
