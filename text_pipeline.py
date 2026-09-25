"""Inventory archived PDFs, extract native page text, and queue pages for OCR.

The PDF archive and archive.duckdb are read-only inputs. Results and resumable
checkpoints live in data/text.duckdb. Later stages are in text_finish.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from config import DB_PATH, ROOT, TEXT_DB_PATH


INVENTORY_RULE = "inventory-v1"
NATIVE_RULE = "native-layout-v1"
TRIAGE_RULE = "triage-v3"
MIN_CLEAR_TEXT = 80
MIN_SHORT_TEXT = 20
BLANK_INK_RATIO = 0.0005
RENDER_SCALE = 512
MAX_COMMAND_SECONDS = 120


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(args: list[str], timeout: int = MAX_COMMAND_SECONDS) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not run {args[0]}: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-1200:]
        raise RuntimeError(f"{args[0]} exited {result.returncode}: {detail}")
    return result


def poppler_version() -> str:
    result = command(["pdfinfo", "-v"])
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    return output.splitlines()[0].strip()


def page_count(path: Path) -> int:
    output = command(["pdfinfo", str(path)]).stdout.decode("utf-8", errors="replace")
    match = re.search(r"^Pages:\s*(\d+)\s*$", output, re.MULTILINE)
    if not match or int(match.group(1)) < 1:
        raise ValueError(f"Could not find a positive page count in pdfinfo: {path}")
    return int(match.group(1))


def split_page_text(raw: bytes, expected_pages: int) -> list[str]:
    text = raw.decode("utf-8", errors="replace")
    parts = text.split("\f")
    if len(parts) == expected_pages + 1 and not parts[-1].strip():
        parts.pop()
    if len(parts) != expected_pages:
        raise ValueError(f"pdftotext returned {len(parts)} pages; expected {expected_pages}")
    return parts


def native_metrics(text: str) -> tuple[int, int, int]:
    alnum = sum(char.isalnum() for char in text)
    words = len(re.findall(r"\b\w+\b", text))
    bad = text.count("\ufffd") + sum(
        ord(char) < 32 and char not in "\n\r\t" for char in text
    )
    return alnum, words, bad


def image_sizes_by_page(path: Path) -> dict[int, list[tuple[int, int]]]:
    output = command(["pdfimages", "-list", str(path)]).stdout.decode(
        "utf-8", errors="replace"
    )
    images: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for line in output.splitlines()[2:]:
        fields = line.split()
        if len(fields) >= 5 and fields[0].isdigit() and fields[3].isdigit() and fields[4].isdigit():
            images[int(fields[0])].append((int(fields[3]), int(fields[4])))
    return images


def pgm_ink_ratio(data: bytes) -> float:
    """Return share of dark pixels in Poppler's 8-bit grayscale PGM output."""
    position = 0

    def token() -> bytes:
        nonlocal position
        while position < len(data):
            if data[position] in b" \t\r\n":
                position += 1
            elif data[position] == ord("#"):
                end = data.find(b"\n", position)
                position = len(data) if end < 0 else end + 1
            else:
                break
        start = position
        while position < len(data) and data[position] not in b" \t\r\n":
            position += 1
        return data[start:position]

    magic = token()
    width = int(token())
    height = int(token())
    maximum = int(token())
    if magic != b"P5" or maximum != 255 or width < 1 or height < 1:
        raise ValueError("Unexpected PGM format from pdftoppm")
    if position >= len(data) or data[position] not in b" \t\r\n":
        raise ValueError("Missing PGM header terminator")
    position += 2 if data[position:position + 2] == b"\r\n" else 1
    pixels = data[position:]
    if len(pixels) != width * height:
        raise ValueError(f"PGM pixel count {len(pixels)} != {width * height}")
    return sum(pixel < 245 for pixel in pixels) / len(pixels)


def render_ink_ratio(path: Path, page_number: int, folder: Path) -> float:
    prefix = folder / f"page_{page_number:05d}"
    command([
        "pdftoppm", "-f", str(page_number), "-l", str(page_number),
        "-scale-to", str(RENDER_SCALE), "-gray", "-singlefile",
        str(path), str(prefix),
    ])
    image = prefix.with_suffix(".pgm")
    try:
        return pgm_ink_ratio(image.read_bytes())
    finally:
        image.unlink(missing_ok=True)


def triage_decision(alnum: int, bad: int, ink: float | None,
                    image_sizes: list[tuple[int, int]],
                    render_error: str | None = None) -> tuple[str, str]:
    if bad > max(5, alnum // 100):
        return "ocr_candidate", "suspicious_native_characters"
    if alnum >= MIN_CLEAR_TEXT:
        return "native_ok", "substantial_native_text"
    full_page_image = any(min(width, height) >= 1000 for width, height in image_sizes)
    if alnum >= MIN_SHORT_TEXT and not full_page_image:
        return "native_ok", "short_native_text_without_large_image"
    if render_error or ink is None:
        return "ocr_candidate", "could_not_render_sparse_page"
    if alnum == 0 and ink < BLANK_INK_RATIO:
        return "blank", "no_text_or_visible_marks"
    if alnum == 0 or full_page_image:
        return "ocr_needed", "visible_content_with_insufficient_native_text"
    return "ocr_candidate", "short_text_with_visible_content"


@contextmanager
def transaction(conn: duckdb.DuckDBPyConnection):
    conn.execute("BEGIN TRANSACTION")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


class TextPipeline:
    def __init__(self, root: Path = ROOT, archive_db: Path = DB_PATH,
                 text_db: Path = TEXT_DB_PATH,
                 existing_versions: bool = False) -> None:
        if not archive_db.is_file():
            raise FileNotFoundError(f"Archive database is missing: {archive_db}")
        text_db.parent.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.archive = duckdb.connect(str(archive_db), read_only=True)
        self.conn = duckdb.connect(str(text_db))
        if existing_versions:
            versions = dict(self.conn.execute("""
                SELECT stage, version FROM current_versions
                WHERE stage IN ('inventory', 'native', 'triage')
            """).fetchall())
            if set(versions) != {"inventory", "native", "triage"}:
                raise RuntimeError("Run inventory, native extraction, and triage first")
            self.poppler = ""
            self.inventory_version = versions["inventory"]
            self.native_version = versions["native"]
            self.triage_version = versions["triage"]
        else:
            self.poppler = poppler_version()
            self.inventory_version = f"{INVENTORY_RULE}|{self.poppler}"
            self.native_version = f"{NATIVE_RULE}|{self.poppler}"
            self.triage_version = (
                f"{TRIAGE_RULE}|{self.poppler}|clear={MIN_CLEAR_TEXT}|short={MIN_SHORT_TEXT}"
                f"|blank={BLANK_INK_RATIO}|scale={RENDER_SCALE}"
            )
            self.create_schema()

    def close(self) -> None:
        self.conn.close()
        self.archive.close()

    def create_schema(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                pdf_sha256 VARCHAR PRIMARY KEY,
                raw_path VARCHAR NOT NULL,
                byte_count BIGINT,
                source_mtime_ns BIGINT,
                page_count INTEGER,
                status VARCHAR NOT NULL,
                error VARCHAR,
                inventory_version VARCHAR NOT NULL,
                inventoried_at VARCHAR NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS document_pages (
                pdf_sha256 VARCHAR NOT NULL,
                page_number INTEGER NOT NULL,
                PRIMARY KEY (pdf_sha256, page_number)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS source_issues (
                media_url VARCHAR PRIMARY KEY,
                download_status VARCHAR NOT NULL,
                error VARCHAR,
                observed_at VARCHAR NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS native_documents (
                pdf_sha256 VARCHAR NOT NULL,
                extractor_version VARCHAR NOT NULL,
                page_count INTEGER,
                status VARCHAR NOT NULL,
                error VARCHAR,
                processed_at VARCHAR NOT NULL,
                PRIMARY KEY (pdf_sha256, extractor_version)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS native_pages (
                pdf_sha256 VARCHAR NOT NULL,
                page_number INTEGER NOT NULL,
                extractor_version VARCHAR NOT NULL,
                text VARCHAR NOT NULL,
                alnum_count INTEGER NOT NULL,
                word_count INTEGER NOT NULL,
                bad_char_count INTEGER NOT NULL,
                PRIMARY KEY (pdf_sha256, page_number, extractor_version)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS page_triage (
                pdf_sha256 VARCHAR NOT NULL,
                page_number INTEGER NOT NULL,
                triage_version VARCHAR NOT NULL,
                native_version VARCHAR NOT NULL,
                decision VARCHAR NOT NULL,
                reason VARCHAR NOT NULL,
                ink_ratio DOUBLE,
                image_count INTEGER,
                large_image BOOLEAN,
                render_error VARCHAR,
                triaged_at VARCHAR NOT NULL,
                PRIMARY KEY (pdf_sha256, page_number, triage_version)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS stage_runs (
                run_id VARCHAR PRIMARY KEY,
                stage VARCHAR NOT NULL,
                started_at VARCHAR NOT NULL,
                finished_at VARCHAR,
                status VARCHAR NOT NULL,
                version VARCHAR NOT NULL,
                stats_json VARCHAR
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS current_versions (
                stage VARCHAR PRIMARY KEY,
                version VARCHAR NOT NULL
            )
        """)
        with transaction(self.conn):
            for stage, version in (
                ("inventory", self.inventory_version),
                ("native", self.native_version),
                ("triage", self.triage_version),
            ):
                self.conn.execute("DELETE FROM current_versions WHERE stage=?", [stage])
                self.conn.execute("INSERT INTO current_versions VALUES (?, ?)", [stage, version])
        self.conn.execute("""
            CREATE OR REPLACE VIEW current_native_pages AS
            SELECT n.* FROM native_pages n
            WHERE n.extractor_version = (
                SELECT version FROM current_versions WHERE stage = 'native'
            )
        """)
        self.conn.execute("""
            CREATE OR REPLACE VIEW current_page_triage AS
            SELECT t.* FROM page_triage t
            WHERE t.triage_version = (
                SELECT version FROM current_versions WHERE stage = 'triage'
            )
        """)
        self.conn.execute("""
            CREATE OR REPLACE VIEW ocr_queue AS
            SELECT t.pdf_sha256, t.page_number, d.raw_path, t.reason,
                   n.alnum_count, t.ink_ratio, t.image_count, t.triage_version
            FROM current_page_triage t
            JOIN documents d USING (pdf_sha256)
            JOIN native_pages n ON n.pdf_sha256 = t.pdf_sha256
                AND n.page_number = t.page_number
                AND n.extractor_version = t.native_version
            WHERE t.decision IN ('ocr_needed', 'ocr_candidate')
                AND d.status = 'ready'
        """)
        self.conn.execute("DROP VIEW IF EXISTS review_queue")
        self.conn.execute("""
            CREATE OR REPLACE VIEW ocr_candidate_queue AS
            SELECT t.pdf_sha256, t.page_number, d.raw_path, t.reason,
                   n.alnum_count, t.ink_ratio, t.image_count, t.render_error,
                   t.triage_version
            FROM current_page_triage t
            JOIN documents d USING (pdf_sha256)
            JOIN native_pages n ON n.pdf_sha256 = t.pdf_sha256
                AND n.page_number = t.page_number
                AND n.extractor_version = t.native_version
            WHERE t.decision = 'ocr_candidate' AND d.status = 'ready'
        """)

    def sources(self) -> list[tuple[str, str, int]]:
        return self.archive.execute("""
            SELECT sha256, min(raw_path), max(byte_count)
            FROM downloads WHERE status = 'downloaded' AND sha256 IS NOT NULL
            GROUP BY sha256 ORDER BY sha256
        """).fetchall()

    def refresh_source_issues(self) -> None:
        rows = self.archive.execute("""
            SELECT media_url, status, error FROM downloads WHERE status != 'downloaded'
            ORDER BY media_url
        """).fetchall()
        with transaction(self.conn):
            self.conn.execute("DELETE FROM source_issues")
            if rows:
                self.conn.executemany(
                    "INSERT INTO source_issues VALUES (?, ?, ?, ?)",
                    [(url, status, error, now_utc()) for url, status, error in rows],
                )

    def pdf_path(self, raw_path: str) -> Path:
        path = (self.root / raw_path).resolve()
        if not path.is_relative_to((self.root / "in" / "pdfs").resolve()):
            raise ValueError(f"PDF path outside archive: {raw_path}")
        return path

    def current_source(self, doc: tuple) -> bool:
        try:
            path = self.pdf_path(doc[1])
            stat = path.stat()
            return (stat.st_size == doc[2] and stat.st_mtime_ns == doc[3]
                    and sha256_file(path) == doc[0])
        except OSError:
            return False

    def start_run(self, stage: str, version: str) -> str:
        run_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO stage_runs VALUES (?, ?, ?, NULL, 'running', ?, NULL)",
            [run_id, stage, now_utc(), version],
        )
        return run_id

    def finish_run(self, run_id: str, stats: Counter[str], crashed: bool = False) -> None:
        status = "crashed" if crashed else (
            "completed_with_errors" if stats["failed"] else "complete"
        )
        self.conn.execute(
            "UPDATE stage_runs SET finished_at=?, status=?, stats_json=? WHERE run_id=?",
            [now_utc(), status, json.dumps(dict(stats), sort_keys=True), run_id],
        )

    def inventory(self, limit: int | None = None, only_sha: str | None = None) -> Counter[str]:
        stats: Counter[str] = Counter()
        run_id = self.start_run("inventory", self.inventory_version)
        try:
            self.refresh_source_issues()
            for sha, raw_path, archived_bytes in self.sources():
                if only_sha and sha != only_sha:
                    continue
                path = self.pdf_path(raw_path)
                previous = self.conn.execute(
                    "SELECT raw_path, byte_count, source_mtime_ns, page_count, status, "
                    "inventory_version FROM documents WHERE pdf_sha256=?", [sha],
                ).fetchone()
                stat = path.stat() if path.is_file() else None
                if previous and stat and previous[4] == "ready" and (
                    previous[0] == raw_path and previous[1] == stat.st_size
                    and previous[2] == stat.st_mtime_ns
                    and previous[5] == self.inventory_version
                ):
                    known_pages = self.conn.execute(
                        "SELECT count(*) FROM document_pages WHERE pdf_sha256=?", [sha]
                    ).fetchone()[0]
                    if known_pages == previous[3]:
                        stats["skipped"] += 1
                        continue
                if limit is not None and stats["processed"] >= limit:
                    break
                size = stat.st_size if stat else None
                mtime = stat.st_mtime_ns if stat else None
                pages = None
                status = "ready"
                error = None
                try:
                    if not stat:
                        raise FileNotFoundError(path)
                    if size != archived_bytes:
                        raise ValueError(f"Size {size} != archived {archived_bytes}")
                    if sha256_file(path) != sha:
                        raise ValueError("Source file SHA-256 differs from archive record")
                    pages = page_count(path)
                except (OSError, RuntimeError, ValueError) as exc:
                    status, error = "error", str(exc)
                    stats["failed"] += 1
                    print(f"  inventory ERROR {sha[:12]}: {error}", file=sys.stderr)
                with transaction(self.conn):
                    self.conn.execute("DELETE FROM document_pages WHERE pdf_sha256=?", [sha])
                    self.conn.execute("DELETE FROM documents WHERE pdf_sha256=?", [sha])
                    self.conn.execute(
                        "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [sha, raw_path, size, mtime, pages, status, error,
                         self.inventory_version, now_utc()],
                    )
                    if pages:
                        self.conn.executemany(
                            "INSERT INTO document_pages VALUES (?, ?)",
                            [(sha, page) for page in range(1, pages + 1)],
                        )
                stats["processed"] += 1
                if stats["processed"] % 250 == 0:
                    print(f"  inventoried {stats['processed']} PDFs", flush=True)
        except Exception:
            self.finish_run(run_id, stats, crashed=True)
            raise
        self.finish_run(run_id, stats)
        print(f"Inventory: {dict(stats)}", flush=True)
        return stats

    def ready_documents(self) -> list[tuple]:
        return self.conn.execute("""
            SELECT pdf_sha256, raw_path, byte_count, source_mtime_ns, page_count
            FROM documents WHERE status='ready' ORDER BY pdf_sha256
        """).fetchall()

    def native_complete(self, sha: str, pages: int) -> bool:
        row = self.conn.execute("""
            SELECT status, page_count FROM native_documents
            WHERE pdf_sha256=? AND extractor_version=?
        """, [sha, self.native_version]).fetchone()
        if not row or row != ("complete", pages):
            return False
        count = self.conn.execute("""
            SELECT count(*) FROM native_pages
            WHERE pdf_sha256=? AND extractor_version=?
        """, [sha, self.native_version]).fetchone()[0]
        return count == pages

    def record_native(self, sha: str, pages: int, texts: list[str] | None,
                      error: str | None) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM native_pages WHERE pdf_sha256=? AND extractor_version=?",
                [sha, self.native_version],
            )
            self.conn.execute(
                "DELETE FROM native_documents WHERE pdf_sha256=? AND extractor_version=?",
                [sha, self.native_version],
            )
            self.conn.execute(
                "INSERT INTO native_documents VALUES (?, ?, ?, ?, ?, ?)",
                [sha, self.native_version, pages, "failed" if error else "complete",
                 error, now_utc()],
            )
            if texts is not None:
                self.conn.executemany(
                    "INSERT INTO native_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        [sha, number, self.native_version, text, *native_metrics(text)]
                        for number, text in enumerate(texts, start=1)
                    ],
                )

    def native(self, limit: int | None = None, only_sha: str | None = None) -> Counter[str]:
        stats: Counter[str] = Counter()
        run_id = self.start_run("native", self.native_version)
        try:
            for sha, raw_path, size, mtime, pages in self.ready_documents():
                if only_sha and sha != only_sha:
                    continue
                if not self.current_source((sha, raw_path, size, mtime, pages)):
                    stats["failed"] += 1
                    print(f"  native ERROR {sha[:12]}: source changed since inventory",
                          file=sys.stderr)
                    continue
                if self.native_complete(sha, pages):
                    stats["skipped"] += 1
                    continue
                if limit is not None and stats["processed"] >= limit:
                    break
                error = None
                texts = None
                try:
                    output = command([
                        "pdftotext", "-enc", "UTF-8", "-layout",
                        str(self.pdf_path(raw_path)), "-",
                    ]).stdout
                    texts = split_page_text(output, pages)
                except (OSError, RuntimeError, ValueError) as exc:
                    error = str(exc)
                    stats["failed"] += 1
                    print(f"  native ERROR {sha[:12]}: {error}", file=sys.stderr)
                self.record_native(sha, pages, texts, error)
                stats["processed"] += 1
                if stats["processed"] % 250 == 0:
                    print(f"  extracted native text from {stats['processed']} PDFs", flush=True)
        except Exception:
            self.finish_run(run_id, stats, crashed=True)
            raise
        self.finish_run(run_id, stats)
        print(f"Native text: {dict(stats)}", flush=True)
        return stats

    def triage_complete(self, sha: str, pages: int) -> bool:
        count = self.conn.execute("""
            SELECT count(*) FROM page_triage
            WHERE pdf_sha256=? AND triage_version=? AND native_version=?
        """, [sha, self.triage_version, self.native_version]).fetchone()[0]
        return count == pages

    def triage(self, limit: int | None = None, only_sha: str | None = None) -> Counter[str]:
        stats: Counter[str] = Counter()
        run_id = self.start_run("triage", self.triage_version)
        try:
            for sha, raw_path, size, mtime, pages in self.ready_documents():
                if only_sha and sha != only_sha:
                    continue
                if not self.current_source((sha, raw_path, size, mtime, pages)):
                    stats["failed"] += 1
                    print(f"  triage ERROR {sha[:12]}: source changed since inventory",
                          file=sys.stderr)
                    continue
                if not self.native_complete(sha, pages):
                    stats["awaiting_native"] += 1
                    continue
                if self.triage_complete(sha, pages):
                    stats["skipped"] += 1
                    continue
                if limit is not None and stats["processed"] >= limit:
                    break
                path = self.pdf_path(raw_path)
                native_rows = self.conn.execute("""
                    SELECT page_number, alnum_count, bad_char_count FROM native_pages
                    WHERE pdf_sha256=? AND extractor_version=? ORDER BY page_number
                """, [sha, self.native_version]).fetchall()
                sparse = any(alnum < MIN_CLEAR_TEXT or bad > max(5, alnum // 100)
                             for _, alnum, bad in native_rows)
                image_error = None
                try:
                    images = image_sizes_by_page(path) if sparse else {}
                except (OSError, RuntimeError, ValueError) as exc:
                    images = {}
                    image_error = str(exc)
                    print(f"  image inventory warning {sha[:12]}: {exc}", file=sys.stderr)
                triage_rows = []
                decisions: Counter[str] = Counter()
                with tempfile.TemporaryDirectory(prefix="court-triage-") as folder:
                    for number, alnum, bad in native_rows:
                        sizes = images.get(number, [])
                        ink = None
                        render_error = None
                        if alnum < MIN_CLEAR_TEXT or bad > max(5, alnum // 100):
                            try:
                                ink = render_ink_ratio(path, number, Path(folder))
                            except (OSError, RuntimeError, ValueError) as exc:
                                render_error = str(exc)
                        decision, reason = triage_decision(
                            alnum, bad, ink, sizes, render_error
                        )
                        if image_error and alnum < MIN_CLEAR_TEXT:
                            decision, reason = "ocr_candidate", "could_not_inspect_images"
                            render_error = image_error
                        triage_rows.append([
                            sha, number, self.triage_version, self.native_version,
                            decision, reason, ink, len(sizes),
                            any(min(width, height) >= 1000 for width, height in sizes),
                            render_error, now_utc(),
                        ])
                        decisions[decision] += 1
                with transaction(self.conn):
                    self.conn.execute(
                        "DELETE FROM page_triage WHERE pdf_sha256=? AND triage_version=?",
                        [sha, self.triage_version],
                    )
                    self.conn.executemany(
                        "INSERT INTO page_triage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        triage_rows,
                    )
                stats.update(decisions)
                stats["processed"] += 1
                if stats["processed"] % 250 == 0:
                    print(f"  triaged {stats['processed']} PDFs", flush=True)
        except Exception:
            self.finish_run(run_id, stats, crashed=True)
            raise
        self.finish_run(run_id, stats)
        print(f"Triage: {dict(stats)}", flush=True)
        return stats

    def status(self) -> None:
        source = self.archive.execute("""
            SELECT status, count(*) FROM downloads GROUP BY status ORDER BY status
        """).fetchall()
        docs = self.conn.execute("""
            SELECT status, count(*) FROM documents GROUP BY status ORDER BY status
        """).fetchall()
        pages = self.conn.execute("SELECT count(*) FROM document_pages").fetchone()[0]
        native_docs = self.conn.execute("""
            SELECT status, count(*) FROM native_documents
            WHERE extractor_version=? GROUP BY status ORDER BY status
        """, [self.native_version]).fetchall()
        native_pages = self.conn.execute("""
            SELECT count(*), sum(CASE WHEN alnum_count=0 THEN 1 ELSE 0 END)
            FROM native_pages WHERE extractor_version=?
        """, [self.native_version]).fetchone()
        triage = self.conn.execute("""
            SELECT decision, count(*) FROM page_triage
            WHERE triage_version=? GROUP BY decision ORDER BY decision
        """, [self.triage_version]).fetchall()
        issues = self.conn.execute("SELECT count(*) FROM source_issues").fetchone()[0]
        print(f"Archive URL records: {source}; source issues: {issues}")
        print(f"PDF inventory: {docs}; inventoried pages: {pages}")
        print(f"Native documents: {native_docs}; native pages: {native_pages[0] or 0}; "
              f"zero-text pages: {native_pages[1] or 0}")
        print(f"Page triage: {triage}")
        print(f"OCR queue pages: {self.conn.execute('SELECT count(*) FROM ocr_queue WHERE triage_version=?', [self.triage_version]).fetchone()[0]}")
        print(f"Ambiguous OCR candidates: {self.conn.execute('SELECT count(*) FROM ocr_candidate_queue').fetchone()[0]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["inventory", "native", "triage", "all", "status"])
    parser.add_argument("--limit", type=int, help="Maximum new PDFs to process in each stage")
    parser.add_argument("--sha", help="Process only one PDF SHA-256")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.sha and not re.fullmatch(r"[0-9a-f]{64}", args.sha):
        parser.error("--sha must be a full lowercase SHA-256")
    if args.stage == "status" and (args.limit or args.sha):
        parser.error("status does not accept --limit or --sha")

    pipeline = TextPipeline()
    try:
        if args.stage == "status":
            pipeline.status()
            return 0
        stages = ["inventory", "native", "triage"] if args.stage == "all" else [args.stage]
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
