"""Read-only coverage check for the current production PDF text output."""

from __future__ import annotations

import sys

import duckdb

from config import TEXT_DB_PATH


def count(conn: duckdb.DuckDBPyConnection, query: str) -> int:
    return conn.execute(query).fetchone()[0]


def main() -> int:
    if not TEXT_DB_PATH.is_file():
        print(f"Missing text database: {TEXT_DB_PATH}", file=sys.stderr)
        return 1

    with duckdb.connect(str(TEXT_DB_PATH), read_only=True) as conn:
        counts = {
            "PDFs": count(conn, "SELECT count(*) FROM documents WHERE status='ready'"),
            "inventory pages": count(conn, "SELECT count(*) FROM document_pages"),
            "native pages": count(conn, "SELECT count(*) FROM current_native_pages"),
            "triaged pages": count(conn, "SELECT count(*) FROM current_page_triage"),
            "OCR queue": count(conn, "SELECT count(*) FROM ocr_queue"),
            "completed OCR pages": count(conn, "SELECT count(*) FROM current_ocr_pages WHERE status='complete'"),
            "selected pages": count(conn, "SELECT count(*) FROM current_selected_pages"),
            "analysis pages": count(conn, "SELECT count(*) FROM current_analysis_pages"),
            "audited PDFs": count(conn, "SELECT count(*) FROM current_audit_documents"),
        }
        checks = {
            "no inventoried PDF failed": """
                SELECT count(*) FROM documents WHERE status!='ready'
            """,
            "inventory matches document page counts": """
                SELECT abs((SELECT count(*) FROM document_pages) -
                           (SELECT coalesce(sum(page_count),0) FROM documents WHERE status='ready'))
            """,
            "native keys match inventory": """
                SELECT count(*) FROM (
                    (SELECT pdf_sha256,page_number FROM document_pages
                     EXCEPT SELECT pdf_sha256,page_number FROM current_native_pages)
                    UNION ALL
                    (SELECT pdf_sha256,page_number FROM current_native_pages
                     EXCEPT SELECT pdf_sha256,page_number FROM document_pages)
                )
            """,
            "triage keys match inventory": """
                SELECT count(*) FROM (
                    (SELECT pdf_sha256,page_number FROM document_pages
                     EXCEPT SELECT pdf_sha256,page_number FROM current_page_triage)
                    UNION ALL
                    (SELECT pdf_sha256,page_number FROM current_page_triage
                     EXCEPT SELECT pdf_sha256,page_number FROM document_pages)
                )
            """,
            "OCR queue has completed results": """
                SELECT count(*) FROM ocr_queue q
                LEFT JOIN current_ocr_pages o USING(pdf_sha256,page_number)
                WHERE o.pdf_sha256 IS NULL OR o.status!='complete'
            """,
            "selection keys match inventory": """
                SELECT count(*) FROM (
                    (SELECT pdf_sha256,page_number FROM document_pages
                     EXCEPT SELECT pdf_sha256,page_number FROM current_selected_pages)
                    UNION ALL
                    (SELECT pdf_sha256,page_number FROM current_selected_pages
                     EXCEPT SELECT pdf_sha256,page_number FROM document_pages)
                )
            """,
            "analysis keys match inventory": """
                SELECT count(*) FROM (
                    (SELECT pdf_sha256,page_number FROM document_pages
                     EXCEPT SELECT pdf_sha256,page_number FROM current_analysis_pages)
                    UNION ALL
                    (SELECT pdf_sha256,page_number FROM current_analysis_pages
                     EXCEPT SELECT pdf_sha256,page_number FROM document_pages)
                )
            """,
            "usable pages have text and a valid source": """
                SELECT count(*) FROM current_analysis_pages
                WHERE status='usable' AND
                      (text IS NULL OR length(trim(text))=0 OR source NOT IN ('native','ocr'))
            """,
            "non-usable pages have no selected text": """
                SELECT count(*) FROM current_analysis_pages
                WHERE status!='usable' AND (text IS NOT NULL OR source IS NOT NULL)
            """,
            "page outcomes use known statuses": """
                SELECT count(*) FROM current_analysis_pages
                WHERE status NOT IN ('usable','blank','unresolved')
            """,
            "audit keys match ready PDFs": """
                SELECT count(*) FROM (
                    (SELECT pdf_sha256 FROM documents WHERE status='ready'
                     EXCEPT SELECT pdf_sha256 FROM current_audit_documents)
                    UNION ALL
                    (SELECT pdf_sha256 FROM current_audit_documents
                     EXCEPT SELECT pdf_sha256 FROM documents WHERE status='ready')
                )
            """,
            "no incomplete audits": """
                SELECT count(*) FROM current_audit_documents
                WHERE status NOT IN ('ready','uncertain')
            """,
        }
        failures = {label: value for label, query in checks.items()
                    if (value := count(conn, query)) != 0}
        if counts["PDFs"] == 0:
            failures["no ready PDFs"] = 1
        outcomes = conn.execute("""
            SELECT status,coalesce(source,'none'),count(*)
            FROM current_analysis_pages GROUP BY 1,2 ORDER BY 1,2
        """).fetchall()
        audit = conn.execute("""
            SELECT status,count(*) FROM current_audit_documents
            GROUP BY 1 ORDER BY 1
        """).fetchall()

    for label, value in counts.items():
        print(f"{label}: {value}")
    print(f"Page outcomes: {outcomes}")
    print(f"Document audit: {audit}")
    if failures:
        for label, value in failures.items():
            print(f"FAIL: {label}: {value}", file=sys.stderr)
        return 1
    print("PASS: current production text has complete page and document coverage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
