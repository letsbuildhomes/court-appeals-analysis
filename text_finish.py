"""OCR queued PDF pages, choose analysis text, and audit page coverage.

The PDF archive is read-only. All results and checkpoints are in text.duckdb.
Run the inventory/native/triage stages in text_pipeline.py first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import uuid
from collections import Counter
from itertools import groupby
from pathlib import Path

from text_pipeline import (
    TextPipeline,
    command,
    native_metrics,
    now_utc,
    transaction,
)


OCR_RULE = "tesseract-page-v2"
PREVIOUS_OCR_RULE = "tesseract-page-v1"
SELECTION_RULE = "selection-v1"
AUDIT_RULE = "audit-v1"
OCR_LONG_EDGE = 2200


def ocr_version() -> str:
    tesseract = command(["tesseract", "--version"])
    renderer = command(["pdftoppm", "-v"])
    tesseract_version = (tesseract.stdout + tesseract.stderr).decode(
        "utf-8", errors="replace"
    ).splitlines()[0].strip()
    renderer_version = (renderer.stdout + renderer.stderr).decode(
        "utf-8", errors="replace"
    ).splitlines()[0].strip()
    return (f"{OCR_RULE}|{tesseract_version}|{renderer_version}|"
            f"lang=eng|psm=3|long_edge={OCR_LONG_EDGE}|tsv=1")


def word_confidence(scores: list[float]) -> tuple[float | None, float | None, int]:
    values = [value * 100 for value in scores if 0 <= value <= 1]
    if not values:
        return None, None, 0
    return sum(values) / len(values), sum(value < 50 for value in values) / len(values), len(values)


def tesseract_result(tsv: str) -> tuple[str, str, list[float]]:
    """Extract line text and scored words from Tesseract's level-5 TSV rows."""
    lines: dict[tuple[int, int, int, int], list[str]] = {}
    words = []
    # Tesseract does not CSV-escape quotes in its final text column. Split at
    # the first 11 tabs so a literal quote cannot consume subsequent rows.
    for line in tsv.splitlines()[1:]:
        fields = line.split("\t", 11)
        if len(fields) != 12 or fields[0] != "5" or not (word := fields[11].strip()):
            continue
        try:
            confidence = float(fields[10])
            box = [int(value) for value in fields[6:10]]
            line_key = tuple(int(value) for value in fields[1:5])
        except ValueError:
            continue
        if not 0 <= confidence <= 100:
            continue
        lines.setdefault(line_key, []).append(word)
        words.append({"text": word, "score": confidence / 100, "box": box})
    line_text = [" ".join(parts) for parts in lines.values()]
    details = json.dumps({"lines": line_text, "words": words}, ensure_ascii=False)
    return "\n".join(line_text), details, [word["score"] for word in words]


def ocr_page(path: Path, page_number: int, folder: Path) -> tuple[str, str, list[float], str]:
    prefix = folder / f"page_{page_number:05d}"
    command([
        "pdftoppm", "-f", str(page_number), "-l", str(page_number),
        "-scale-to", str(OCR_LONG_EDGE), "-gray", "-singlefile", "-png",
        str(path), str(prefix),
    ])
    image = prefix.with_suffix(".png")
    if not image.is_file():
        raise RuntimeError(f"pdftoppm did not produce page {page_number}")
    try:
        tsv = command([
            "tesseract", str(image), "stdout", "-l", "eng", "--psm", "3", "tsv"
        ]).stdout.decode("utf-8", errors="replace")
    finally:
        image.unlink(missing_ok=True)
    text, details, scores = tesseract_result(tsv)
    return text, details, scores, tsv


def token_coverage(needle: str, haystack: str) -> float:
    """Share of meaningful OCR words also found in native text, ignoring layout."""
    ocr_words = re.findall(r"[a-z0-9]{2,}", needle.casefold())
    native_words = set(re.findall(r"[a-z0-9]{2,}", haystack.casefold()))
    return sum(word in native_words for word in ocr_words) / len(ocr_words) if ocr_words else 0.0


def choose_page(decision: str, native_text: str, native_alnum: int,
                native_bad: int, ocr_status: str | None, ocr_text: str | None,
                ocr_alnum: int | None, ocr_words: int | None,
                ocr_bad: int | None, ocr_mean_conf: float | None) -> tuple[str, str | None, str]:
    """Return (status, source, reason); preserve both raw extractions elsewhere."""
    if decision == "native_ok":
        return "usable", "native", "triage_native_ok"
    if decision == "blank":
        return "blank", None, "triage_blank"
    if decision not in {"ocr_needed", "ocr_candidate"}:
        return "unresolved", None, "unknown_triage_decision"
    if ocr_status != "complete":
        return "unresolved", None, "ocr_missing_or_failed"
    if (not ocr_text or (ocr_alnum or 0) < 10 or (ocr_words or 0) < 2
            or ocr_mean_conf is None or ocr_mean_conf < 45
            or (ocr_bad or 0) > max(5, (ocr_alnum or 0) // 100)):
        return "unresolved", None, "ocr_quality_insufficient"
    if native_alnum < 20 or native_bad > max(5, native_alnum // 100):
        return "usable", "ocr", "native_missing_or_garbled"
    if (ocr_alnum or 0) >= native_alnum + max(40, native_alnum // 2):
        return "usable", "ocr", "ocr_recovers_more_text"
    if (ocr_alnum or 0) <= native_alnum * 1.25 and token_coverage(
        ocr_text, native_text
    ) >= 0.75:
        return "usable", "native", "ocr_confirms_native_text"
    return "unresolved", None, "native_ocr_disagree"


class TextFinishPipeline(TextPipeline):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.create_finish_schema()

    def active_version(self, stage: str) -> str | None:
        row = self.conn.execute(
            "SELECT version FROM current_versions WHERE stage=?", [stage]
        ).fetchone()
        return row[0] if row else None

    def start_run(self, stage: str, version: str) -> str:
        # DuckDB allows only one writer; any older running row belongs to a
        # process that ended before this process acquired the database.
        self.conn.execute("""
            UPDATE stage_runs SET status='interrupted', finished_at=?
            WHERE status='running'
        """, [now_utc()])
        return super().start_run(stage, version)

    def set_active_version(self, stage: str, version: str) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM current_versions WHERE stage=?", [stage])
            self.conn.execute("INSERT INTO current_versions VALUES (?, ?)", [stage, version])

    def create_finish_schema(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ocr_pages (
                pdf_sha256 VARCHAR NOT NULL,
                page_number INTEGER NOT NULL,
                ocr_version VARCHAR NOT NULL,
                triage_version VARCHAR NOT NULL,
                result_id VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                text VARCHAR,
                word_tsv VARCHAR,
                detail_json VARCHAR,
                alnum_count INTEGER,
                word_count INTEGER,
                bad_char_count INTEGER,
                mean_word_confidence DOUBLE,
                low_confidence_share DOUBLE,
                confidence_word_count INTEGER,
                error VARCHAR,
                processed_at VARCHAR NOT NULL,
                PRIMARY KEY (pdf_sha256, page_number, ocr_version)
            )
        """)
        self.conn.execute("ALTER TABLE ocr_pages ADD COLUMN IF NOT EXISTS detail_json VARCHAR")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS selection_documents (
                pdf_sha256 VARCHAR NOT NULL,
                selection_version VARCHAR NOT NULL,
                input_signature VARCHAR NOT NULL,
                result_id VARCHAR NOT NULL,
                page_count INTEGER NOT NULL,
                selected_at VARCHAR NOT NULL,
                PRIMARY KEY (pdf_sha256, selection_version)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS selected_pages (
                pdf_sha256 VARCHAR NOT NULL,
                page_number INTEGER NOT NULL,
                selection_version VARCHAR NOT NULL,
                native_version VARCHAR NOT NULL,
                triage_version VARCHAR NOT NULL,
                ocr_version VARCHAR NOT NULL,
                ocr_result_id VARCHAR,
                status VARCHAR NOT NULL,
                source VARCHAR,
                reason VARCHAR NOT NULL,
                selected_at VARCHAR NOT NULL,
                PRIMARY KEY (pdf_sha256, page_number, selection_version)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_documents (
                pdf_sha256 VARCHAR NOT NULL,
                audit_version VARCHAR NOT NULL,
                selection_result_id VARCHAR,
                expected_pages INTEGER NOT NULL,
                selected_pages INTEGER NOT NULL,
                unresolved_pages INTEGER NOT NULL,
                flagged_pages INTEGER NOT NULL,
                status VARCHAR NOT NULL,
                audited_at VARCHAR NOT NULL,
                PRIMARY KEY (pdf_sha256, audit_version)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_page_flags (
                pdf_sha256 VARCHAR NOT NULL,
                page_number INTEGER NOT NULL,
                audit_version VARCHAR NOT NULL,
                flags_json VARCHAR NOT NULL,
                PRIMARY KEY (pdf_sha256, page_number, audit_version)
            )
        """)
        self.conn.execute("""
            CREATE OR REPLACE VIEW current_ocr_pages AS
            SELECT o.* FROM ocr_pages o WHERE o.ocr_version = (
                SELECT version FROM current_versions WHERE stage='ocr'
            )
        """)
        self.conn.execute("""
            CREATE OR REPLACE VIEW current_selected_pages AS
            SELECT s.* FROM selected_pages s WHERE s.selection_version = (
                SELECT version FROM current_versions WHERE stage='selection'
            )
        """)
        self.conn.execute("""
            CREATE OR REPLACE VIEW current_analysis_pages AS
            SELECT s.pdf_sha256, s.page_number, s.status, s.source, s.reason,
                   CASE WHEN s.status='usable' AND s.source='native' THEN n.text
                        WHEN s.status='usable' AND s.source='ocr' THEN o.text
                        ELSE NULL END AS text,
                   s.native_version, s.triage_version, s.ocr_version,
                   o.mean_word_confidence AS ocr_mean_word_confidence
            FROM current_selected_pages s
            LEFT JOIN native_pages n ON n.pdf_sha256=s.pdf_sha256
                AND n.page_number=s.page_number
                AND n.extractor_version=s.native_version
            LEFT JOIN ocr_pages o ON o.pdf_sha256=s.pdf_sha256
                AND o.page_number=s.page_number AND o.ocr_version=s.ocr_version
        """)
        self.conn.execute("""
            CREATE OR REPLACE VIEW current_audit_documents AS
            SELECT a.* FROM audit_documents a WHERE a.audit_version = (
                SELECT version FROM current_versions WHERE stage='audit'
            )
        """)

    def record_ocr(self, sha: str, page: int, version: str, triage_version: str,
                   text: str | None, details: str | None,
                   scores: list[float] | None, error: str | None,
                   word_tsv: str | None = None) -> None:
        alnum, words, bad = native_metrics(text) if text is not None else (None, None, None)
        mean, low_share, confidence_words = word_confidence(scores or [])
        with transaction(self.conn):
            self.conn.execute("""
                DELETE FROM ocr_pages WHERE pdf_sha256=? AND page_number=? AND ocr_version=?
            """, [sha, page, version])
            self.conn.execute("""
                INSERT INTO ocr_pages (
                    pdf_sha256, page_number, ocr_version, triage_version,
                    result_id, status, text, word_tsv, detail_json,
                    alnum_count, word_count, bad_char_count,
                    mean_word_confidence, low_confidence_share,
                    confidence_word_count, error, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [sha, page, version, triage_version, uuid.uuid4().hex,
                  "failed" if error else "complete", text, word_tsv, details,
                  alnum, words, bad,
                  mean, low_share, confidence_words, error, now_utc()])

    def ocr(self, limit: int | None = None, only_sha: str | None = None) -> Counter[str]:
        version = ocr_version()
        previous_version = version.replace(OCR_RULE + "|", PREVIOUS_OCR_RULE + "|", 1)
        self.set_active_version("ocr", version)
        stats: Counter[str] = Counter()
        run_id = self.start_run("ocr", version)
        try:
            queue = self.conn.execute("""
                SELECT q.pdf_sha256, q.page_number, q.raw_path, q.triage_version,
                       d.byte_count, d.source_mtime_ns, d.page_count
                FROM ocr_queue q JOIN documents d USING (pdf_sha256)
                ORDER BY q.pdf_sha256, q.page_number
            """).fetchall()
            for sha, group in groupby(queue, key=lambda row: row[0]):
                if only_sha and sha != only_sha:
                    continue
                pages = list(group)
                complete = {
                    page for page, in self.conn.execute("""
                        SELECT page_number FROM ocr_pages
                        WHERE pdf_sha256=? AND ocr_version=? AND status='complete'
                    """, [sha, version]).fetchall()
                }
                pending = [row for row in pages if row[1] not in complete]
                stats["skipped_pages"] += len(pages) - len(pending)
                if not pending:
                    continue
                if limit is not None and stats["processed_documents"] >= limit:
                    break
                stats["processed_documents"] += 1
                first = pending[0]
                source = (sha, first[2], first[4], first[5], first[6])
                if not self.current_source(source):
                    for row in pending:
                        self.record_ocr(sha, row[1], version, row[3], None, None,
                                        None, "source changed since inventory")
                        stats["failed"] += 1
                    continue
                with tempfile.TemporaryDirectory(prefix="court-ocr-") as folder:
                    for row in pending:
                        page_number, triage_version = row[1], row[3]
                        text = details = error = word_tsv = None
                        scores = None
                        try:
                            cached_tsv = self.conn.execute("""
                                SELECT word_tsv FROM ocr_pages
                                WHERE pdf_sha256=? AND page_number=? AND ocr_version=?
                                  AND triage_version=? AND status='complete'
                                  AND word_tsv IS NOT NULL
                            """, [sha, page_number, previous_version,
                                  triage_version]).fetchone()
                            if cached_tsv:
                                word_tsv = cached_tsv[0]
                                text, details, scores = tesseract_result(word_tsv)
                                stats["reparsed_pages"] += 1
                            else:
                                text, details, scores, word_tsv = ocr_page(
                                    self.pdf_path(first[2]), page_number, Path(folder)
                                )
                        except Exception as exc:
                            error = str(exc)
                            text = details = scores = None
                            stats["failed"] += 1
                            print(f"  OCR ERROR {sha[:12]} page {page_number}: {error}",
                                  file=sys.stderr)
                        self.record_ocr(sha, page_number, version, triage_version,
                                        text, details, scores, error, word_tsv)
                        stats["processed_pages"] += 1
                        if stats["processed_pages"] % 50 == 0:
                            print(f"  OCR processed {stats['processed_pages']} pages",
                                  flush=True)
        except BaseException:
            self.finish_run(run_id, stats, crashed=True)
            raise
        self.finish_run(run_id, stats)
        print(f"OCR: {dict(stats)}", flush=True)
        return stats

    def selection_version(self) -> str:
        ocr_version = self.active_version("ocr")
        if not ocr_version:
            raise RuntimeError("Run OCR before selecting page text")
        return (f"{SELECTION_RULE}|native={self.native_version}|"
                f"triage={self.triage_version}|ocr={ocr_version}")

    def selection_input(self, sha: str, ocr_version: str) -> list[tuple]:
        return self.conn.execute("""
            SELECT t.page_number, t.decision, t.reason, n.text,
                   n.alnum_count, n.bad_char_count, o.result_id, o.status,
                   o.text, o.alnum_count, o.word_count, o.bad_char_count,
                   o.mean_word_confidence
            FROM current_page_triage t
            JOIN current_native_pages n USING (pdf_sha256, page_number)
            LEFT JOIN ocr_pages o ON o.pdf_sha256=t.pdf_sha256
                AND o.page_number=t.page_number AND o.ocr_version=?
            WHERE t.pdf_sha256=? ORDER BY t.page_number
        """, [ocr_version, sha]).fetchall()

    def select(self, limit: int | None = None, only_sha: str | None = None) -> Counter[str]:
        version = self.selection_version()
        ocr_version = self.active_version("ocr")
        assert ocr_version is not None
        self.set_active_version("selection", version)
        stats: Counter[str] = Counter()
        run_id = self.start_run("selection", version)
        try:
            for sha, _path, _size, _mtime, pages in self.ready_documents():
                if only_sha and sha != only_sha:
                    continue
                rows = self.selection_input(sha, ocr_version)
                if len(rows) != pages:
                    stats["failed"] += 1
                    print(f"  selection ERROR {sha[:12]}: {len(rows)}/{pages} inputs",
                          file=sys.stderr)
                    continue
                signature = hashlib.sha256(json.dumps([
                    (row[0], row[1], row[2], row[6], row[7]) for row in rows
                ]).encode()).hexdigest()
                prior = self.conn.execute("""
                    SELECT input_signature, page_count FROM selection_documents
                    WHERE pdf_sha256=? AND selection_version=?
                """, [sha, version]).fetchone()
                selected_count = self.conn.execute("""
                    SELECT count(*) FROM selected_pages
                    WHERE pdf_sha256=? AND selection_version=?
                """, [sha, version]).fetchone()[0]
                if prior == (signature, pages) and selected_count == pages:
                    stats["skipped_documents"] += 1
                    continue
                if limit is not None and stats["processed_documents"] >= limit:
                    break
                selected = []
                for row in rows:
                    (page, decision, _triage_reason, native_text, native_alnum,
                     native_bad, ocr_id, ocr_status, ocr_text, ocr_alnum,
                     ocr_words, ocr_bad, ocr_conf) = row
                    status, source, reason = choose_page(
                        decision, native_text, native_alnum, native_bad,
                        ocr_status, ocr_text, ocr_alnum, ocr_words, ocr_bad,
                        ocr_conf,
                    )
                    selected.append((sha, page, version, self.native_version,
                                     self.triage_version, ocr_version, ocr_id,
                                     status, source, reason, now_utc()))
                    stats[status] += 1
                    if source:
                        stats[f"source_{source}"] += 1
                with transaction(self.conn):
                    self.conn.execute("""
                        DELETE FROM selected_pages
                        WHERE pdf_sha256=? AND selection_version=?
                    """, [sha, version])
                    self.conn.execute("""
                        DELETE FROM selection_documents
                        WHERE pdf_sha256=? AND selection_version=?
                    """, [sha, version])
                    self.conn.executemany("""
                        INSERT INTO selected_pages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, selected)
                    self.conn.execute("""
                        INSERT INTO selection_documents VALUES (?, ?, ?, ?, ?, ?)
                    """, [sha, version, signature, uuid.uuid4().hex, pages,
                          now_utc()])
                stats["processed_documents"] += 1
                if stats["processed_documents"] % 250 == 0:
                    print(f"  selected {stats['processed_documents']} PDFs", flush=True)
        except BaseException:
            self.finish_run(run_id, stats, crashed=True)
            raise
        self.finish_run(run_id, stats)
        print(f"Selection: {dict(stats)}", flush=True)
        return stats

    def audit(self, limit: int | None = None, only_sha: str | None = None) -> Counter[str]:
        selection_version = self.active_version("selection")
        if not selection_version:
            raise RuntimeError("Run selection before the text audit")
        version = f"{AUDIT_RULE}|selection={selection_version}"
        self.set_active_version("audit", version)
        stats: Counter[str] = Counter()
        run_id = self.start_run("audit", version)
        try:
            for sha, _path, _size, _mtime, pages in self.ready_documents():
                if only_sha and sha != only_sha:
                    continue
                selection_doc = self.conn.execute("""
                    SELECT result_id FROM selection_documents
                    WHERE pdf_sha256=? AND selection_version=?
                """, [sha, selection_version]).fetchone()
                result_id = selection_doc[0] if selection_doc else None
                prior = self.conn.execute("""
                    SELECT selection_result_id, expected_pages FROM audit_documents
                    WHERE pdf_sha256=? AND audit_version=?
                """, [sha, version]).fetchone()
                if prior == (result_id, pages) and result_id is not None:
                    stats["skipped_documents"] += 1
                    continue
                if limit is not None and stats["processed_documents"] >= limit:
                    break
                rows = self.conn.execute("""
                    SELECT p.page_number, s.status, s.source, s.reason,
                           o.status, o.mean_word_confidence, t.decision,
                           n.text, o.text
                    FROM document_pages p
                    LEFT JOIN selected_pages s ON s.pdf_sha256=p.pdf_sha256
                        AND s.page_number=p.page_number AND s.selection_version=?
                    LEFT JOIN page_triage t ON t.pdf_sha256=p.pdf_sha256
                        AND t.page_number=p.page_number AND t.triage_version=?
                    LEFT JOIN native_pages n ON n.pdf_sha256=p.pdf_sha256
                        AND n.page_number=p.page_number
                        AND n.extractor_version=s.native_version
                    LEFT JOIN ocr_pages o ON o.pdf_sha256=s.pdf_sha256
                        AND o.page_number=s.page_number AND o.ocr_version=s.ocr_version
                    WHERE p.pdf_sha256=? ORDER BY p.page_number
                """, [selection_version, self.triage_version, sha]).fetchall()
                flags = []
                selected_count = unresolved = 0
                for (page, status, source, _reason, ocr_status, ocr_conf,
                     decision, native_text, ocr_text) in rows:
                    page_flags = []
                    if status is None:
                        page_flags.append("selection_missing")
                    else:
                        selected_count += 1
                        if decision is None:
                            page_flags.append("triage_missing")
                        if status == "unresolved":
                            unresolved += 1
                            page_flags.append("unresolved_text")
                        if decision in {"ocr_needed", "ocr_candidate"} and ocr_status != "complete":
                            page_flags.append("ocr_missing_or_failed")
                        if source == "ocr" and ocr_status != "complete":
                            page_flags.append("selected_ocr_missing")
                        if source == "ocr" and ocr_conf is not None and ocr_conf < 60:
                            page_flags.append("ocr_low_mean_confidence")
                        if status == "usable" and not (
                            (source == "native" and native_text and native_text.strip())
                            or (source == "ocr" and ocr_text and ocr_text.strip())
                        ):
                            page_flags.append("selected_text_missing")
                    if page_flags:
                        flags.append((sha, page, version, json.dumps(page_flags)))
                        stats.update(page_flags)
                total_selected = self.conn.execute("""
                    SELECT count(*) FROM selected_pages
                    WHERE pdf_sha256=? AND selection_version=?
                """, [sha, selection_version]).fetchone()[0]
                if total_selected != selected_count:
                    stats["orphan_selection_rows"] += total_selected - selected_count
                structural_problem = any(
                    any(flag in flag_row[3] for flag in (
                        "selection_missing", "triage_missing",
                        "selected_ocr_missing", "selected_text_missing",
                    )) for flag_row in flags
                )
                audit_status = (
                    "incomplete" if (selected_count != pages or total_selected != pages
                                     or structural_problem) else
                    "uncertain" if flags else "ready"
                )
                with transaction(self.conn):
                    self.conn.execute("""
                        DELETE FROM audit_page_flags WHERE pdf_sha256=? AND audit_version=?
                    """, [sha, version])
                    self.conn.execute("""
                        DELETE FROM audit_documents WHERE pdf_sha256=? AND audit_version=?
                    """, [sha, version])
                    if flags:
                        self.conn.executemany("""
                            INSERT INTO audit_page_flags VALUES (?, ?, ?, ?)
                        """, flags)
                    self.conn.execute("""
                        INSERT INTO audit_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, [sha, version, result_id, pages, selected_count,
                          unresolved, len(flags), audit_status, now_utc()])
                stats["processed_documents"] += 1
                stats[f"documents_{audit_status}"] += 1
                if audit_status == "incomplete":
                    stats["failed"] += 1
                if stats["processed_documents"] % 250 == 0:
                    print(f"  audited {stats['processed_documents']} PDFs", flush=True)
        except BaseException:
            self.finish_run(run_id, stats, crashed=True)
            raise
        self.finish_run(run_id, stats)
        print(f"Audit: {dict(stats)}", flush=True)
        return stats

    def status(self) -> None:
        super().status()
        for label, query in (
            ("OCR results", "SELECT status, count(*) FROM current_ocr_pages GROUP BY status ORDER BY status"),
            ("Selected pages", "SELECT status, source, count(*) FROM current_selected_pages GROUP BY status, source ORDER BY status, source"),
            ("Unresolved reasons", "SELECT reason, count(*) FROM current_selected_pages WHERE status='unresolved' GROUP BY reason ORDER BY reason"),
            ("Audited documents", "SELECT status, count(*) FROM current_audit_documents GROUP BY status ORDER BY status"),
            ("Audit page flags", "SELECT flags_json, count(*) FROM audit_page_flags WHERE audit_version=(SELECT version FROM current_versions WHERE stage='audit') GROUP BY flags_json ORDER BY flags_json"),
        ):
            print(f"{label}: {self.conn.execute(query).fetchall()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["ocr", "select", "audit", "all", "status"])
    parser.add_argument("--limit", type=int, help="Maximum new PDFs per stage")
    parser.add_argument("--sha", help="Process only one PDF SHA-256")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.sha and not re.fullmatch(r"[0-9a-f]{64}", args.sha):
        parser.error("--sha must be a full lowercase SHA-256")
    if args.stage == "status" and (args.limit or args.sha):
        parser.error("status does not accept --limit or --sha")
    pipeline = TextFinishPipeline(existing_versions=True)
    try:
        if args.stage == "status":
            pipeline.status()
            return 0
        stages = ("ocr", "select", "audit") if args.stage == "all" else (args.stage,)
        failures = 0
        for stage in stages:
            stats = getattr(pipeline, stage)(args.limit, args.sha)
            failures += stats["failed"]
        pipeline.status()
        return 1 if failures else 0
    finally:
        pipeline.close()


if __name__ == "__main__":
    raise SystemExit(main())
