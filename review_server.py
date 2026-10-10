"""Small, read-only local browser for comparing PDF pages with extracted text."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import duckdb

from config import ROOT, TEXT_DB_PATH
from listen import DEFAULT_HOST, display_url


QA_DB_PATH = ROOT / "data" / "triage_qa.duckdb"
ARCHIVE_DIR = (ROOT / "in" / "pdfs").resolve()
PAGE_RE = re.compile(r"[0-9a-f]{64}")
HTML_PATH = ROOT / "review.html"


def valid_page_query(query: dict[str, list[str]]) -> tuple[str, int]:
    sha = query.get("sha", [""])[0]
    raw_page = query.get("page", [""])[0]
    if not PAGE_RE.fullmatch(sha) or not raw_page.isdecimal():
        raise ValueError("Expected a PDF SHA-256 and positive page number")
    page = int(raw_page)
    if page < 1:
        raise ValueError("Page number must be positive")
    return sha, page


def newest_qa_version(qa: duckdb.DuckDBPyConnection) -> str | None:
    row = qa.execute("""
        SELECT qa_version FROM qa_runs
        WHERE stage='recompare' AND status='complete'
        ORDER BY finished_at DESC LIMIT 1
    """).fetchone()
    if row:
        return row[0]
    row = qa.execute("""
        SELECT qa_version FROM screen_documents
        WHERE status='complete' ORDER BY processed_at DESC LIMIT 1
    """).fetchone()
    return row[0] if row else None


def source_pdf(text_db: duckdb.DuckDBPyConnection, sha: str, page: int) -> tuple[Path, int]:
    row = text_db.execute("""
        SELECT raw_path,page_count FROM documents
        WHERE pdf_sha256=? AND status='ready'
    """, [sha]).fetchone()
    if not row or page > row[1]:
        raise LookupError("PDF page not in the inventory")
    path = (ROOT / row[0]).resolve()
    if not path.is_relative_to(ARCHIVE_DIR) or path.name != f"{sha}.pdf":
        raise ValueError("Archive path does not match the PDF checksum")
    return path, row[1]


def page_payload(text_db: duckdb.DuckDBPyConnection,
                 qa: duckdb.DuckDBPyConnection | None,
                 sha: str, page: int) -> dict:
    _path, page_count = source_pdf(text_db, sha, page)
    row = text_db.execute("""
        SELECT t.decision,t.reason,n.text,s.status,s.source,s.reason,
               a.text,o.text,o.status,o.mean_word_confidence
        FROM current_page_triage t
        JOIN current_native_pages n USING(pdf_sha256,page_number)
        LEFT JOIN current_selected_pages s USING(pdf_sha256,page_number)
        LEFT JOIN current_analysis_pages a USING(pdf_sha256,page_number)
        LEFT JOIN current_ocr_pages o USING(pdf_sha256,page_number)
        WHERE t.pdf_sha256=? AND t.page_number=?
    """, [sha, page]).fetchone()
    if row is None:
        raise LookupError("Page text is missing from the inventory")
    (decision, triage_reason, native, selected_status, selected_source,
     selected_reason, selected, ocr, ocr_status, ocr_confidence) = row
    issues = []
    if decision != "native_ok":
        issues.append(decision)
    if selected_status == "unresolved":
        issues.append("unresolved_text")
    qa_category = None
    ocr_origin = "production" if ocr is not None else None
    if qa:
        qa_version = newest_qa_version(qa)
        if qa_version:
            qa_row = qa.execute("""
                SELECT flags_json,category,max_image_area,image_count
                FROM qa_pages WHERE pdf_sha256=? AND page_number=?
                AND qa_version=?
            """, [sha, page, qa_version]).fetchone()
            if qa_row:
                issues.extend(json.loads(qa_row[0]))
                image_area, image_count = qa_row[2], qa_row[3]
            else:
                image_area = image_count = None
            comparison = qa.execute("""
                SELECT category,text FROM qa_ocr
                WHERE pdf_sha256=? AND page_number=? AND qa_version=?
            """, [sha, page, qa_version]).fetchone()
            if comparison:
                qa_category = comparison[0]
                if qa_category not in {"ocr_agrees", "qa_error"}:
                    issues.append(qa_category)
                if ocr is None and comparison[1] is not None:
                    ocr, ocr_origin = comparison[1], "QA sample"
        else:
            image_area = image_count = None
    else:
        image_area = image_count = None
    return {
        "sha": sha, "page": page, "page_count": page_count,
        "triage": decision, "triage_reason": triage_reason,
        "selected_status": selected_status, "selected_source": selected_source,
        "selected_reason": selected_reason,
        "native_text": native, "selected_text": selected, "ocr_text": ocr,
        "ocr_status": ocr_status, "ocr_origin": ocr_origin,
        "ocr_mean_confidence": ocr_confidence,
        "qa_category": qa_category, "image_count": image_count,
        "max_image_area": image_area,
        "issues": list(dict.fromkeys(issues)),
    }


def random_page(text_db: duckdb.DuckDBPyConnection,
                qa: duckdb.DuckDBPyConnection | None,
                mode: str) -> tuple[str, int]:
    if mode == "native_ok":
        row = text_db.execute("""
            SELECT pdf_sha256,page_number FROM current_page_triage
            WHERE decision='native_ok' ORDER BY random() LIMIT 1
        """).fetchone()
    elif mode == "unresolved":
        row = text_db.execute("""
            SELECT pdf_sha256,page_number FROM current_selected_pages
            WHERE status='unresolved' ORDER BY random() LIMIT 1
        """).fetchone()
    elif mode == "flagged":
        if not qa or not (version := newest_qa_version(qa)):
            raise LookupError("QA screen results are unavailable")
        row = qa.execute("""
            SELECT pdf_sha256,page_number FROM qa_pages
            WHERE qa_version=? AND
                  (category='risk_flagged' OR triage_decision!='native_ok')
            ORDER BY random() LIMIT 1
        """, [version]).fetchone()
    else:
        raise ValueError("Unknown review mode")
    if not row:
        raise LookupError("No pages available for this mode")
    return row


class ReviewHandler(BaseHTTPRequestHandler):
    server: "ReviewServer"

    def send_body(self, status: HTTPStatus, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: HTTPStatus, value: dict) -> None:
        self.send_body(status, "application/json; charset=utf-8",
                       json.dumps(value, ensure_ascii=False).encode())

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path in {"/", "/review.html"}:
                self.send_body(HTTPStatus.OK, "text/html; charset=utf-8",
                               HTML_PATH.read_bytes())
                return
            if parsed.path == "/api/random":
                with duckdb.connect(str(TEXT_DB_PATH), read_only=True) as text_db:
                    if QA_DB_PATH.is_file():
                        with duckdb.connect(str(QA_DB_PATH), read_only=True) as qa:
                            sha, page = random_page(text_db, qa, query.get("mode", ["native_ok"])[0])
                            payload = page_payload(text_db, qa, sha, page)
                    else:
                        sha, page = random_page(text_db, None, query.get("mode", ["native_ok"])[0])
                        payload = page_payload(text_db, None, sha, page)
                self.send_json(HTTPStatus.OK, payload)
                return
            if parsed.path in {"/api/page", "/api/render"}:
                sha, page = valid_page_query(query)
                with duckdb.connect(str(TEXT_DB_PATH), read_only=True) as text_db:
                    if parsed.path == "/api/page":
                        if QA_DB_PATH.is_file():
                            with duckdb.connect(str(QA_DB_PATH), read_only=True) as qa:
                                payload = page_payload(text_db, qa, sha, page)
                        else:
                            payload = page_payload(text_db, None, sha, page)
                        self.send_json(HTTPStatus.OK, payload)
                    else:
                        pdf, _page_count = source_pdf(text_db, sha, page)
                        with tempfile.TemporaryDirectory(prefix="court-review-") as folder:
                            output = Path(folder) / "page"
                            subprocess.run([
                                "pdftoppm", "-f", str(page), "-l", str(page),
                                "-scale-to", "1600", "-png", "-singlefile",
                                str(pdf), str(output),
                            ], check=True, capture_output=True, timeout=90)
                            png = output.with_suffix(".png").read_bytes()
                        self.send_body(HTTPStatus.OK, "image/png", png)
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found"})
        except (ValueError, LookupError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (OSError, subprocess.SubprocessError, duckdb.Error) as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not TEXT_DB_PATH.is_file():
        parser.error(f"Missing text database: {TEXT_DB_PATH}")
    server = ReviewServer((args.host, args.port), ReviewHandler)
    print(f"Review tool listening on {display_url(args.host, args.port)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
