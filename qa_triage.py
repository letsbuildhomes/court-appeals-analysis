"""Exploratory, resumable QA of native PDF text; does not change production triage.

Reads data/text.duckdb and archived PDFs. Writes only data/triage_qa.duckdb.
This experiment is meant to inform revisions to the production text pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from config import ROOT, TEXT_DB_PATH
from text_finish import OCR_LONG_EDGE, ocr_page, ocr_version
from text_pipeline import command, transaction


QA_DB_PATH = ROOT / "data" / "triage_qa.duckdb"
QA_RULE = "native-qa-experiment-v1"
QA_OCR_RULE = "tesseract-page-v1"  # Original QA recognition setup; TSV is retained.
SAMPLE_SEED = "native-qa-2026-09-24-v1"
RANDOM_PAGES = 500
RISK_PAGES = 500
INK_SCALE = 512
INK_LUT = bytes(1 if value < 245 else 0 for value in range(256))
TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text.casefold())


def tsv_words(tsv: str):
    """Read Tesseract's raw TSV; its text field is not CSV-quote escaped."""
    for line in tsv.splitlines()[1:]:
        fields = line.split("\t", 11)
        if len(fields) != 12 or fields[0] != "5":
            continue
        try:
            confidence = float(fields[10])
            top, height = float(fields[7]), float(fields[9])
        except ValueError:
            continue
        if not 0 <= confidence <= 100:
            continue
        yield confidence, top, height, tuple(fields[1:5]), fields[11].strip()


def tsv_text(tsv: str) -> str:
    lines: dict[tuple[str, ...], list[str]] = {}
    for _confidence, _top, _height, line_key, word in tsv_words(tsv):
        if word:
            lines.setdefault(line_key, []).append(word)
    return "\n".join(" ".join(words) for words in lines.values())


def multiset_recall(reference: str, candidate: str) -> float | None:
    wanted = Counter(tokens(reference))
    if not wanted:
        return None
    available = Counter(tokens(candidate))
    return sum(min(count, available[word]) for word, count in wanted.items()) / sum(wanted.values())


def parse_image_list(output: str) -> dict[int, list[tuple[int, int, float, float]]]:
    result: dict[int, list[tuple[int, int, float, float]]] = {}
    for line in output.splitlines()[2:]:
        fields = line.split()
        if len(fields) < 14 or not fields[0].isdigit() or fields[2] != "image":
            continue
        try:
            page = int(fields[0])
            width, height = int(fields[3]), int(fields[4])
            xppi, yppi = float(fields[12]), float(fields[13])
        except ValueError:
            continue
        if min(width, height, xppi, yppi) > 0:
            result.setdefault(page, []).append((width, height, xppi, yppi))
    return result


def parse_bbox(output: bytes) -> list[tuple[float, float, list[tuple[str, float]]]]:
    """Return page dimensions and each word's vertical center in page units."""
    # Poppler can emit PDF control characters that XML 1.0 forbids.
    cleaned = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", output)
    root = ET.fromstring(cleaned)
    pages = []
    for page in root.iter():
        if page.tag.rsplit("}", 1)[-1] != "page":
            continue
        width, height = float(page.attrib["width"]), float(page.attrib["height"])
        words = []
        for element in page:
            if element.tag.rsplit("}", 1)[-1] != "word":
                continue
            word = (element.text or "").strip()
            if word:
                y = (float(element.attrib["yMin"]) + float(element.attrib["yMax"])) / 2
                words.append((word, y / height))
        pages.append((width, height, words))
    return pages


def pgm_ink_bands(data: bytes) -> tuple[float, list[float]]:
    """Compute dark-pixel share overall and in five horizontal page bands."""
    pos = 0

    def field() -> bytes:
        nonlocal pos
        while pos < len(data):
            if data[pos] in b" \t\r\n":
                pos += 1
            elif data[pos] == ord("#"):
                end = data.find(b"\n", pos)
                pos = len(data) if end < 0 else end + 1
            else:
                break
        start = pos
        while pos < len(data) and data[pos] not in b" \t\r\n":
            pos += 1
        return data[start:pos]

    magic, width, height, maximum = field(), int(field()), int(field()), int(field())
    if magic != b"P5" or maximum != 255 or width < 1 or height < 1:
        raise ValueError("Unexpected Poppler PGM format")
    if pos >= len(data) or data[pos] not in b" \t\r\n":
        raise ValueError("Missing PGM header terminator")
    pos += 2 if data[pos:pos + 2] == b"\r\n" else 1
    pixels = data[pos:]
    if len(pixels) != width * height:
        raise ValueError("Unexpected PGM pixel count")
    dark = pixels.translate(INK_LUT)
    bands = []
    for index in range(5):
        start, end = height * index // 5, height * (index + 1) // 5
        bands.append(dark[start * width:end * width].count(1) / ((end - start) * width))
    return dark.count(1) / len(dark), bands


def render_ink_bands(path: Path, page: int, folder: Path) -> tuple[float, list[float]]:
    prefix = folder / f"ink_{page:05d}"
    command([
        "pdftoppm", "-f", str(page), "-l", str(page), "-scale-to",
        str(INK_SCALE), "-gray", "-singlefile", str(path), str(prefix),
    ])
    image = prefix.with_suffix(".pgm")
    try:
        return pgm_ink_bands(image.read_bytes())
    finally:
        image.unlink(missing_ok=True)


def risk_flags(alnum: int, bad: int, max_image_area: float,
               bbox_words: int, body_words: int, bbox_recall: float | None,
               body_ink: float | None, relative_alnum: float) -> tuple[list[str], int]:
    flags = []
    score = 0
    if max_image_area >= 0.5:
        flags.append("page_sized_image")
        score += 5
    elif max_image_area >= 0.2:
        flags.append("substantial_image")
        score += 3
    if body_ink is not None and body_ink >= 0.01 and body_words < 5 and bbox_words >= 5:
        flags.append("body_ink_without_native_words")
        score += 4
    if bbox_recall is not None and bbox_words >= 20 and bbox_recall < 0.9:
        flags.append("poppler_output_disagreement")
        score += 2
    if alnum < 500:
        flags.append("short_native_text")
        score += 1
    if relative_alnum < 0.25 and body_ink is not None and body_ink >= 0.01:
        flags.append("sparse_relative_to_document")
        score += 1
    if bad:
        flags.append("suspicious_characters")
        score += 1
    return flags, score


def compare_ocr(tsv: str, native_text: str, page_width: float,
                page_height: float, max_image_area: float,
                body_ink_without_words: bool) -> dict:
    """Compare confident OCR words to native text; disagreement is not ground truth."""
    available = Counter(tokens(native_text))
    all_words = []
    high_conf = missing = missing_body = longest = run = 0
    confidence_sum = 0.0
    render_height = OCR_LONG_EDGE * page_height / max(page_width, page_height)
    for confidence, top, height, _line_key, raw_word in tsv_words(tsv):
        y = (top + height / 2) / render_height
        confidence_sum += confidence
        all_words.append(confidence)
        for word in tokens(raw_word):
            if confidence < 80:
                run = 0
                continue
            high_conf += 1
            if available[word] > 0:
                available[word] -= 1
                run = 0
            else:
                missing += 1
                run += 1
                longest = max(longest, run)
                if 0.2 <= y <= 0.8:
                    missing_body += 1
    fraction = missing / high_conf if high_conf else None
    if high_conf < 10:
        category = "ocr_inconclusive"
    elif fraction is not None and fraction <= 0.1 and longest < 5:
        category = "ocr_agrees"
    elif (high_conf >= 20 and fraction is not None and fraction >= 0.2
          and longest >= 8 and (missing_body >= 8 or max_image_area >= 0.2
                                or body_ink_without_words)):
        category = "suspected_native_omission"
    else:
        category = "ocr_disagreement"
    return {
        "category": category,
        "high_conf_words": high_conf,
        "missing_words": missing,
        "missing_fraction": fraction,
        "longest_missing_run": longest,
        "missing_body_words": missing_body,
        "mean_confidence": confidence_sum / len(all_words) if all_words else None,
    }


class QAPipeline:
    def __init__(self) -> None:
        if not TEXT_DB_PATH.is_file():
            raise FileNotFoundError(TEXT_DB_PATH)
        self.source = duckdb.connect(str(TEXT_DB_PATH), read_only=True)
        versions = dict(self.source.execute("SELECT stage,version FROM current_versions").fetchall())
        ocr_tools = ocr_version().split("|", 1)[1]
        self.version = (f"{QA_RULE}|native={versions['native']}|"
                        f"triage={versions['triage']}|ocr={QA_OCR_RULE}|"
                        f"{ocr_tools}|ink={INK_SCALE}")
        QA_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(QA_DB_PATH))
        self.create_schema()

    def close(self) -> None:
        self.conn.close()
        self.source.close()

    def create_schema(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS screen_documents (
                pdf_sha256 VARCHAR, qa_version VARCHAR, page_count INTEGER,
                status VARCHAR, error VARCHAR, processed_at VARCHAR,
                PRIMARY KEY (pdf_sha256,qa_version))
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS qa_pages (
                pdf_sha256 VARCHAR, page_number INTEGER, qa_version VARCHAR,
                triage_decision VARCHAR, alnum_count INTEGER, word_count INTEGER,
                bad_char_count INTEGER, page_width DOUBLE, page_height DOUBLE,
                image_count INTEGER, max_image_area DOUBLE, bbox_word_count INTEGER,
                bbox_body_words INTEGER, bbox_native_recall DOUBLE,
                ink_ratio DOUBLE, body_ink_ratio DOUBLE, ink_bands_json VARCHAR,
                relative_alnum DOUBLE, flags_json VARCHAR, risk_score INTEGER,
                category VARCHAR, error VARCHAR, processed_at VARCHAR,
                PRIMARY KEY (pdf_sha256,page_number,qa_version))
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS qa_sample (
                pdf_sha256 VARCHAR, page_number INTEGER, qa_version VARCHAR,
                sample_group VARCHAR, sample_rank INTEGER,
                PRIMARY KEY (pdf_sha256,page_number,qa_version))
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS qa_ocr (
                pdf_sha256 VARCHAR, page_number INTEGER, qa_version VARCHAR,
                sample_group VARCHAR, status VARCHAR, category VARCHAR,
                text VARCHAR, word_tsv VARCHAR, high_conf_words INTEGER,
                missing_words INTEGER, missing_fraction DOUBLE,
                longest_missing_run INTEGER, missing_body_words INTEGER,
                mean_confidence DOUBLE, error VARCHAR, processed_at VARCHAR,
                PRIMARY KEY (pdf_sha256,page_number,qa_version))
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS qa_runs (
                run_id VARCHAR PRIMARY KEY, stage VARCHAR, qa_version VARCHAR,
                started_at VARCHAR, finished_at VARCHAR, status VARCHAR,
                stats_json VARCHAR)
        """)

    def start_run(self, stage: str) -> str:
        run_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO qa_runs VALUES (?,?,?,?,NULL,'running',NULL)",
            [run_id, stage, self.version, now_utc()],
        )
        return run_id

    def finish_run(self, run_id: str, stats: Counter, crashed: bool = False) -> None:
        status = "crashed" if crashed else "complete_with_errors" if stats["failed"] else "complete"
        self.conn.execute(
            "UPDATE qa_runs SET finished_at=?,status=?,stats_json=? WHERE run_id=?",
            [now_utc(), status, json.dumps(dict(stats), sort_keys=True), run_id],
        )

    def screen(self, limit: int | None = None) -> Counter:
        stats: Counter = Counter()
        run_id = self.start_run("screen")
        try:
            docs = self.source.execute("""
                SELECT pdf_sha256,raw_path,page_count FROM documents
                WHERE status='ready' ORDER BY pdf_sha256
            """).fetchall()
            for sha, raw_path, pages in docs:
                prior = self.conn.execute("""
                    SELECT status,page_count FROM screen_documents
                    WHERE pdf_sha256=? AND qa_version=?
                """, [sha, self.version]).fetchone()
                count = self.conn.execute("""
                    SELECT count(*) FROM qa_pages WHERE pdf_sha256=? AND qa_version=?
                """, [sha, self.version]).fetchone()[0]
                if prior == ("complete", pages) and count == pages:
                    stats["skipped_documents"] += 1
                    continue
                if limit is not None and stats["processed_documents"] >= limit:
                    break
                path = (ROOT / raw_path).resolve()
                try:
                    if not path.is_relative_to((ROOT / "in" / "pdfs").resolve()):
                        raise ValueError("Source path outside PDF archive")
                    images = parse_image_list(command(["pdfimages", "-list", str(path)]).stdout.decode(
                        "utf-8", errors="replace"
                    ))
                    bbox = parse_bbox(command([
                        "pdftotext", "-bbox", "-enc", "UTF-8", str(path), "-"
                    ]).stdout)
                    if len(bbox) != pages:
                        raise ValueError(f"Poppler bbox returned {len(bbox)} of {pages} pages")
                    native = self.source.execute("""
                        SELECT n.page_number,n.text,n.alnum_count,n.word_count,
                               n.bad_char_count,t.decision
                        FROM current_native_pages n JOIN current_page_triage t
                        USING (pdf_sha256,page_number)
                        WHERE n.pdf_sha256=? ORDER BY n.page_number
                    """, [sha]).fetchall()
                    if len(native) != pages:
                        raise ValueError(f"Native/triage rows {len(native)} of {pages}")
                    median_alnum = statistics.median(
                        row[2] for row in native if row[5] == "native_ok"
                    ) if any(row[5] == "native_ok" for row in native) else 0
                    page_rows = []
                    with tempfile.TemporaryDirectory(prefix="court-qa-screen-") as folder:
                        for number, native_text, alnum, word_count, bad, decision in native:
                            width, height, bbox_words = bbox[number - 1]
                            placed_images = images.get(number, [])
                            max_image = max((
                                min(1.0, (px_width / xppi * 72) * (px_height / yppi * 72)
                                    / (width * height))
                                for px_width, px_height, xppi, yppi in placed_images
                            ), default=0.0)
                            bbox_text = " ".join(word for word, _ in bbox_words)
                            body_words = sum(0.2 <= y <= 0.8 for _, y in bbox_words)
                            recall = multiset_recall(bbox_text, native_text)
                            ink = body_ink = bands_json = error = None
                            if decision == "native_ok":
                                try:
                                    ink, bands = render_ink_bands(path, number, Path(folder))
                                    body_ink = sum(bands[1:4]) / 3
                                    bands_json = json.dumps(bands)
                                except (OSError, RuntimeError, ValueError) as exc:
                                    error = str(exc)
                                    stats["page_errors"] += 1
                            relative = alnum / median_alnum if median_alnum else 1.0
                            flags, score = risk_flags(
                                alnum, bad, max_image, len(bbox_words), body_words,
                                recall, body_ink, relative,
                            ) if decision == "native_ok" and not error else ([], 0)
                            category = ("qa_error" if error else "not_native_path"
                                        if decision != "native_ok" else
                                        "risk_flagged" if flags else "screened_no_signal")
                            page_rows.append((
                                sha, number, self.version, decision, alnum, word_count,
                                bad, width, height, len(placed_images), max_image,
                                len(bbox_words), body_words, recall, ink, body_ink,
                                bands_json, relative, json.dumps(flags), score,
                                category, error, now_utc(),
                            ))
                            stats[category] += 1
                            stats.update(flags)
                    with transaction(self.conn):
                        self.conn.execute(
                            "DELETE FROM qa_pages WHERE pdf_sha256=? AND qa_version=?",
                            [sha, self.version],
                        )
                        self.conn.execute(
                            "DELETE FROM screen_documents WHERE pdf_sha256=? AND qa_version=?",
                            [sha, self.version],
                        )
                        self.conn.executemany(
                            "INSERT INTO qa_pages VALUES (" + ",".join("?" for _ in range(23)) + ")",
                            page_rows,
                        )
                        self.conn.execute(
                            "INSERT INTO screen_documents VALUES (?,?,?,'complete',NULL,?)",
                            [sha, self.version, pages, now_utc()],
                        )
                    stats["processed_documents"] += 1
                    if stats["processed_documents"] % 250 == 0:
                        print(f"  QA screened {stats['processed_documents']} PDFs", flush=True)
                except (OSError, RuntimeError, ValueError, ET.ParseError) as exc:
                    stats["failed"] += 1
                    with transaction(self.conn):
                        self.conn.execute(
                            "DELETE FROM screen_documents WHERE pdf_sha256=? AND qa_version=?",
                            [sha, self.version],
                        )
                        self.conn.execute(
                            "INSERT INTO screen_documents VALUES (?,?,?,'failed',?,?)",
                            [sha, self.version, pages, str(exc), now_utc()],
                        )
                    print(f"  QA screen ERROR {sha[:12]}: {exc}", file=sys.stderr)
        except BaseException:
            self.finish_run(run_id, stats, crashed=True)
            raise
        self.finish_run(run_id, stats)
        print(f"QA screen: {dict(stats)}", flush=True)
        return stats

    def plan_sample(self) -> None:
        existing = self.conn.execute(
            "SELECT count(*) FROM qa_sample WHERE qa_version=?", [self.version]
        ).fetchone()[0]
        if existing:
            return
        ready_docs = self.source.execute(
            "SELECT count(*) FROM documents WHERE status='ready'"
        ).fetchone()[0]
        screened_docs = self.conn.execute("""
            SELECT count(*) FROM screen_documents
            WHERE qa_version=? AND status='complete'
        """, [self.version]).fetchone()[0]
        if screened_docs != ready_docs:
            raise RuntimeError(f"QA screen incomplete: {screened_docs}/{ready_docs} PDFs")
        rows = self.conn.execute("""
            SELECT pdf_sha256,page_number,risk_score FROM qa_pages
            WHERE qa_version=? AND triage_decision='native_ok' AND category!='qa_error'
        """, [self.version]).fetchall()
        random_rows = sorted(rows, key=lambda row: hashlib.sha256(
            f"{SAMPLE_SEED}|{row[0]}|{row[1]}".encode()
        ).hexdigest())[:RANDOM_PAGES]
        picked = {(sha, page) for sha, page, _ in random_rows}
        risk_rows = sorted(
            (row for row in rows if row[2] > 0 and (row[0], row[1]) not in picked),
            key=lambda row: (-row[2], row[0], row[1]),
        )[:RISK_PAGES]
        sample = [(sha, page, self.version, "random", index)
                  for index, (sha, page, _) in enumerate(random_rows, 1)]
        sample += [(sha, page, self.version, "risk", index)
                   for index, (sha, page, _) in enumerate(risk_rows, 1)]
        with transaction(self.conn):
            self.conn.executemany("INSERT INTO qa_sample VALUES (?,?,?,?,?)", sample)
        print(f"QA sample: {len(random_rows)} random + {len(risk_rows)} risk pages", flush=True)

    def compare(self, limit: int | None = None) -> Counter:
        self.plan_sample()
        stats: Counter = Counter()
        run_id = self.start_run("compare")
        try:
            rows = self.conn.execute("""
                SELECT s.pdf_sha256,s.page_number,s.sample_group,p.page_width,
                       p.page_height,p.max_image_area,p.flags_json,d.raw_path,
                       n.text
                FROM qa_sample s JOIN qa_pages p USING(pdf_sha256,page_number,qa_version)
                JOIN read_text_documents d USING(pdf_sha256)
                JOIN read_text_native n USING(pdf_sha256,page_number)
                WHERE s.qa_version=?
                ORDER BY s.pdf_sha256,s.page_number
            """, [self.version]).fetchall()
            with tempfile.TemporaryDirectory(prefix="court-qa-ocr-") as folder:
                for (sha, page, group, width, height, max_image, flags_json,
                     raw_path, native_text) in rows:
                    prior = self.conn.execute("""
                        SELECT status FROM qa_ocr WHERE pdf_sha256=? AND page_number=?
                        AND qa_version=?
                    """, [sha, page, self.version]).fetchone()
                    if prior == ("complete",):
                        stats["skipped_pages"] += 1
                        continue
                    if limit is not None and stats["processed_pages"] >= limit:
                        break
                    try:
                        _text, _details, _scores, tsv = ocr_page(
                            ROOT / raw_path, page, Path(folder)
                        )
                        text = tsv_text(tsv)
                        result = compare_ocr(
                            tsv, native_text, width, height, max_image,
                            "body_ink_without_native_words" in json.loads(flags_json),
                        )
                        error = None
                        stats[result["category"]] += 1
                    except (OSError, RuntimeError, ValueError) as exc:
                        text = tsv = None
                        result = dict.fromkeys((
                            "high_conf_words", "missing_words", "missing_fraction",
                            "longest_missing_run", "missing_body_words", "mean_confidence",
                        ))
                        result["category"] = "qa_error"
                        error = str(exc)
                        stats["failed"] += 1
                        print(f"  QA OCR ERROR {sha[:12]} page {page}: {exc}", file=sys.stderr)
                    with transaction(self.conn):
                        self.conn.execute("""
                            DELETE FROM qa_ocr WHERE pdf_sha256=? AND page_number=?
                            AND qa_version=?
                        """, [sha, page, self.version])
                        self.conn.execute("""
                            INSERT INTO qa_ocr VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, [sha, page, self.version, group,
                              "failed" if error else "complete", result["category"],
                              text, tsv, result["high_conf_words"],
                              result["missing_words"], result["missing_fraction"],
                              result["longest_missing_run"], result["missing_body_words"],
                              result["mean_confidence"], error, now_utc()])
                    stats["processed_pages"] += 1
                    if stats["processed_pages"] % 50 == 0:
                        print(f"  QA OCR compared {stats['processed_pages']} pages", flush=True)
        except BaseException:
            self.finish_run(run_id, stats, crashed=True)
            raise
        self.finish_run(run_id, stats)
        print(f"QA compare: {dict(stats)}", flush=True)
        return stats

    def recompare(self, limit: int | None = None) -> Counter:
        """Reclassify saved OCR TSV without rerendering or rerunning Tesseract."""
        stats: Counter = Counter()
        run_id = self.start_run("recompare")
        try:
            rows = self.conn.execute("""
                SELECT o.pdf_sha256,o.page_number,o.word_tsv,p.page_width,
                       p.page_height,p.max_image_area,p.flags_json,n.text
                FROM qa_ocr o JOIN qa_pages p USING(pdf_sha256,page_number,qa_version)
                JOIN read_text_native n USING(pdf_sha256,page_number)
                WHERE o.qa_version=? AND o.status='complete'
                ORDER BY o.pdf_sha256,o.page_number
            """, [self.version]).fetchall()
            for sha, page, tsv, width, height, max_image, flags_json, native_text in rows:
                if limit is not None and stats["processed_pages"] >= limit:
                    break
                result = compare_ocr(
                    tsv, native_text, width, height, max_image,
                    "body_ink_without_native_words" in json.loads(flags_json),
                )
                self.conn.execute("""
                    UPDATE qa_ocr SET text=?,category=?,high_conf_words=?,
                        missing_words=?,missing_fraction=?,longest_missing_run=?,
                        missing_body_words=?,mean_confidence=?,processed_at=?
                    WHERE pdf_sha256=? AND page_number=? AND qa_version=?
                """, [
                    tsv_text(tsv), result["category"], result["high_conf_words"],
                    result["missing_words"], result["missing_fraction"],
                    result["longest_missing_run"], result["missing_body_words"],
                    result["mean_confidence"], now_utc(), sha, page, self.version,
                ])
                stats["processed_pages"] += 1
                stats[result["category"]] += 1
        except BaseException:
            self.finish_run(run_id, stats, crashed=True)
            raise
        self.finish_run(run_id, stats)
        print(f"QA recompare: {dict(stats)}", flush=True)
        return stats

    def attach_source_views(self) -> None:
        # Read-only source connection remains separate; attach solely for joins.
        quoted_path = str(TEXT_DB_PATH).replace("'", "''")
        self.conn.execute(f"ATTACH '{quoted_path}' AS text_source (READ_ONLY)")
        self.conn.execute("""
            CREATE OR REPLACE TEMP VIEW read_text_documents AS
            SELECT pdf_sha256,raw_path FROM text_source.documents
        """)
        self.conn.execute("""
            CREATE OR REPLACE TEMP VIEW read_text_native AS
            SELECT pdf_sha256,page_number,text FROM text_source.current_native_pages
        """)

    def status(self) -> None:
        for label, query in (
            ("Screen documents", "SELECT status,count(*) FROM screen_documents WHERE qa_version=? GROUP BY 1 ORDER BY 1"),
            ("Screen page categories", "SELECT category,count(*) FROM qa_pages WHERE qa_version=? GROUP BY 1 ORDER BY 1"),
            ("Sample", "SELECT sample_group,count(*) FROM qa_sample WHERE qa_version=? GROUP BY 1 ORDER BY 1"),
            ("OCR comparison", "SELECT sample_group,category,count(*) FROM qa_ocr WHERE qa_version=? GROUP BY 1,2 ORDER BY 1,2"),
            ("QA runs", "SELECT stage,status,stats_json FROM qa_runs WHERE qa_version=? ORDER BY started_at"),
        ):
            print(f"{label}: {self.conn.execute(query, [self.version]).fetchall()}")
        flags = Counter(
            flag for (encoded,) in self.conn.execute("""
                SELECT flags_json FROM qa_pages
                WHERE qa_version=? AND triage_decision='native_ok'
            """, [self.version]).fetchall()
            for flag in json.loads(encoded)
        )
        print(f"Screen flags (pages may have several): {sorted(flags.items())}")
        print("Sample by screen category:", self.conn.execute("""
            SELECT s.sample_group,p.category,count(*)
            FROM qa_sample s JOIN qa_pages p USING(pdf_sha256,page_number,qa_version)
            WHERE s.qa_version=? GROUP BY 1,2 ORDER BY 1,2
        """, [self.version]).fetchall())
        print("OCR by screen category:", self.conn.execute("""
            SELECT o.sample_group,p.category,o.category,count(*)
            FROM qa_ocr o JOIN qa_pages p USING(pdf_sha256,page_number,qa_version)
            WHERE o.qa_version=? GROUP BY 1,2,3 ORDER BY 1,2,3
        """, [self.version]).fetchall())
        print("Raster image inventory (occurrences, pages, PDFs):", self.conn.execute("""
            SELECT sum(image_count),count(*) FILTER (WHERE image_count>0),
                   count(DISTINCT pdf_sha256) FILTER (WHERE image_count>0)
            FROM qa_pages WHERE qa_version=?
        """, [self.version]).fetchone())
        print("Raster images by triage (decision, occurrences, pages):", self.conn.execute("""
            SELECT triage_decision,sum(image_count),
                   count(*) FILTER (WHERE image_count>0)
            FROM qa_pages WHERE qa_version=? GROUP BY 1 ORDER BY 1
        """, [self.version]).fetchall())
        print("Largest raster coverage (bucket, pages):", self.conn.execute("""
            SELECT CASE WHEN max_image_area>=0.5 THEN 'at least 50%'
                        WHEN max_image_area>=0.2 THEN '20% to 50%'
                        WHEN max_image_area>0 THEN 'under 20%'
                        ELSE 'no raster image' END,count(*)
            FROM qa_pages WHERE qa_version=? GROUP BY 1 ORDER BY 1
        """, [self.version]).fetchall())
        production_ocr = self.source.execute("""
            SELECT o.text,o.word_tsv,s.source
            FROM current_ocr_pages o
            LEFT JOIN current_selected_pages s USING(pdf_sha256,page_number)
            WHERE o.status='complete' AND o.word_tsv IS NOT NULL
        """).fetchall()
        differing = [(source, stored, tsv) for stored, tsv, source in production_ocr
                     if stored != tsv_text(tsv)]
        print("Production OCR TSV parse differences:", {
            "ocr_pages_checked": len(production_ocr),
            "different_text": len(differing),
            "currently_selected_ocr": sum(source == "ocr" for source, _, _ in differing),
        })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("screen", "compare", "recompare", "all", "status"))
    parser.add_argument("--limit", type=int, help="Maximum new PDFs for screen or pages for compare")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    pipeline = QAPipeline()
    try:
        pipeline.attach_source_views()
        if args.stage == "status":
            pipeline.status()
            return 0
        stages = ("screen", "compare") if args.stage == "all" else (args.stage,)
        failed = 0
        for stage in stages:
            failed += getattr(pipeline, stage)(args.limit)["failed"]
        pipeline.status()
        return 1 if failed else 0
    finally:
        pipeline.close()


if __name__ == "__main__":
    raise SystemExit(main())
