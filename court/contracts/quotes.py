"""Matching a quoted passage against page text, the rule every citation is checked by."""
import difflib
import html
import re

PUNCTUATION = str.maketrans({"‐": "-", "‑": "-", "‘": "'", "’": "'", "‚": "'", "‛": "'",
                             "′": "'", "“": '"', "”": '"', "„": '"',
                             "″": '"', "–": "-", "—": "-", "−": "-",
                             " ": " ", "­": None})
ELLIPSIS_RE = re.compile(r"\s*(?:\.\s?\.\s?\.|…)\s*")


def comparable(text):
    """Casefolded text with typographic quotes, dashes, and spacing made plain."""
    plain = " ".join(html.unescape(text).translate(PUNCTUATION).split())
    return re.sub(r"(\w)- (\w)", r"\1-\2", plain).casefold()  # hyphen split at a line break


def quote_on_page(quote, page, following=""):
    """Whether a quote is on a page (or runs onto the next one), tolerating
    elisions, footnote markers, and old encodings of quotation marks."""
    text = comparable(page + " " + following)
    q = comparable(quote)
    if q in text:
        return True
    pieces = [piece for piece in ELLIPSIS_RE.split(q) if len(piece) >= 8]
    if len(pieces) > 1:
        position = 0
        for piece in pieces:
            found = text.find(piece, position)
            if found < 0:
                break
            position = found + len(piece)
        else:
            return True
    # Near-exact: around the longest shared run, 90% of the quote's characters
    # align with one passage no more than 1.4 times the quote's length.
    if len(q) >= 20:
        anchor = difflib.SequenceMatcher(None, q, text, autojunk=False).find_longest_match(0, len(q), 0, len(text))
        if anchor.size >= 12:
            start = max(0, anchor.b - anchor.a - 10)
            window = text[start:start + int(len(q) * 1.4) + 20]
            matcher = difflib.SequenceMatcher(None, q, window, autojunk=False)
            if sum(block.size for block in matcher.get_matching_blocks()) >= 0.9 * len(q):
                return True
    return False
