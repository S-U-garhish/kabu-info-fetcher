import shutil
import unittest
import zipfile
from pathlib import Path

import package_handoff as package


class PackageHandoffTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(".test_package")
        shutil.rmtree(self.root, ignore_errors=True)
        (self.root / "output" / "7203_トヨタ").mkdir(parents=True)
        (self.root / "output" / "7203_トヨタ" / "report.pdf").write_bytes(
            b"%PDF-test"
        )
        (self.root / "output" / "writing.pdf.part").write_bytes(b"partial")
        (self.root / "output" / ".edinet" / "lists").mkdir(parents=True)
        (self.root / "output" / ".edinet" / "lists" / "2025-01-01.json").write_text(
            "{}", encoding="utf-8"
        )
        incomplete = self.root / "output" / "7203_トヨタ" / "doc_xbrl"
        incomplete.mkdir()
        (incomplete / "partial.xbrl").write_text("partial", encoding="utf-8")
        complete = self.root / "output" / "7203_トヨタ" / "doc_csv"
        complete.mkdir()
        (complete / "data.csv").write_text("ok", encoding="utf-8")
        (complete / ".complete").write_text("ok", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_collect_results_excludes_state_and_parts_by_default(self):
        results, state = package.collect_results(self.root / "output", False)
        self.assertEqual(
            [item.source.name for item in results],
            ["data.csv", "report.pdf"],
        )
        self.assertEqual(state, [])

    def test_collect_results_can_include_resume_state(self):
        results, state = package.collect_results(self.root / "output", True)
        self.assertEqual(len(results), 2)
        self.assertEqual([item.source.name for item in state], ["2025-01-01.json"])

    def test_write_zip(self):
        results, _ = package.collect_results(self.root / "output", False)
        destination = self.root / "results.zip"
        package.write_zip(destination, results, zipfile.ZIP_DEFLATED)
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(
                archive.namelist(),
                [
                    "output/7203_トヨタ/doc_csv/data.csv",
                    "output/7203_トヨタ/report.pdf",
                ],
            )

    def test_partition_files(self):
        results, _ = package.collect_results(self.root / "output", False)
        parts = package.partition_files(results, results[0].size)
        self.assertEqual([len(part) for part in parts], [1, 1])


if __name__ == "__main__":
    unittest.main()
