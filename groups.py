"""Deterministic docket grouping of court PDFs, with per-document membership basis.

Each PDF's own docket numbers come from its page-1 caption ("Docket No. ...")
and, as a fallback, from the listing's docket field. Documents are joined only
through docket numbers that appear in a document's grouping key; a caption
listing several dockets therefore joins their groups. Near misses and listing
disagreements never merge anything; they are recorded as tags so later steps
can weigh them. Rebuilding is deterministic for the same archive and text.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from datetime import date

import duckdb

from config import DB_PATH, ROOT, TEXT_DB_PATH


GROUPS_DB_PATH = ROOT / "data" / "groups.duckdb"
GROUPING_VERSION = "g1"
CAPTION_WINDOW = 3000

DOCKET_RE = re.compile(
    r"\b(?:(\d{1,3})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{2})\s*-?\s*V\s*[a-z]{2,3}\b"
    r"|(\d{2})\s*-?\s*(ENV|CC|EC|AP)\s*-?\s*(\d{2,6})\b"
    r"|eo\s*(\d{2})-(\d{2,4})\b)", re.I)
LABEL_RE = re.compile(r"(?:Docket|Case)\s+Nos?\.?\s*:?", re.I)
HOUSING_RE = re.compile(
    r"dwelling units?|single[- ]family|two[- ]family|multi[- ]?family|duplex|triplex"
    r"|apartments?|residential units?|condominium|townhouses?|housing units?"
    r"|planned (?:unit|residential) development|\bPUD\b|\bPRD\b"
    r"|\d+[- ]lot (?:residential )?subdivision", re.I)
MUNICIPAL_RE = re.compile(
    r"Development Review Board|Zoning Board of Adjustment|\bDRB\b|\bZBA\b"
    r"|Planning Commission|zoning administrator|24 V\.S\.A\. ?§ ?44(?:65|71|72)", re.I)

# Membership basis of one document, from strongest to weakest.
BASES = ("agree", "caption_extra", "caption_fewer", "caption_only",
         "disagree", "listing_only", "none")


def normalize_match(m: re.Match) -> str:
    if m[1]:
        return f"{int(m[1])}-{int(m[2])}-{m[3]} Vtec"
    if m[4]:
        return f"{m[4]}-{m[5].upper()}-{int(m[6]):05d}"
    return f"eo{m[7]}-{int(m[8]):03d}"


def normalize_dockets(text: str | None) -> list[str]:
    """All docket-shaped strings in text, normalized, in first-seen order."""
    seen: dict[str, None] = {}
    for m in DOCKET_RE.finditer(text or ""):
        seen.setdefault(normalize_match(m))
    return list(seen)


def caption_dockets(page_one: str | None) -> list[str]:
    """Dockets immediately following a "Docket No." or "Case No." label on page 1.

    Citations in the opening text ("In re X, No. 1-2-11 Vtec, slip op.") carry no
    label and are ignored. Continuation lines of a multi-docket caption are kept
    while the text between numbers contains no ordinary words.
    """
    text = (page_one or "")[:CAPTION_WINDOW]
    seen: dict[str, None] = {}
    for label in LABEL_RE.finditer(text):
        segment = text[label.end():label.end() + 160]
        position = 0
        for m in DOCKET_RE.finditer(segment):
            gap = segment[position:m.start()]
            if position == 0 and gap.strip(" \t\r\n|{}:"):
                break
            if position and re.search(r"[A-Za-z]{3,}", re.sub(r"Docket|Nos?\b|and", "", gap)):
                break
            seen.setdefault(normalize_match(m))
            position = m.end()
    return list(seen)


def filing_period(docket: str) -> tuple[int, int] | None:
    """(year, month) encoded by a docket number, when the format encodes one."""
    if m := re.fullmatch(r"\d+-(\d+)-(\d\d) Vtec", docket):
        year = int(m[2])
        return (2000 + year if year < 70 else 1900 + year, int(m[1]))
    if m := re.fullmatch(r"(\d\d)-[A-Z]+-\d+", docket):
        return (2000 + int(m[1]), 1)
    return None


def one_component_apart(a: str, b: str) -> bool:
    """True for two same-format dockets differing in exactly one number."""
    na, nb = re.findall(r"\d+", a), re.findall(r"\d+", b)
    same_format = re.sub(r"\d+", "#", a) == re.sub(r"\d+", "#", b)
    return same_format and len(na) == len(nb) and sum(x != y for x, y in zip(na, nb)) == 1


def classify(listing: list[str], caption: list[str]) -> tuple[str, list[str]]:
    """Membership basis and the dockets used as the grouping key."""
    ls, cs = set(listing), set(caption)
    if cs and ls:
        if cs == ls:
            return "agree", caption
        if ls < cs:
            return "caption_extra", caption
        if cs < ls:
            return "caption_fewer", caption
        return "disagree", caption
    if cs:
        return "caption_only", caption
    if ls:
        return "listing_only", listing
    return "none", []


def document_flags(listing: list[str], caption: list[str], key: list[str],
                   posted: str | None) -> list[str]:
    flags = []
    if set(listing) != set(caption) and listing and caption:
        for c in set(caption) - set(listing):
            if any(one_component_apart(c, l) for l in set(listing) - set(caption)):
                flags.append(f"typo_like:{c}")
    if posted:
        try:
            posted_on = date.fromisoformat(posted[:10])
        except ValueError:
            posted_on = None
        for docket in key:
            period = filing_period(docket)
            if posted_on and period and period > (posted_on.year, posted_on.month):
                flags.append(f"filed_after_posting:{docket}")
    return flags


def load_documents() -> list[dict]:
    with duckdb.connect(str(DB_PATH), read_only=True) as archive:
        snapshot = archive.execute(
            "SELECT snapshot_id FROM snapshots ORDER BY snapshot_id DESC LIMIT 1").fetchone()
        if not snapshot:
            raise RuntimeError("No archive listing snapshot found")
        listing_rows = archive.execute("""
            SELECT d.sha256, e.title, e.date_attribute, e.docket_raw
            FROM listing_entries e JOIN downloads d ON d.media_url = e.media_url
            WHERE e.snapshot_id = ? AND d.status = 'downloaded' AND d.sha256 IS NOT NULL
            ORDER BY e.page_index, e.ordinal
        """, [snapshot[0]]).fetchall()
    listings: dict[str, dict] = {}
    for sha, title, posted, raw in listing_rows:
        entry = listings.setdefault(sha, {"title": title, "posted": posted, "raw": [], "dockets": []})
        if posted and (entry["posted"] is None or posted < entry["posted"]):
            entry["posted"] = posted
        if raw:
            entry["raw"].append(raw)
        for docket in normalize_dockets(raw):
            if docket not in entry["dockets"]:
                entry["dockets"].append(docket)
    with duckdb.connect(str(TEXT_DB_PATH), read_only=True) as text_db:
        pages = text_db.execute("""
            SELECT pdf_sha256, page_number, text FROM current_analysis_pages
            ORDER BY pdf_sha256, page_number
        """).fetchall()
    texts: dict[str, list[str]] = collections.defaultdict(list)
    for sha, _, text in pages:
        texts[sha].append(text or "")
    documents = []
    for sha, page_texts in texts.items():
        entry = listings.get(sha, {"title": None, "posted": None, "raw": [], "dockets": []})
        caption = caption_dockets(page_texts[0])
        basis, key = classify(entry["dockets"], caption)
        full = "\n".join(page_texts)
        documents.append({
            "sha": sha, "title": entry["title"], "posted": entry["posted"],
            "listing_raw": " | ".join(entry["raw"]) or None,
            "listing_dockets": entry["dockets"], "caption_dockets": caption,
            "key_dockets": key, "basis": basis,
            "flags": document_flags(entry["dockets"], caption, key, entry["posted"]),
            "pages": len(page_texts), "chars": len(full),
            "housing_terms": len(HOUSING_RE.findall(full)),
            "municipal_terms": len(MUNICIPAL_RE.findall(full)),
        })
    return documents


def build_groups(documents: list[dict]) -> list[dict]:
    """Union-find over key dockets; a document without dockets stands alone."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for doc in documents:
        key = sorted(doc["key_dockets"])
        for docket in key:
            find(docket)
        for docket in key[1:]:
            a, b = find(key[0]), find(docket)
            if a != b:
                parent[max(a, b)] = min(a, b)
    members: dict[str, list[dict]] = collections.defaultdict(list)
    for doc in documents:
        root = find(doc["key_dockets"][0]) if doc["key_dockets"] else f"pdf:{doc['sha']}"
        members[root].append(doc)
    groups = []
    for root, docs in members.items():
        docs.sort(key=lambda d: (d["posted"] is None, d["posted"] or "", d["sha"]))
        dockets = sorted({d for doc in docs for d in doc["key_dockets"]},
                         key=lambda d: (filing_period(d) or (0, 0), d))
        group_id = ("g-" + re.sub(r"[^a-z0-9]+", "-", dockets[0].lower()).strip("-")
                    if dockets else f"g-pdf-{docs[0]['sha'][:12]}")
        key_sets = {tuple(sorted(doc["key_dockets"])) for doc in docs}
        weak = [doc for doc in docs if doc["basis"] in {"disagree", "listing_only", "none"}
                or doc["flags"]]
        if len(docs) == 1:
            composition = "single"
        elif len(key_sets) == 1:
            composition = "uniform"
        else:
            composition = "linked"
        groups.append({
            "group_id": group_id, "dockets": dockets, "documents": docs,
            "composition": composition, "weak_documents": len(weak),
            "chars": sum(doc["chars"] for doc in docs),
            "pages": sum(doc["pages"] for doc in docs),
            "housing_terms": sum(doc["housing_terms"] for doc in docs),
            "municipal_terms": sum(doc["municipal_terms"] for doc in docs),
        })
    groups.sort(key=lambda g: g["group_id"])
    ids = collections.Counter(g["group_id"] for g in groups)
    if any(count > 1 for count in ids.values()):
        raise RuntimeError("Duplicate group IDs")
    return groups


def write_groups(groups: list[dict], path=GROUPS_DB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(path)) as conn:
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS group_documents")
        conn.execute("DROP TABLE IF EXISTS groups")
        conn.execute("DROP TABLE IF EXISTS grouping_meta")
        conn.execute("CREATE TABLE grouping_meta (version VARCHAR, built_at VARCHAR)")
        conn.execute("INSERT INTO grouping_meta VALUES (?, current_timestamp::VARCHAR)",
                     [GROUPING_VERSION])
        conn.execute("""
            CREATE TABLE groups (
                group_id VARCHAR PRIMARY KEY, dockets VARCHAR[] NOT NULL,
                doc_count INTEGER NOT NULL, composition VARCHAR NOT NULL,
                weak_documents INTEGER NOT NULL, pages INTEGER NOT NULL,
                text_chars BIGINT NOT NULL, housing_terms INTEGER NOT NULL,
                municipal_terms INTEGER NOT NULL
            )""")
        conn.execute("""
            CREATE TABLE group_documents (
                group_id VARCHAR NOT NULL, ordinal INTEGER NOT NULL,
                pdf_sha256 VARCHAR NOT NULL UNIQUE, listing_title VARCHAR,
                listing_date VARCHAR, listing_raw VARCHAR,
                listing_dockets VARCHAR[] NOT NULL, caption_dockets VARCHAR[] NOT NULL,
                key_dockets VARCHAR[] NOT NULL, basis VARCHAR NOT NULL,
                flags VARCHAR[] NOT NULL, pages INTEGER NOT NULL, text_chars BIGINT NOT NULL,
                housing_terms INTEGER NOT NULL, municipal_terms INTEGER NOT NULL,
                PRIMARY KEY (group_id, ordinal)
            )""")
        for g in groups:
            conn.execute("INSERT INTO groups VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [g["group_id"], g["dockets"], len(g["documents"]), g["composition"],
                          g["weak_documents"], g["pages"], g["chars"],
                          g["housing_terms"], g["municipal_terms"]])
            for ordinal, d in enumerate(g["documents"], 1):
                conn.execute("INSERT INTO group_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             [g["group_id"], ordinal, d["sha"], d["title"], d["posted"],
                              d["listing_raw"], d["listing_dockets"], d["caption_dockets"],
                              d["key_dockets"], d["basis"], d["flags"], d["pages"], d["chars"],
                              d["housing_terms"], d["municipal_terms"]])
        conn.execute("COMMIT")


def summary(path=GROUPS_DB_PATH) -> str:
    with duckdb.connect(str(path), read_only=True) as conn:
        version = conn.execute("SELECT version, built_at FROM grouping_meta").fetchone()
        lines = [f"Grouping {version[0]} built {version[1][:19]}"]
        docs, groups = conn.execute(
            "SELECT (SELECT count(*) FROM group_documents), count(*) FROM groups").fetchone()
        lines.append(f"{docs:,} PDFs in {groups:,} groups")
        lines.append("Document basis: " + ", ".join(
            f"{b} {n}" for b, n in conn.execute("""
                SELECT basis, count(*) FROM group_documents GROUP BY 1 ORDER BY 2 DESC""").fetchall()))
        lines.append("Flagged documents: " + str(conn.execute(
            "SELECT count(*) FROM group_documents WHERE len(flags) > 0").fetchone()[0]))
        lines.append("Group composition: " + ", ".join(
            f"{c} {n}" for c, n in conn.execute("""
                SELECT composition, count(*) FROM groups GROUP BY 1 ORDER BY 2 DESC""").fetchall()))
        lines.append("Documents per group: " + ", ".join(
            f"{k}:{n}" for k, n in conn.execute("""
                SELECT doc_count, count(*) FROM groups GROUP BY 1 ORDER BY 1""").fetchall()))
        p50, p90, p99, top = conn.execute("""
            SELECT quantile_disc(text_chars, 0.5), quantile_disc(text_chars, 0.9),
                   quantile_disc(text_chars, 0.99), max(text_chars) FROM groups""").fetchone()
        lines.append(f"Characters per group: median {p50:,}, p90 {p90:,}, p99 {p99:,}, max {top:,}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="rebuild data/groups.duckdb from archive and text databases")
    sub.add_parser("status", help="summarize the current grouping")
    show = sub.add_parser("show", help="print one group's documents and membership basis")
    show.add_argument("group_id")
    args = parser.parse_args()
    if args.command == "build":
        write_groups(build_groups(load_documents()))
        print(summary())
    elif args.command == "status":
        print(summary())
    else:
        with duckdb.connect(str(GROUPS_DB_PATH), read_only=True) as conn:
            rows = conn.execute("""
                SELECT ordinal, listing_date, listing_title, listing_dockets, caption_dockets,
                       basis, flags, pages FROM group_documents WHERE group_id = ? ORDER BY ordinal
            """, [args.group_id]).fetchall()
        if not rows:
            raise SystemExit(f"Unknown group: {args.group_id}")
        for row in rows:
            print(json.dumps(dict(zip(("doc", "posted", "title", "listing", "caption",
                                       "basis", "flags", "pages"), row)), ensure_ascii=False))


if __name__ == "__main__":
    main()
