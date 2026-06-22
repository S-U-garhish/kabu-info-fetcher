import csv
import io
import shutil
import unittest
import zipfile
from pathlib import Path

import edinet_downloader as edinet


class EdinetDownloaderTests(unittest.TestCase):
    def test_normalize_security_code(self):
        self.assertEqual(edinet.normalize_security_code("7203"), ("72030", "7203"))
        self.assertEqual(edinet.normalize_security_code("72030"), ("72030", "7203"))
        self.assertEqual(edinet.normalize_security_code("130A"), ("130A0", "130A"))

    def test_read_companies_with_header(self):
        path = Path(".test_companies.csv")
        try:
            path.write_text(
                "証券コード,企業名\n7203,トヨタ自動車\n", encoding="utf-8-sig"
            )
            companies = edinet.read_companies(path)
            self.assertEqual(companies["72030"].name, "トヨタ自動車")
        finally:
            path.unlink(missing_ok=True)

    def test_target_document_filter(self):
        companies = {
            "72030": edinet.Company("7203", "72030", "7203", "トヨタ自動車")
        }
        document = {
            "secCode": "72030",
            "docTypeCode": "120",
            "withdrawalStatus": "0",
            "disclosureStatus": "0",
            "legalStatus": "1",
        }
        self.assertTrue(edinet.is_target_document(document, companies))
        document["docTypeCode"] = "030"
        self.assertFalse(edinet.is_target_document(document, companies))

    def test_safe_zip_extraction_rejects_traversal(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("../escape.txt", "bad")
        destination = Path(".test_extract")
        try:
            with self.assertRaises(edinet.EdinetError):
                edinet.extract_zip_safely(stream.getvalue(), destination)
        finally:
            shutil.rmtree(destination, ignore_errors=True)

    def test_decode_official_code_list_shape(self):
        csv_text = (
            "ダウンロード実行日,2026年06月20日現在,件数,1件\r\n"
            "ＥＤＩＮＥＴコード,提出者種別,上場区分,提出者名,証券コード\r\n"
            '"E00001","内国法人・組合","上場","株式会社テスト","12340"\r\n'
        )
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("EdinetcodeDlInfo.csv", csv_text.encode("cp932"))
        rows = edinet.decode_code_list(stream.getvalue())
        self.assertEqual(rows[0]["証券コード"], "12340")

    def test_classify_shareholder_documents(self):
        self.assertEqual(
            edinet.classify_notice("第121回 定時株主総会 招集ご通知"),
            "定時株主総会招集通知",
        )
        self.assertEqual(
            edinet.classify_notice("臨時株主総会招集通知"),
            "臨時株主総会招集通知",
        )
        self.assertEqual(
            edinet.classify_notice("株主総会 その他の電子提供措置事項"),
            "定時株主総会招集通知",
        )
        self.assertIsNone(edinet.classify_notice("定時株主総会決議ご通知"))
        self.assertIsNone(
            edinet.classify_notice("株主総会後の取締役体制について")
        )

    def test_jpx_link_parser(self):
        parser = edinet.LinkParser()
        parser.feed(
            '<a href="/disc/72030/140120250513546491.pdf">'
            "2025年定時株主総会招集通知</a>"
        )
        self.assertEqual(
            parser.links,
            [
                (
                    "/disc/72030/140120250513546491.pdf",
                    "2025年定時株主総会招集通知",
                )
            ],
        )

    def test_attachment_zip_is_saved_before_inspection(self):
        class FakeClient:
            def download_document(self, doc_id, api_type):
                if (doc_id, api_type) != ("S100TEST", "3"):
                    raise AssertionError((doc_id, api_type))
                return b"not-a-zip", "application/octet-stream"

        output = Path(".test_attachment_output")
        shutil.rmtree(output, ignore_errors=True)
        company = edinet.Company("7203", "72030", "7203", "トヨタ自動車")
        document = {
            "docID": "S100TEST",
            "docTypeCode": "120",
            "attachDocFlag": "1",
            "submitDateTime": "2025-06-18 10:00",
        }
        try:
            notices, failures = edinet.process_edinet_attachments(
                FakeClient(), output, company, document, False
            )
            self.assertEqual(notices, [])
            self.assertEqual(len(failures), 1)
            self.assertEqual(
                edinet.attachment_archive_path(output, "S100TEST").read_bytes(),
                b"not-a-zip",
            )
        finally:
            shutil.rmtree(output, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
