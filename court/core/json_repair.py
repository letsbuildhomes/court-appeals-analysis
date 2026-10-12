"""Deterministic repair of common bracket slips in model JSON output.

Models occasionally close or omit a bracket in long nested JSON. This repairs
only structural punctuation outside strings; it never edits string contents.
Each repair is described so the change stays auditable.
"""

from __future__ import annotations

import json
import re

FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)
CLOSER = {"{": "}", "[": "]"}


def _string_end(text: str, start: int) -> int:
    """Index just after the JSON string starting at text[start] == '"'."""
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return len(text)


def _close_before_comma(out: list[str], closers: list[str]) -> None:
    """Insert closers before a comma already emitted, keeping the comma after them."""
    while out and out[-1].isspace():
        out.pop()
    comma = bool(out) and out[-1] == ","
    if comma:
        out.pop()
    out.extend(closers)
    if comma:
        out.append(",")


def repair_json(raw: str, root_keys: frozenset[str] = frozenset()) -> tuple[object | None, list[str]]:
    """Parse raw JSON, repairing bracket slips if needed.

    `root_keys` names keys that only occur in the top-level object; one found
    deeper means the model forgot to close its enclosing containers.
    Returns (value, notes). Notes is empty when the input parsed unchanged, and
    value is None when even the repaired text does not parse.
    """
    try:
        return json.loads(raw), []
    except json.JSONDecodeError:
        pass
    notes: list[str] = []
    text = FENCE_RE.sub("", raw).strip()
    if text != raw.strip():
        notes.append("removed code fence")
    out: list[str] = []
    stack: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            end = _string_end(text, i)
            token = text[i:end]
            is_key = text[end:].lstrip().startswith(":")
            if is_key and token[1:-1] in root_keys and len(stack) > 1:
                closers = [CLOSER[stack.pop()] for _ in range(len(stack) - 1)]
                _close_before_comma(out, closers)
                notes.append(f"closed {''.join(closers)} before top-level key {token}")
            elif is_key and stack and stack[-1] == "[":
                # A "key": inside an array means the array was never closed.
                closers = []
                while stack and stack[-1] == "[":
                    closers.append(CLOSER[stack.pop()])
                _close_before_comma(out, closers)
                notes.append(f"closed an unclosed array before key {token[:40]}")
            out.append(token)
            i = end
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            expected = CLOSER[stack[-1]] if stack else None
            if expected is None:
                notes.append(f"dropped an extra '{ch}'")
                i += 1
                continue
            if ch != expected:
                # Close inner containers until this closer matches, if it can.
                if CLOSER.get("{" if ch == "}" else "[") and ("{" if ch == "}" else "[") in stack:
                    while stack and CLOSER[stack[-1]] != ch:
                        out.append(CLOSER[stack.pop()])
                        notes.append(f"inserted '{out[-1]}' before '{ch}'")
                else:
                    notes.append(f"dropped a mismatched '{ch}'")
                    i += 1
                    continue
            # A trailing comma before a closer is invalid JSON.
            j = len(out) - 1
            while j >= 0 and out[j].isspace():
                j -= 1
            if j >= 0 and out[j] == ",":
                del out[j]
                notes.append("removed a trailing comma")
            stack.pop()
        out.append(ch)
        i += 1
    while stack:
        out.append(CLOSER[stack.pop()])
        notes.append(f"appended missing '{out[-1]}' at end")
    repaired = "".join(out)
    try:
        return json.loads(repaired), notes or ["reformatted"]
    except json.JSONDecodeError as exc:
        return None, notes + [f"still invalid after repair: {exc.msg} at {exc.pos}"]
