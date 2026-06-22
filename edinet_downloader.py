#!/usr/bin/env python3
"""EDINET documents downloader with JPX shareholder-document fallback."""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import random
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


API_BASE = "https://api.edinet-fsa.go.jp/api/v2"
CODE_LIST_URL = (
    "https://disclosure2dl.edinet-fsa.go.jp/"
    "searchdocument/codelist/Edinetcode.zip"
)
SUPPORTED_TYPES = {
    "120": "有価証券報告書",
    "160": "半期報告書",
}
DOWNLOAD_TYPES = {
    "pdf": ("2", "pdfFlag"),
    "xbrl": ("1", "xbrlFlag"),
    "csv": ("5", "csvFlag"),
}
JPX_BASE = "https://www2.jpx.co.jp"
NOTICE_KEYWORDS = ("招集通知", "招集ご通知", "株主総会")
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class EdinetError(RuntimeError):
    pass


@dataclass(frozen=True)
class Company:
    input_code: str
    sec_code: str
    output_code: str
    name: str


@dataclass(frozen=True)
class Notice:
    security_code: str
    company_name: str
    source: str
    notice_type: str
    date: str
    title: str
    path: str
    url: str = ""
    doc_id: str = ""


class RateLimiter:
    def __init__(self, minimum: float, maximum: float) -> None:
        if minimum < 0 or maximum < minimum:
            raise ValueError("不正な待機間隔です")
        self.minimum = minimum
        self.maximum = maximum
        self.last_request_at: float | None = None

    def wait(self) -> None:
        if self.last_request_at is not None:
            target = random.uniform(self.minimum, self.maximum)
            elapsed = time.monotonic() - self.last_request_at
            if elapsed < target:
                time.sleep(target - elapsed)
        self.last_request_at = time.monotonic()


class EdinetClient:
    def __init__(
        self,
        api_key: str,
        list_interval: tuple[float, float],
        download_interval: tuple[float, float],
        backoffs: tuple[int, ...] = (60, 120, 300),
        timeout: int = 120,
    ) -> None:
        self.api_key = api_key
        self.list_limiter = RateLimiter(*list_interval)
        self.download_limiter = RateLimiter(*download_interval)
        self.backoffs = backoffs
        self.timeout = timeout

    def _request(
        self,
        url: str,
        limiter: RateLimiter,
        expected_content_types: tuple[str, ...],
    ) -> tuple[bytes, str]:
        attempts = len(self.backoffs) + 1
        for attempt in range(attempts):
            limiter.wait()
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "edinet-batch-downloader/1.0"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    content_type = response.headers.get_content_type()
            except urllib.error.HTTPError as exc:
                body = exc.read()
                if exc.code == 429 and attempt < len(self.backoffs):
                    delay = self.backoffs[attempt]
                    logging.warning("HTTP 429。%s秒後に再試行します", delay)
                    time.sleep(delay)
                    continue
                raise EdinetError(
                    f"HTTP {exc.code}: {body[:500].decode('utf-8', 'replace')}"
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < len(self.backoffs):
                    delay = self.backoffs[attempt]
                    logging.warning("通信エラー。%s秒後に再試行します: %s", delay, exc)
                    time.sleep(delay)
                    continue
                raise EdinetError(f"通信に失敗しました: {exc}") from exc

            if content_type == "application/json":
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise EdinetError("EDINETから不正なJSONが返されました") from exc
                status = str(
                    payload.get("metadata", {}).get("status")
                    or payload.get("StatusCode")
                    or ""
                )
                if status == "429" and attempt < len(self.backoffs):
                    delay = self.backoffs[attempt]
                    logging.warning("JSON status 429。%s秒後に再試行します", delay)
                    time.sleep(delay)
                    continue
                if content_type not in expected_content_types:
                    message = (
                        payload.get("metadata", {}).get("message")
                        or payload.get("message")
                        or payload
                    )
                    raise EdinetError(f"EDINET APIエラー ({status}): {message}")

            if content_type not in expected_content_types:
                raise EdinetError(
                    f"想定外のContent-Typeです: {content_type} ({body[:200]!r})"
                )
            return body, content_type

        raise AssertionError("retry loop exhausted")

    def list_documents(self, target_date: date) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "date": target_date.isoformat(),
                "type": "2",
                "Subscription-Key": self.api_key,
            }
        )
        body, _ = self._request(
            f"{API_BASE}/documents.json?{query}",
            self.list_limiter,
            ("application/json",),
        )
        payload = json.loads(body)
        metadata = payload.get("metadata", {})
        if str(metadata.get("status")) != "200":
            raise EdinetError(
                f"一覧APIエラー ({metadata.get('status')}): {metadata.get('message')}"
            )
        return payload

    def download_document(self, doc_id: str, api_type: str) -> tuple[bytes, str]:
        query = urllib.parse.urlencode(
            {"type": api_type, "Subscription-Key": self.api_key}
        )
        return self._request(
            f"{API_BASE}/documents/{urllib.parse.quote(doc_id)}?{query}",
            self.download_limiter,
            ("application/pdf", "application/octet-stream"),
        )


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text).strip()))
            self._href = None
            self._text = []


class JPXClient:
    SEARCH_URL = f"{JPX_BASE}/tseHpFront/JJK010010Action.do"
    DETAIL_URL = f"{JPX_BASE}/tseHpFront/JJK010030Action.do"

    def __init__(
        self,
        interval: tuple[float, float] = (3.0, 10.0),
        timeout: int = 120,
    ) -> None:
        self.limiter = RateLimiter(*interval)
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        self.headers = {"User-Agent": "edinet-batch-downloader/1.0"}
        self.initialized = False

    def _request(self, url: str, data: dict[str, str] | None = None) -> bytes:
        self.limiter.wait()
        encoded = urllib.parse.urlencode(data).encode() if data else None
        request = urllib.request.Request(
            url, data=encoded, headers=self.headers
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.read()
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise EdinetError(f"JPXへの接続に失敗しました: {exc}") from exc

    def list_notices(
        self, company: Company, start: date, end: date
    ) -> list[dict[str, str]]:
        if not self.initialized:
            self._request(f"{self.SEARCH_URL}?Show=Show")
            self.initialized = True

        result = self._request(
            self.SEARCH_URL,
            {
                "ListShow": "ListShow",
                "eqMgrCd": company.output_code,
                "dspSsuPd": "10",
            },
        ).decode("utf-8", "replace")
        if company.name not in result and company.sec_code not in result:
            return []

        detail = self._request(
            self.DETAIL_URL,
            {
                "BaseJh": "BaseJh",
                "mgrCd": company.sec_code,
                "jjHisiFlg": "1",
            },
        ).decode("utf-8", "replace")
        parser = LinkParser()
        parser.feed(detail)
        notices: list[dict[str, str]] = []
        for href, title in parser.links:
            if not re.fullmatch(r"/disc/[^\"?#]+\.pdf", href, re.IGNORECASE):
                continue
            notice_type = classify_notice(title)
            if notice_type is None:
                continue
            match = re.search(r"(20\d{6})", Path(href).name)
            if not match:
                continue
            published = datetime.strptime(match.group(1), "%Y%m%d").date()
            notices.append(
                {
                    "url": urllib.parse.urljoin(JPX_BASE, href),
                    "title": title,
                    "date": published.isoformat(),
                    "notice_type": notice_type,
                }
            )
        return notices

    def download(self, url: str) -> bytes:
        data = self._request(url)
        if not data.startswith(b"%PDF-"):
            raise EdinetError(f"JPXからPDF以外が返されました: {url}")
        return data


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日付はYYYY-MM-DD形式で指定してください") from exc


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_security_code(raw: str) -> tuple[str, str]:
    code = raw.strip().upper()
    if not re.fullmatch(r"[0-9A-Z]{4,5}", code):
        raise ValueError(f"証券コードは4桁または5桁で指定してください: {raw!r}")
    if len(code) == 4:
        return code + "0", code
    output_code = code[:-1] if code.endswith("0") else code
    return code, output_code


def read_companies(path: Path) -> dict[str, Company]:
    last_error: UnicodeDecodeError | None = None
    text = ""
    for encoding in ("utf-8-sig", "cp932"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    else:
        raise EdinetError(f"CSVの文字コードを判定できません: {last_error}")

    companies: dict[str, Company] = {}
    for line_no, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) < 2:
            raise EdinetError(f"{path}:{line_no}: 2列（証券コード,企業名）が必要です")
        raw_code, name = row[0].strip(), row[1].strip()
        if line_no == 1 and raw_code in {"証券コード", "コード", "secCode"}:
            continue
        try:
            sec_code, output_code = normalize_security_code(raw_code)
        except ValueError as exc:
            raise EdinetError(f"{path}:{line_no}: {exc}") from exc
        if not name:
            raise EdinetError(f"{path}:{line_no}: 企業名が空です")
        companies[sec_code] = Company(raw_code, sec_code, output_code, name)
    if not companies:
        raise EdinetError(f"対象企業が1件もありません: {path}")
    return companies


def safe_name(value: str, fallback: str = "unnamed", max_length: int = 100) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().rstrip(". ")
    value = re.sub(r"\s+", " ", value)
    if not value:
        value = fallback
    if value.upper() in WINDOWS_RESERVED:
        value = f"_{value}"
    return value[:max_length].rstrip(". ")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("／", "/")


def classify_notice(value: str) -> str | None:
    text = normalize_text(value)
    if "株主総会" not in text and "招集通知" not in text and "招集ご通知" not in text:
        return None
    if "決議ご通知" in text or "決議通知" in text:
        return None
    if not (
        "招集通知" in text
        or "招集ご通知" in text
        or "電子提供措置事項" in text
        or "交付書面省略事項" in text
        or "株主総会資料" in text
        or "参考書類" in text
    ):
        return None
    return "臨時株主総会招集通知" if "臨時株主総会" in text else "定時株主総会招集通知"


def extract_pdf_text(data: bytes, max_pages: int = 3) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise EdinetError(
            "招集通知PDFの内容判定には pypdf が必要です。"
            " `python -m pip install -r requirements.txt` を実行してください"
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(
            reader.pages[index].extract_text() or ""
            for index in range(min(max_pages, len(reader.pages)))
        )
    except Exception as exc:
        raise EdinetError(f"PDFの文字抽出に失敗しました: {exc}") from exc


def notice_directory(
    output: Path,
    company: Company,
    published: date,
    notice_type: str,
) -> Path:
    company_dir = safe_name(f"{company.output_code}_{company.name}")
    return output / company_dir / safe_name(f"{published.year}{notice_type}")


def save_notice(
    output: Path,
    company: Company,
    data: bytes,
    published: date,
    notice_type: str,
    source: str,
    identifier: str,
    title: str,
    url: str = "",
    doc_id: str = "",
) -> Notice:
    destination_dir = notice_directory(output, company, published, notice_type)
    filename = safe_name(
        f"{company.output_code}_{notice_type}_{source}_{identifier}.pdf",
        f"{company.output_code}_{source}_{identifier}.pdf",
        180,
    )
    destination = destination_dir / filename
    if not destination.exists() or destination.stat().st_size == 0:
        atomic_write(destination, data)
    return Notice(
        security_code=company.output_code,
        company_name=company.name,
        source=source,
        notice_type=notice_type,
        date=published.isoformat(),
        title=title,
        path=str(destination),
        url=url,
        doc_id=doc_id,
    )


def date_range(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)


def extract_zip_safely(data: bytes, destination: Path) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    extracted = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            parts = Path(info.filename.replace("\\", "/")).parts
            if (
                not parts
                or "__MACOSX" in parts
                or parts[-1] in {".DS_Store", "Thumbs.db"}
            ):
                continue
            target = (destination / Path(*parts)).resolve()
            if root != target and root not in target.parents:
                raise EdinetError(f"危険なZIPエントリを拒否しました: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            extracted += 1
    return extracted


def attachment_marker(output: Path, doc_id: str) -> Path:
    return output / ".edinet" / "attachments" / f"{doc_id}.json"


def attachment_archive_path(output: Path, doc_id: str) -> Path:
    return output / ".edinet" / "attachment-zips" / f"{doc_id}.zip"


def jpx_cache_path(output: Path, company: Company) -> Path:
    return output / ".edinet" / "jpx" / f"{company.sec_code}.json"


def notice_from_dict(data: dict[str, str]) -> Notice:
    return Notice(
        security_code=data["security_code"],
        company_name=data["company_name"],
        source=data["source"],
        notice_type=data["notice_type"],
        date=data["date"],
        title=data["title"],
        path=data["path"],
        url=data.get("url", ""),
        doc_id=data.get("doc_id", ""),
    )


def process_edinet_attachments(
    client: EdinetClient,
    output: Path,
    company: Company,
    document: dict[str, Any],
    dry_run: bool,
    refresh: bool = False,
) -> tuple[list[Notice], list[dict[str, str]]]:
    if document.get("docTypeCode") != "120" or document.get("attachDocFlag") != "1":
        return [], []

    doc_id = document["docID"]
    marker = attachment_marker(output, doc_id)
    if marker.exists() and not refresh:
        cached = json.loads(marker.read_text(encoding="utf-8"))
        return [notice_from_dict(item) for item in cached], []
    if dry_run:
        logging.info("[dry-run] %s EDINET添付文書を検査", doc_id)
        return [], []

    try:
        archive_path = attachment_archive_path(output, doc_id)
        if (
            archive_path.exists()
            and archive_path.stat().st_size > 0
            and not refresh
        ):
            data = archive_path.read_bytes()
            logging.info("保存済み添付ZIPを再検査: %s", archive_path)
        else:
            data, content_type = client.download_document(doc_id, "3")
            if content_type != "application/octet-stream":
                raise EdinetError(
                    f"{doc_id}: 添付ZIPに対して{content_type}が返されました"
                )
            atomic_write(archive_path, data)
            logging.info("添付ZIPを退避保存: %s", archive_path)

        submitted = date.fromisoformat(document["submitDateTime"][:10])
        notices: list[Notice] = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".pdf"):
                    continue
                pdf_data = archive.read(info)
                text = extract_pdf_text(pdf_data)
                notice_type = classify_notice(f"{info.filename}\n{text}")
                if notice_type is None:
                    continue
                notice = save_notice(
                    output,
                    company,
                    pdf_data,
                    submitted,
                    notice_type,
                    "EDINET",
                    f"{doc_id}_{Path(info.filename).stem}",
                    (text.strip().splitlines() or [Path(info.filename).name])[0],
                    doc_id=doc_id,
                )
                notices.append(notice)
                logging.info("EDINET添付から招集通知を保存: %s", notice.path)
        atomic_write(
            marker,
            json.dumps(
                [notice.__dict__ for notice in notices],
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
        )
        return notices, []
    except Exception as exc:
        logging.error("%s 添付文書の検査失敗: %s", doc_id, exc)
        return [], [
            {
                "docID": doc_id,
                "date": document.get("submitDateTime", "")[:10],
                "format": "attachments",
                "error": str(exc),
                "queuedAt": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
            }
        ]


def cache_path(output: Path, target_date: date) -> Path:
    return output / ".edinet" / "lists" / f"{target_date.isoformat()}.json"


def load_or_fetch_list(
    client: EdinetClient,
    output: Path,
    target_date: date,
    refresh: bool,
) -> dict[str, Any]:
    path = cache_path(output, target_date)
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    payload = client.list_documents(target_date)
    atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return payload


def is_target_document(
    document: dict[str, Any],
    companies: dict[str, Company],
    allowed_types: frozenset[str] | None = None,
) -> bool:
    if allowed_types is None:
        allowed_types = frozenset(SUPPORTED_TYPES)
    return (
        document.get("secCode") in companies
        and document.get("docTypeCode") in allowed_types
        and document.get("withdrawalStatus") == "0"
        and document.get("disclosureStatus") == "0"
        and document.get("legalStatus") in {"1", "2"}
    )


def document_directory(
    output: Path,
    company: Company,
    document: dict[str, Any],
) -> Path:
    submitted = document.get("submitDateTime", "")
    year = submitted[:4] if re.match(r"^\d{4}", submitted) else "unknown"
    type_name = SUPPORTED_TYPES[document["docTypeCode"]]
    company_dir = safe_name(f"{company.output_code}_{company.name}")
    return output / company_dir / safe_name(f"{year}{type_name}")


def download_one(
    client: EdinetClient,
    output: Path,
    company: Company,
    document: dict[str, Any],
    formats: tuple[str, ...],
    dry_run: bool,
) -> tuple[int, list[dict[str, str]]]:
    doc_id = document["docID"]
    doc_type = SUPPORTED_TYPES[document["docTypeCode"]]
    target_dir = document_directory(output, company, document)
    description = safe_name(document.get("docDescription") or doc_type, doc_type, 60)
    base = safe_name(f"{company.output_code}_{description}_{doc_id}", doc_id, 140)
    completed = 0
    failures: list[dict[str, str]] = []

    for format_name in formats:
        api_type, flag_name = DOWNLOAD_TYPES[format_name]
        if document.get(flag_name) != "1":
            logging.info("%s %s: %sなし", doc_id, format_name, flag_name)
            continue

        if format_name == "pdf":
            destination = target_dir / f"{base}.pdf"
            done = destination.exists() and destination.stat().st_size > 0
        else:
            destination = target_dir / f"{base}_{format_name}"
            done = (destination / ".complete").exists()

        if done:
            logging.info("既存のためスキップ: %s", destination)
            continue
        if dry_run:
            logging.info("[dry-run] %s %s -> %s", doc_id, format_name, destination)
            continue

        try:
            data, content_type = client.download_document(doc_id, api_type)
            if format_name == "pdf":
                if content_type != "application/pdf":
                    raise EdinetError(
                        f"{doc_id}: PDFに対して{content_type}が返されました"
                    )
                atomic_write(destination, data)
            else:
                if content_type != "application/octet-stream":
                    raise EdinetError(
                        f"{doc_id}: ZIPに対して{content_type}が返されました"
                    )
                count = extract_zip_safely(data, destination)
                if count == 0:
                    raise EdinetError(f"{doc_id}: ZIP内にファイルがありません")
                atomic_write(destination / ".complete", b"ok\n")
            completed += 1
            logging.info("保存しました: %s", destination)
        except Exception as exc:  # continue the batch and persist the retry queue
            logging.error("%s %s の取得失敗: %s", doc_id, format_name, exc)
            failures.append(
                {
                    "docID": doc_id,
                    "date": document.get("submitDateTime", "")[:10],
                    "format": format_name,
                    "error": str(exc),
                    "queuedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            )
    return completed, failures


def write_retry_queue(output: Path, failures: list[dict[str, str]]) -> None:
    path = output / ".edinet" / "retry-queue.json"
    if failures:
        atomic_write(
            path,
            json.dumps(failures, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    elif path.exists():
        path.unlink()


def write_notice_reports(
    output: Path,
    companies: dict[str, Company],
    notices: list[Notice],
    statuses: dict[str, tuple[str, str]],
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    fields = [
        "証券コード",
        "企業名",
        "取得元",
        "書類種別",
        "日付",
        "タイトル",
        "保存先",
        "URL",
        "docID",
    ]
    with (output / "shareholder-notices.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        for notice in sorted(
            notices,
            key=lambda item: (item.security_code, item.date, item.source, item.title),
        ):
            writer.writerow(
                [
                    notice.security_code,
                    notice.company_name,
                    notice.source,
                    notice.notice_type,
                    notice.date,
                    notice.title,
                    notice.path,
                    notice.url,
                    notice.doc_id,
                ]
            )

    with (output / "jpx-required.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "証券コード",
                "企業名",
                "状態",
                "備考",
                "書類種別",
                "日付",
                "タイトル",
                "保存先",
                "URL",
            ]
        )
        written_codes: set[str] = set()
        for notice in notices:
            if notice.source != "JPX":
                continue
            written_codes.add(notice.security_code)
            writer.writerow(
                [
                    notice.security_code,
                    notice.company_name,
                    "jpx_required",
                    "EDINETでは見つからずJPXから取得",
                    notice.notice_type,
                    notice.date,
                    notice.title,
                    notice.path,
                    notice.url,
                ]
            )
        for sec_code, (status, note) in statuses.items():
            company = companies[sec_code]
            if "jpx_required" not in status or company.output_code in written_codes:
                continue
            writer.writerow(
                [
                    company.output_code,
                    company.name,
                    status,
                    note,
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )

    with (output / "shareholder-notice-status.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["証券コード", "企業名", "状態", "備考"])
        for sec_code, company in sorted(companies.items()):
            status, note = statuses.get(sec_code, ("not_found", "該当書類なし"))
            writer.writerow([company.output_code, company.name, status, note])


def supplement_from_jpx(
    client: JPXClient,
    output: Path,
    companies: dict[str, Company],
    covered_years: set[tuple[str, int]],
    start: date,
    end: date,
    dry_run: bool,
    refresh_cache: bool = False,
) -> tuple[list[Notice], dict[str, tuple[str, str]], list[dict[str, str]]]:
    notices: list[Notice] = []
    statuses: dict[str, tuple[str, str]] = {}
    failures: list[dict[str, str]] = []
    required_years = set(range(start.year, end.year + 1))

    for sec_code, company in companies.items():
        company_covered = {
            year for code, year in covered_years if code == sec_code
        }
        missing_years = required_years - company_covered
        if not missing_years:
            statuses[sec_code] = (
                "edinet",
                f"EDINET添付文書から取得（{','.join(map(str, sorted(company_covered))) }年）",
            )
            continue
        logging.info("JPX補完を確認: %s %s", company.output_code, company.name)
        try:
            cache = jpx_cache_path(output, company)
            cached_candidates: list[dict[str, str]] | None = None
            if cache.exists() and not refresh_cache:
                cached = json.loads(cache.read_text(encoding="utf-8"))
                fetched_at = date.fromisoformat(cached["fetchedAt"][:10])
                current_range_is_fresh = end < date.today() or fetched_at == date.today()
                if cached.get("schemaVersion") == 1 and current_range_is_fresh:
                    cached_candidates = cached.get("candidates", [])
                    logging.info("JPX検索キャッシュを使用: %s", cache)

            if cached_candidates is None:
                cached_candidates = client.list_notices(company, start, end)
                atomic_write(
                    cache,
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "securityCode": company.output_code,
                            "companyName": company.name,
                            "fetchedAt": datetime.now()
                            .astimezone()
                            .isoformat(timespec="seconds"),
                            "candidates": cached_candidates,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ).encode("utf-8"),
                )

            candidates = [
                candidate
                for candidate in cached_candidates
                if start <= date.fromisoformat(candidate["date"]) <= end
                and date.fromisoformat(candidate["date"]).year in missing_years
            ]
            if not candidates:
                if company_covered:
                    statuses[sec_code] = (
                        "edinet_partial",
                        "EDINET取得年="
                        + ",".join(map(str, sorted(company_covered)))
                        + "、JPX該当なし年="
                        + ",".join(map(str, sorted(missing_years))),
                    )
                else:
                    statuses[sec_code] = (
                        "not_found",
                        "EDINET・JPXとも該当書類なし",
                    )
                continue
            for candidate in candidates:
                published = date.fromisoformat(candidate["date"])
                identifier = Path(urllib.parse.urlparse(candidate["url"]).path).stem
                destination = notice_directory(
                    output, company, published, candidate["notice_type"]
                ) / safe_name(
                    f"{company.output_code}_{candidate['notice_type']}_JPX_{identifier}.pdf",
                    f"{company.output_code}_JPX_{identifier}.pdf",
                    180,
                )
                if dry_run:
                    logging.info("[dry-run] JPX %s -> %s", candidate["url"], destination)
                    notice = Notice(
                        security_code=company.output_code,
                        company_name=company.name,
                        source="JPX",
                        notice_type=candidate["notice_type"],
                        date=candidate["date"],
                        title=candidate["title"],
                        path=str(destination),
                        url=candidate["url"],
                    )
                else:
                    data = (
                        destination.read_bytes()
                        if destination.exists() and destination.stat().st_size > 0
                        else client.download(candidate["url"])
                    )
                    notice = save_notice(
                        output,
                        company,
                        data,
                        published,
                        candidate["notice_type"],
                        "JPX",
                        identifier,
                        candidate["title"],
                        url=candidate["url"],
                    )
                    logging.info("JPXから招集通知を保存: %s", notice.path)
                notices.append(notice)
            status_name = (
                "edinet+jpx_required" if company_covered else "jpx_required"
            )
            statuses[sec_code] = (
                status_name,
                f"JPXから{len(candidates)}件取得"
                if not dry_run
                else f"JPXに{len(candidates)}件（dry-run）",
            )
        except Exception as exc:
            statuses[sec_code] = ("error", str(exc))
            failures.append(
                {
                    "docID": "",
                    "date": "",
                    "format": "jpx",
                    "error": f"{company.output_code} {company.name}: {exc}",
                    "queuedAt": datetime.now().astimezone().isoformat(
                        timespec="seconds"
                    ),
                }
            )
    return notices, statuses, failures


def run_download(args: argparse.Namespace) -> int:
    api_key = os.environ.get("EDINET_API_KEY", "").strip()
    if not api_key:
        raise EdinetError("環境変数 EDINET_API_KEY を設定してください")
    if args.end < args.start:
        raise EdinetError("--end は --start 以降の日付にしてください")

    companies = read_companies(args.companies)
    logging.info("対象企業: %s社", len(companies))
    client = EdinetClient(
        api_key,
        (args.list_delay_min, args.list_delay_max),
        (args.download_delay_min, args.download_delay_max),
    )
    failures: list[dict[str, str]] = []
    notices: list[Notice] = []
    matched = 0
    downloaded = 0

    allowed_types = frozenset(args.doc_types)
    for target_date in date_range(args.start, args.end):
        logging.info("一覧処理: %s", target_date)
        try:
            payload = load_or_fetch_list(
                client, args.output, target_date, args.refresh_lists
            )
        except Exception as exc:
            logging.error("%s の一覧取得失敗: %s", target_date, exc)
            failures.append(
                {
                    "docID": "",
                    "date": target_date.isoformat(),
                    "format": "list",
                    "error": str(exc),
                    "queuedAt": datetime.now().astimezone().isoformat(
                        timespec="seconds"
                    ),
                }
            )
            continue

        for document in payload.get("results", []):
            if not is_target_document(document, companies, allowed_types):
                continue
            matched += 1
            company = companies[document["secCode"]]
            count, document_failures = download_one(
                client,
                args.output,
                company,
                document,
                tuple(args.formats),
                args.dry_run,
            )
            downloaded += count
            failures.extend(document_failures)
            if not args.no_shareholder_docs:
                found, attachment_failures = process_edinet_attachments(
                    client,
                    args.output,
                    company,
                    document,
                    args.dry_run,
                    args.refresh_attachments,
                )
                notices.extend(found)
                failures.extend(attachment_failures)

    if not args.no_shareholder_docs:
        covered_years = {
            (company.sec_code, date.fromisoformat(notice.date).year)
            for company in companies.values()
            for notice in notices
            if notice.security_code == company.output_code
            and notice.source == "EDINET"
        }
        if args.no_jpx:
            required_years = set(range(args.start.year, args.end.year + 1))
            statuses = {}
            for code in companies:
                found_years = {
                    year for covered_code, year in covered_years if covered_code == code
                }
                missing_years = required_years - found_years
                statuses[code] = (
                    (
                        "edinet"
                        if not missing_years
                        else (
                            "edinet+jpx_required"
                            if found_years
                            else "jpx_required"
                        )
                    ),
                    (
                        "EDINET添付文書から取得"
                        if not missing_years
                        else "JPX補完が必要な年="
                        + ",".join(map(str, sorted(missing_years)))
                    ),
                )
        else:
            jpx_client = JPXClient((args.jpx_delay_min, args.jpx_delay_max))
            jpx_notices, statuses, jpx_failures = supplement_from_jpx(
                jpx_client,
                args.output,
                companies,
                covered_years,
                args.start,
                args.end,
                args.dry_run,
                args.refresh_jpx,
            )
            notices.extend(jpx_notices)
            failures.extend(jpx_failures)
        write_notice_reports(args.output, companies, notices, statuses)

    if not args.dry_run:
        write_retry_queue(args.output, failures)
    logging.info(
        "完了: 該当書類=%s、今回保存=%s、再取得キュー=%s",
        matched,
        downloaded,
        len(failures),
    )
    return 1 if failures else 0


def decode_code_list(data: bytes) -> list[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not names:
            raise EdinetError("EDINETコードリストZIPにCSVがありません")
        text = archive.read(names[0]).decode("cp932")
    lines = text.splitlines()
    if len(lines) < 2:
        raise EdinetError("EDINETコードリストCSVが空です")
    return list(csv.DictReader(lines[1:]))


def run_companies(args: argparse.Namespace) -> int:
    request = urllib.request.Request(
        CODE_LIST_URL,
        headers={"User-Agent": "edinet-batch-downloader/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            rows = decode_code_list(response.read())
    except (urllib.error.URLError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise EdinetError(f"EDINETコードリストの取得・解析に失敗しました: {exc}") from exc

    companies: dict[str, str] = {}
    for row in rows:
        sec_code = (row.get("証券コード") or "").strip()
        listed = (row.get("上場区分") or "").strip()
        name = (row.get("提出者名") or "").strip()
        if not sec_code or not name:
            continue
        if not args.include_unlisted and listed != "上場":
            continue
        try:
            _, output_code = normalize_security_code(sec_code)
        except ValueError:
            continue
        companies[output_code] = name

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["証券コード", "企業名"])
        writer.writerows(sorted(companies.items()))
    logging.info("%s社を出力しました: %s", len(companies), args.output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="EDINETから指定企業の開示書類を一括取得します"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="詳細ログを表示します"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser("download", help="書類を取得します")
    download.add_argument("companies", type=Path, help="証券コード,企業名のCSV")
    download.add_argument("--output", type=Path, default=Path("output"))
    download.add_argument("--start", type=parse_date, default=date(2025, 1, 1))
    download.add_argument("--end", type=parse_date, default=date.today())
    download.add_argument(
        "--formats",
        nargs="+",
        choices=tuple(DOWNLOAD_TYPES),
        default=list(DOWNLOAD_TYPES),
    )
    download.add_argument(
        "--doc-types",
        nargs="+",
        choices=tuple(SUPPORTED_TYPES),
        default=list(SUPPORTED_TYPES),
        metavar="TYPE",
        help=(
            "取得する書類種別コード（複数可）。"
            + "、".join(f"{k}={v}" for k, v in SUPPORTED_TYPES.items())
            + "。デフォルトは全種別"
        ),
    )
    download.add_argument("--list-delay-min", type=float, default=1.0)
    download.add_argument("--list-delay-max", type=float, default=3.0)
    download.add_argument("--download-delay-min", type=float, default=3.0)
    download.add_argument("--download-delay-max", type=float, default=10.0)
    download.add_argument("--jpx-delay-min", type=float, default=3.0)
    download.add_argument("--jpx-delay-max", type=float, default=10.0)
    download.add_argument(
        "--no-shareholder-docs",
        action="store_true",
        help="株主総会招集通知のEDINET検査・JPX補完を行いません",
    )
    download.add_argument(
        "--no-jpx",
        action="store_true",
        help="JPXからの補完取得を行わず、必要企業のリストだけ出力します",
    )
    download.add_argument("--refresh-lists", action="store_true")
    download.add_argument("--refresh-attachments", action="store_true")
    download.add_argument("--refresh-jpx", action="store_true")
    download.add_argument("--dry-run", action="store_true")
    download.set_defaults(handler=run_download)

    companies = subparsers.add_parser(
        "companies", help="公式コードリストから企業CSVを生成します"
    )
    companies.add_argument("--output", type=Path, default=Path("companies.csv"))
    companies.add_argument("--include-unlisted", action="store_true")
    companies.set_defaults(handler=run_companies)
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.stderr.reconfigure(encoding='utf-8')
    load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return args.handler(args)
    except (EdinetError, OSError, ValueError) as exc:
        logging.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
