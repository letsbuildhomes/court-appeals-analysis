"""Offline, temporary-PDF checks for the resumable text stages."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

import text_finish
from text_finish import TextFinishPipeline, choose_page, tesseract_result, word_confidence
from text_pipeline import TextPipeline, pgm_ink_ratio, triage_decision


def one_page_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


@unittest.skipUnless(all(shutil.which(tool) for tool in
                         ("pdfinfo", "pdftotext", "pdfimages", "pdftoppm")),
                     "Poppler tools required")
class TextPipelineTests(unittest.TestCase):
    def test_stages_checkpoint_and_skip_completed_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdf_bytes = one_page_pdf("Hello Vermont court world")
            sha = hashlib.sha256(pdf_bytes).hexdigest()
            relative = f"in/pdfs/{sha}.pdf"
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_bytes(pdf_bytes)
            archive_path = root / "data" / "archive.duckdb"
            archive_path.parent.mkdir(parents=True)
            archive = duckdb.connect(str(archive_path))
            archive.execute("""
                CREATE TABLE downloads (
                    media_url VARCHAR, status VARCHAR, error VARCHAR,
                    sha256 VARCHAR, raw_path VARCHAR, byte_count BIGINT
                )
            """)
            archive.execute(
                "INSERT INTO downloads VALUES (?, 'downloaded', NULL, ?, ?, ?)",
                ["https://www.vtcourts.gov/media/example", sha, relative, len(pdf_bytes)],
            )
            archive.execute(
                "INSERT INTO downloads VALUES (?, 'failed', ?, NULL, NULL, NULL)",
                ["https://www.vtcourts.gov/media/non-pdf", "not a PDF"],
            )
            archive.close()

            pipeline = TextPipeline(root, archive_path, root / "data" / "text.duckdb")
            try:
                self.assertEqual(pipeline.inventory()["processed"], 1)
                self.assertEqual(pipeline.native()["processed"], 1)
                self.assertEqual(pipeline.triage()["processed"], 1)
                row = pipeline.conn.execute("""
                    SELECT p.text, t.decision FROM native_pages p JOIN page_triage t
                    USING (pdf_sha256, page_number) WHERE p.pdf_sha256=?
                """, [sha]).fetchone()
                self.assertIn("Hello Vermont", row[0])
                self.assertEqual(row[1], "native_ok")
                self.assertEqual(pipeline.inventory()["skipped"], 1)
                self.assertEqual(pipeline.native()["skipped"], 1)
                self.assertEqual(pipeline.triage()["skipped"], 1)
                self.assertEqual(
                    pipeline.conn.execute("SELECT count(*) FROM source_issues").fetchone()[0], 1
                )
                pipeline.conn.execute(
                    "DELETE FROM native_pages WHERE pdf_sha256=?", [sha]
                )
                self.assertEqual(pipeline.native()["processed"], 1)
                pipeline.conn.execute(
                    "DELETE FROM page_triage WHERE pdf_sha256=?", [sha]
                )
                self.assertEqual(pipeline.triage()["processed"], 1)
            finally:
                pipeline.close()

    def test_blank_and_scan_decisions_remain_distinct(self) -> None:
        self.assertEqual(triage_decision(0, 0, 0.0, [])[0], "blank")
        self.assertEqual(triage_decision(0, 0, 0.15, [(2550, 3300)])[0], "ocr_needed")
        self.assertEqual(triage_decision(5, 0, 0.03, [])[0], "ocr_candidate")

    def test_pgm_dark_pixel_ratio(self) -> None:
        self.assertEqual(pgm_ink_ratio(b"P5\n2 2\n255\n\xff\x00\xff\xff"), 0.25)

    def test_text_selection_is_conservative(self) -> None:
        self.assertEqual(choose_page("native_ok", "Court order", 10, 0,
                                     None, None, None, None, None, None)[:2],
                         ("usable", "native"))
        self.assertEqual(choose_page("blank", "", 0, 0,
                                     None, None, None, None, None, None)[0], "blank")
        self.assertEqual(choose_page("ocr_needed", "", 0, 0,
                                     "complete", "The court issues a decision.",
                                     23, 5, 0, 90)[:2], ("usable", "ocr"))
        self.assertEqual(choose_page("ocr_candidate", "Judge", 5, 0,
                                     "failed", None, None, None, None, None)[0],
                         "unresolved")
        self.assertEqual(choose_page("ocr_candidate", "Court order", 10, 0,
                                     "complete", "x", 1, 1, 0, 95)[0],
                         "unresolved")
        self.assertEqual(choose_page("ocr_candidate", "Environmental Judge", 20, 0,
                                     "complete", "Environmental Judge", 20, 2, 0, 90)[:2],
                         ("usable", "native"))

    def test_ocr_confidence_uses_word_scores(self) -> None:
        self.assertEqual(word_confidence([0.9, 0.3]), (60.0, 0.5, 2))

    def test_tesseract_tsv_words_and_empty_result(self) -> None:
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        text, details, scores = tesseract_result(header)
        self.assertEqual(text, "")
        self.assertEqual(details, '{"lines": [], "words": []}')
        self.assertEqual(scores, [])
        text, details, scores = tesseract_result(
            header + "5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t91.5\tVermont\n"
            + "5\t1\t1\t1\t1\t2\t45\t20\t30\t12\t72\tCourt\n"
        )
        self.assertEqual(text, "Vermont Court")
        self.assertEqual(scores, [0.915, 0.72])
        self.assertIn('"text": "Vermont"', details)
        text, details, scores = tesseract_result(
            header + '5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t91\t"quoted\n'
            + '5\t1\t1\t1\t1\t2\t45\t20\t30\t12\t92\tfollowing\n'
        )
        self.assertEqual(text, '"quoted following')
        self.assertEqual(len(json.loads(details)["words"]), 2)
        self.assertEqual(scores, [0.91, 0.92])

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract required")
    def test_ocr_selection_audit_resume_and_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdf_bytes = one_page_pdf("Vermont Environmental Court decision on appeal")
            sha = hashlib.sha256(pdf_bytes).hexdigest()
            relative = f"in/pdfs/{sha}.pdf"
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_bytes(pdf_bytes)
            archive_path = root / "data" / "archive.duckdb"
            archive_path.parent.mkdir(parents=True)
            archive = duckdb.connect(str(archive_path))
            archive.execute("""
                CREATE TABLE downloads (
                    media_url VARCHAR, status VARCHAR, error VARCHAR,
                    sha256 VARCHAR, raw_path VARCHAR, byte_count BIGINT
                )
            """)
            archive.execute("INSERT INTO downloads VALUES (?, 'downloaded', NULL, ?, ?, ?)",
                            ["https://example.test/pdf", sha, relative, len(pdf_bytes)])
            archive.close()
            with patch.object(text_finish, "OCR_LONG_EDGE", 1400):
                pipeline = TextFinishPipeline(root, archive_path, root / "data" / "text.duckdb")
                try:
                    pipeline.inventory()
                    pipeline.native()
                    pipeline.triage()
                    pipeline.conn.execute("""
                        UPDATE page_triage SET decision='ocr_candidate',
                            reason='test_ambiguous_text' WHERE pdf_sha256=?
                    """, [sha])
                    self.assertEqual(pipeline.ocr()["processed_pages"], 1)
                    self.assertEqual(pipeline.ocr()["skipped_pages"], 1)
                    self.assertEqual(pipeline.select()["processed_documents"], 1)
                    self.assertEqual(pipeline.audit()["processed_documents"], 1)
                    self.assertEqual(pipeline.select()["skipped_documents"], 1)
                    self.assertEqual(pipeline.audit()["skipped_documents"], 1)
                    row = pipeline.conn.execute("""
                        SELECT status, text FROM current_analysis_pages
                        WHERE pdf_sha256=?
                    """, [sha]).fetchone()
                    self.assertEqual(row[0], "usable")
                    self.assertIn("Vermont", row[1])
                    version = pipeline.active_version("ocr")
                    pipeline.record_ocr(sha, 1, version, pipeline.triage_version,
                                        "Vermont Environmental Court decision on appeal",
                                        '{"lines": [], "words": []}', [0.9], None)
                    self.assertEqual(pipeline.select()["processed_documents"], 1)
                    self.assertEqual(pipeline.audit()["processed_documents"], 1)
                    pipeline.record_ocr(sha, 1, version, pipeline.triage_version,
                                        None, None, None, "temporary OCR failure")
                    self.assertEqual(pipeline.ocr()["processed_pages"], 1)
                    self.assertEqual(pipeline.conn.execute("""
                        SELECT status FROM current_ocr_pages WHERE pdf_sha256=?
                    """, [sha]).fetchone()[0], "complete")
                finally:
                    pipeline.close()
                with patch("text_pipeline.poppler_version", side_effect=AssertionError("Poppler called")):
                    reopened = TextFinishPipeline(
                        root, archive_path, root / "data" / "text.duckdb",
                        existing_versions=True,
                    )
                    try:
                        self.assertEqual(reopened.active_version("ocr"), version)
                    finally:
                        reopened.close()


if __name__ == "__main__":
    unittest.main()
