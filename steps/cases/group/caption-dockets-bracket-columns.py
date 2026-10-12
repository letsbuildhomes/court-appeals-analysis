"""Group documents into cases by the docket numbers in their captions, reading through bracket columns.

The same rules as ``cases/group:caption-dockets``, except that a docket number
may be separated from its court suffix by a caption's bracket column. Older
decisions set the parties and the docket number side by side with a column
of ``}`` between them, so the text reads::

    Appeals of Wesco, Inc      } Docket Nos. 209-12-97
                               } Vtec, 175-10-01 ...

and the earlier rule, which allows only spaces before "Vtec", missed the docket.

Rules, in order:

1. **Caption dockets.** A document's dockets are the numbers right after a
   "Docket No." or "Case No." label in the first 3,000 characters of page 1.
   Spaces, line breaks and ``}`` or ``|`` columns may separate a number from
   "Vtec". Unlabeled citations ("In re X, No. 1-2-11 Vtec") are ignored.
2. **Listing fallback.** A document with no caption dockets uses the dockets in
   its court listing entries. The basis records how caption and listing agree:
   agree, caption_extra, caption_fewer, caption_only, disagree, listing_only, none.
3. **Joining.** Documents sharing a key docket form one group; a caption that
   lists several dockets joins their groups. A document without dockets stands
   alone.
4. **Flags, never merges.** A caption docket one digit away from a listing
   docket (``typo_like``) and a docket filed after the document was posted
   (``filed_after_posting``) are flagged for review.

A group's ID is ``g-`` plus its earliest-filed docket, normalized; its
documents are ordered by posting date, then hash.

Output: group ID → {group_id, dockets, composition, documents}, each document
with its sha256, title, posted, listing and caption dockets, key dockets,
basis, flags, page count and character count.
"""
import collections
from datetime import date
import re

from court.contracts import pages as page_contract
from court.core.step import Step

CAPTION_WINDOW = 3000
DOCKET_RE = re.compile(
    r"\b(?:(\d{1,3})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{2})[\s}|]*-?[\s}|]*V\s*[a-z]{2,3}\b"
    r"|(\d{2})\s*-?\s*(ENV|CC|EC|AP)\s*-?\s*(\d{2,6})\b"
    r"|eo\s*(\d{2})-(\d{2,4})\b)", re.I)
LABEL_RE = re.compile(r"(?:Docket|Case)\s+Nos?\.?\s*:?", re.I)


def normalize_match(m):
    if m[1]:
        return f"{int(m[1])}-{int(m[2])}-{m[3]} Vtec"
    if m[4]:
        return f"{m[4]}-{m[5].upper()}-{int(m[6]):05d}"
    return f"eo{m[7]}-{int(m[8]):03d}"


def normalize_dockets(text):
    """All docket-shaped strings in text, normalized, in first-seen order."""
    seen = {}
    for m in DOCKET_RE.finditer(text or ""):
        seen.setdefault(normalize_match(m))
    return list(seen)


def caption_dockets(page_one):
    """Rule 1: dockets immediately following a "Docket No." or "Case No." label on page 1."""
    text = (page_one or "")[:CAPTION_WINDOW]
    seen = {}
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


def classify(listing, caption):
    """Rule 2: membership basis and the dockets used as the grouping key."""
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


def flags(listing, caption, key, posted):
    """Rule 4: near misses worth a person's review."""
    found = []
    if set(listing) != set(caption) and listing and caption:
        # Caption order, not set order, so the flags are the same on every run.
        for c in [c for c in caption if c not in listing]:
            if any(one_component_apart(c, l) for l in listing if l not in caption):
                found.append(f"typo_like:{c}")
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
    """One document's grouping facts, from its listing entries and page text."""
    title, posted, raw, listed = None, None, [], []
    for i, entry in enumerate(document['listings']):
        if i == 0:
            title, posted = entry['title'], entry['date_attribute']
        elif entry['date_attribute'] and (posted is None or entry['date_attribute'] < posted):
            posted = entry['date_attribute']
        if entry['docket_raw']:
            raw.append(entry['docket_raw'])
        for docket in normalize_dockets(entry['docket_raw']):
            if docket not in listed:
                listed.append(docket)
    caption = caption_dockets(page_texts[0])
    basis, key = classify(listed, caption)
    return {'sha256': document['sha256'], 'title': title, 'posted': posted, 'listing_dockets': listed,
            'caption_dockets': caption, 'key_dockets': key, 'basis': basis,
            'flags': flags(listed, caption, key, posted), 'pages': len(page_texts),
            'chars': len("\n".join(page_texts))}


def join(documents):
    """Rule 3: union-find over key dockets; a document without dockets stands alone."""
    parent = {}

    def find(x):
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
    members = collections.defaultdict(list)
    for doc in documents:
        root = find(doc["key_dockets"][0]) if doc["key_dockets"] else f"pdf:{doc['sha256']}"
        members[root].append(doc)
    groups = {}
    for docs in members.values():
        docs.sort(key=lambda d: (d["posted"] is None, d["posted"] or "", d["sha256"]))
        dockets = sorted({d for doc in docs for d in doc["key_dockets"]}, key=lambda d: (filing_period(d) or (0, 0), d))
        group_id = ("g-" + re.sub(r"[^a-z0-9]+", "-", dockets[0].lower()).strip("-")
                    if dockets else f"g-pdf-{docs[0]['sha256'][:12]}")
        key_sets = {tuple(sorted(doc["key_dockets"])) for doc in docs}
        composition = "single" if len(docs) == 1 else "uniform" if len(key_sets) == 1 else "linked"
        if group_id in groups:
            raise ValueError(f"Duplicate group ID {group_id}")
        groups[group_id] = {'group_id': group_id, 'dockets': dockets, 'composition': composition, 'documents': docs}
    return groups


class CaptionDocketGroups(Step):
    description = ('Groups documents into cases by the docket numbers in their page-1 captions, reading through '
                   'the bracket columns of older captions, falling back to the court listing; flags near misses for '
                   'review without merging them.')
    inputs = {'documents': 'documents', 'pages': 'pages'}
    output = None

    def run(self, inputs, ctx):
        texts = {doc: [p['text'] or '' for p in values]
                 for doc, values in page_contract.by_document(inputs['pages']).items()}
        described = [describe(document, texts[sha]) for sha, document in sorted(inputs['documents'].items())
                     if sha in texts]
        return join(described)
