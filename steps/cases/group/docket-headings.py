"""One case per docket, reading only true headings: ``docket-cases`` without citations that wrap.

``cases/group:docket-cases`` took a label at the start of a line as a caption
label. Body text wraps, though, so a citation such as "Appeal of Doyle,
/ Docket No. 100-5-02 Vtec (Vt. Envtl. Ct., Jan. 21, 2003)" can start a line,
and the Leikert decision joined the Doyle and Lussier & Noe cases that way.
Here a label is a heading only when, besides starting a column, the line it
starts doesn't continue as a sentence after the dockets, and, for a label at
the left margin, the line before doesn't end mid-sentence with a comma. A
parenthetical note between stacked heading dockets ("(from 4/27/04 ZBA
decision)") no longer ends the column. Everything else is unchanged.


Earlier versions joined documents into groups through shared dockets, and a
docket number anywhere in the first 3,000 characters of page 1 counted, so a
decision citing an earlier case ("Appeal of Doyle, Docket No. 100-5-02 Vtec
(Vt. Envtl. Ct. 2003)") joined that case's group, and groups chained. Here
nothing is joined. A case is one docket; a decision the court issued in
several dockets belongs to each of their cases; the body text is never read.

Rules, in order:

1. **Heading dockets.** The dockets after a "Docket No." or "Case No." label
   in the first 3,000 characters of page 1, where the label starts a column
   of the caption: at the start of a line, after a bracket column (``}`` or
   ``|``) or after a wide gap. A label inside a sentence is a citation or a
   mention and is skipped, as is "Supreme Court Docket No.". Dockets continue
   on the following lines in the label's column, so a stacked list of numbers
   is read whole while the parties' names in the left column are ignored.
   Dash variants, line breaks and bracket columns between a number and
   "Vtec", footnote markers ("Vtec1") and OCR's letter O in "22-ENV-OOO3O"
   are read as the plain docket.
2. **Listing fallback.** A document whose heading names no docket uses its
   court listing's dockets; one with neither stands alone. A listing docket
   the heading doesn't name never makes a case: the listing has typos and
   names Supreme Court numbers (``eo07-113``, ``22-AP-00296``), which never
   make cases. It is flagged instead.
3. **Cases.** Each docket is a case of every document naming it. A document
   without dockets is a case of its own (``g-pdf-`` and its hash).
4. **Flags, never changes.** ``typo_like:X`` and ``filed_after_posting:X``
   are flags, as before, and appear in the case text. The new checks,
   ``listing_only:X`` (a listing docket the heading doesn't name),
   ``no_heading_docket`` and ``no_docket``, are kept in ``checks``, outside the
   case text, so a case whose documents and dockets are unchanged keeps
   exactly its earlier text and its saved model answers.

A case's ID is ``g-`` plus its docket, normalized; its documents are ordered
by posting date, then hash. Its composition is ``single`` (one document),
``uniform`` (every document names only this docket) or ``shared`` (some
document also belongs to other dockets' cases, which extraction must be told).

Output: case ID → {group_id, dockets, composition, documents}, each document
with its sha256, title, posted, listing and caption (heading) dockets, key
dockets, basis, flags, checks, page count and character count.
"""
from datetime import date
import re

from court.contracts import pages as page_contract
from court.core.step import Step

CAPTION_WINDOW = 3000
# Lines after a label that may continue its column of docket numbers.
CONTINUATION_LINES = 4
DOCKET_RE = re.compile(
    r"\b(?:(\d{1,3})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{2})[\s}|]*-?[\s}|]*V\s*[a-z]{2,3}\b"
    r"|(\d{2})\s*-?\s*(ENV|CC|EC|AP)\s*-?\s*(\d{2,6})\b"
    r"|eo\s*(\d{2})-(\d{2,4})\b)", re.I)
LABEL_RE = re.compile(r"(?:Docket|Case)\s+Nos?\.?\s*:?", re.I)
DASHES = re.compile('[‐-―−­﹣－]')
# Text before a label on its line that leaves the label starting a caption column.
CELL_BREAK = re.compile(r"[}|{]| {3,}|\t")


def normalize_match(m):
    if m[1]:
        return f"{int(m[1])}-{int(m[2])}-{m[3]} Vtec"
    if m[4]:
        return f"{m[4]}-{m[5].upper()}-{int(m[6]):05d}"
    return f"eo{m[7]}-{int(m[8]):03d}"


def clean(text):
    """Text with dash variants as hyphens, non-breaking spaces as spaces and OCR damage in numbers undone."""
    text = DASHES.sub('-', (text or '').replace(' ', ' '))
    # A footnote marker or a repeated number run into "Vtec" ("Vtec1", "Vtec147-10-13").
    text = re.sub(r"(V\s*tec)(?=\d)", r"\1 ", text, flags=re.I)
    # The letter O read for a zero in the numeric part of a docket ("22-ENV-OOO3O").
    return re.sub(r"\b(\d{2}\s*-?\s*(?:ENV|CC|EC|AP)\s*-?\s*)([0-9Oo]{5})\b",
                  lambda m: m[1]+m[2].replace('O', '0').replace('o', '0'), text, flags=re.I)


def normalize_dockets(text):
    """All docket-shaped strings in text, normalized, in first-seen order."""
    seen = {}
    for m in DOCKET_RE.finditer(clean(text)):
        seen.setdefault(normalize_match(m))
    return list(seen)


def column(lines, index, label):
    """The label's line after the label, and the next lines from the label's column on.

    The column ends at a line with a label of its own, which is read (or skipped) as its own heading.
    """
    parts = [lines[index][label.end():]]
    for line in lines[index+1:index+1+CONTINUATION_LINES]:
        if LABEL_RE.search(line):
            break
        parts.append(line[max(0, label.start()-2):])
    return '\n'.join(parts)


def read_after_label(segment):
    """Dockets at the start of a segment, continuing over separators and stopping at other words, and
    where the reading ended."""
    found, position = [], 0
    for m in DOCKET_RE.finditer(segment):
        gap = segment[position:m.start()]
        if position == 0 and gap.strip(" \t\r\n|{}:"):
            break
        # A parenthetical note between stacked dockets ("(from 4/27/04 ZBA decision)") belongs to the caption.
        if position and re.search(r"[A-Za-z]{3,}", re.sub(r"Docket|Nos?\b|and|\([^()]*\)", "", gap)):
            break
        found.append(normalize_match(m))
        position = m.end()
    return found, position


def continues_as_sentence(segment, position, previous, margin):
    """Whether a label's line reads as body text: words after its dockets in the same column, or, for a
    label at the left margin, a previous line ending with a comma, as when a citation wraps onto a new line."""
    line_end = segment.find('\n')
    rest = segment[position:line_end if line_end >= 0 else None] if position <= (line_end if line_end >= 0
                                                                                   else len(segment)) else ''
    rest = re.split(r" {3,}|\t|[}|]", rest)[0]
    if re.search(r"[A-Za-z]{3,}", re.sub(r"\bV\s*tec\b|\band\b|\bNos?\b", "", rest, flags=re.I)):
        return True
    return margin and previous.rstrip().endswith(',')


def heading_dockets(page_one):
    """Rule 1: dockets after a caption label that starts a column, with its continuation lines."""
    lines = clean((page_one or '')[:CAPTION_WINDOW]).splitlines()
    seen = {}
    for index, line in enumerate(lines):
        for label in LABEL_RE.finditer(line):
            before = CELL_BREAK.split(line[:label.start()])[-1]
            if before.strip():
                continue
            segment = column(lines, index, label)
            dockets, position = read_after_label(segment)
            previous = next((line for line in reversed(lines[:index]) if line.strip()), '')
            margin = not line[:label.start()].strip() and label.start() < 8
            if dockets and continues_as_sentence(segment, position, previous, margin):
                continue
            for docket in dockets:
                if trial_court(docket):
                    seen.setdefault(docket)
    return list(seen)


def trial_court(docket):
    """Whether a docket is the Environmental Court's own, not a Supreme Court number."""
    return not docket.startswith('eo') and '-AP-' not in docket


def classify(listing, heading):
    """How heading and listing agree: agree, caption_extra, caption_fewer, caption_only, disagree,
    listing_only or none."""
    ls, hs = set(listing), set(heading)
    if hs and ls:
        if hs == ls:
            return "agree"
        if ls < hs:
            return "caption_extra"
        if hs < ls:
            return "caption_fewer"
        return "disagree"
    if hs:
        return "caption_only"
    return "listing_only" if ls else "none"


def filing_period(docket):
    """(year, month) encoded by a docket number, when the format encodes one."""
    if m := re.fullmatch(r"\d+-(\d+)-(\d\d) Vtec", docket):
        year = int(m[2])
        return (2000 + year if year < 70 else 1900 + year, int(m[1]))
    if m := re.fullmatch(r"(\d\d)-[A-Z]+-\d+", docket):
        return (2000 + int(m[1]), 1)
    return None


def one_component_apart(a, b):
    na, nb = re.findall(r"\d+", a), re.findall(r"\d+", b)
    same_format = re.sub(r"\d+", "#", a) == re.sub(r"\d+", "#", b)
    return same_format and len(na) == len(nb) and sum(x != y for x, y in zip(na, nb)) == 1


def checks(listing, heading):
    """Rule 4: how the heading and listing left the document's dockets; never changes a case."""
    trial = [d for d in listing if trial_court(d)]
    if heading:
        return [f"listing_only:{d}" for d in trial if d not in heading]
    return ["no_heading_docket" if trial else "no_docket"]


def flags(listing, heading, key, posted):
    """Rule 4: near misses worth a person's review; never changes a case."""
    found = []
    trial = [d for d in listing if trial_court(d)]
    if heading:
        # Heading order, not set order, so the flags are the same on every run.
        for d in heading:
            if d not in listing and any(one_component_apart(d, l) for l in trial if l not in heading):
                found.append(f"typo_like:{d}")
    if posted:
        try:
            posted_on = date.fromisoformat(posted[:10])
        except ValueError:
            posted_on = None
        for docket in key:
            period = filing_period(docket)
            if posted_on and period and period > (posted_on.year, posted_on.month):
                found.append(f"filed_after_posting:{docket}")
    return found


def describe(document, page_texts):
    """One document's dockets and how they were found, from its listing entries and page 1."""
    title, posted, listed = None, None, []
    for i, entry in enumerate(document['listings']):
        if i == 0:
            title, posted = entry['title'], entry['date_attribute']
        elif entry['date_attribute'] and (posted is None or entry['date_attribute'] < posted):
            posted = entry['date_attribute']
        for docket in normalize_dockets(entry['docket_raw']):
            if docket not in listed:
                listed.append(docket)
    heading = heading_dockets(page_texts[0])
    key = heading or [d for d in listed if trial_court(d)]
    return {'sha256': document['sha256'], 'title': title, 'posted': posted, 'listing_dockets': listed,
            'caption_dockets': heading, 'key_dockets': key, 'basis': classify(listed, heading),
            'flags': flags(listed, heading, key, posted), 'checks': checks(listed, heading), 'pages': len(page_texts),
            'chars': len("\n".join(page_texts))}


def case_id(docket):
    return "g-" + re.sub(r"[^a-z0-9]+", "-", docket.lower()).strip("-")


def cases(documents):
    """Rule 3: one case per docket, holding every document that names it."""
    members = {}
    for doc in documents:
        for key in doc["key_dockets"] or [f"pdf:{doc['sha256']}"]:
            members.setdefault(key, []).append(doc)
    built = {}
    for key, docs in members.items():
        docs.sort(key=lambda d: (d["posted"] is None, d["posted"] or "", d["sha256"]))
        alone = key.startswith("pdf:")
        group_id = f"g-pdf-{key[4:16]}" if alone else case_id(key)
        composition = ("single" if len(docs) == 1 and len(docs[0]["key_dockets"]) <= 1
                       else "uniform" if all(d["key_dockets"] == [key] for d in docs) else "shared")
        if group_id in built:
            raise ValueError(f"Duplicate case ID {group_id}")
        built[group_id] = {'group_id': group_id, 'dockets': [] if alone else [key], 'composition': composition,
                           'documents': docs}
    return built


class DocketHeadings(Step):
    description = ('Makes one case per docket. A document belongs to every docket its page-1 heading names, '
                   'falling back to the court listing when the heading names none; nothing is merged and the '
                   'body text is never read. Listing dockets the heading doesn\'t name are flagged, not made into '
                   'cases.')
    inputs = {'documents': 'documents', 'pages': 'pages'}
    output = None

    def run(self, inputs, ctx):
        texts = {doc: [p['text'] or '' for p in values]
                 for doc, values in page_contract.by_document(inputs['pages']).items()}
        described = [describe(document, texts[sha]) for sha, document in sorted(inputs['documents'].items())
                     if sha in texts]
        return cases(described)
