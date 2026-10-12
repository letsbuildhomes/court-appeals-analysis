"""Choose one text per page: native where usable, OCR where it is clearly better.

    native_ok                     native text
    blank                         no text
    OCR missing or weak           unresolved (fewer than 10 characters or 2 words,
                                  confidence under 45, or garbled)
    native missing or garbled     OCR
    OCR finds much more text      OCR
    OCR confirms native text      native
    otherwise                     unresolved

Output: the pages contract.
"""
import re

from court.core.step import Failure, Step, is_failure


def token_coverage(needle, haystack):
    ocr_words = re.findall(r'[a-z0-9]{2,}', needle.casefold())
    native_words = set(re.findall(r'[a-z0-9]{2,}', haystack.casefold()))
    return sum(word in native_words for word in ocr_words) / len(ocr_words) if ocr_words else 0.0


def choose(decision, native, ocr):
    if decision == 'native_ok':
        return 'usable', 'native', 'triage_native_ok'
    if decision == 'blank':
        return 'blank', None, 'triage_blank'
    if decision not in ('ocr_needed', 'ocr_candidate'):
        return 'unresolved', None, 'unknown_triage_decision'
    if ocr is None or ocr['status'] != 'complete':
        return 'unresolved', None, 'ocr_missing_or_failed'
    if (not ocr['text'] or (ocr['alnum'] or 0) < 10 or (ocr['words'] or 0) < 2 or ocr['mean_confidence'] is None
            or ocr['mean_confidence'] < 45 or (ocr['bad'] or 0) > max(5, (ocr['alnum'] or 0) // 100)):
        return 'unresolved', None, 'ocr_quality_insufficient'
    if native['alnum'] < 20 or native['bad'] > max(5, native['alnum'] // 100):
        return 'usable', 'ocr', 'native_missing_or_garbled'
    if (ocr['alnum'] or 0) >= native['alnum'] + max(40, native['alnum'] // 2):
        return 'usable', 'ocr', 'ocr_recovers_more_text'
    if (ocr['alnum'] or 0) <= native['alnum'] * 1.25 and token_coverage(ocr['text'], native['text']) >= 0.75:
        return 'usable', 'native', 'ocr_confirms_native_text'
    return 'unresolved', None, 'native_ocr_disagree'


class PreferNative(Step):
    description = 'Chooses native text where it is usable and OCR text where it is clearly better; otherwise unresolved.'
    inputs = {'native': None, 'usability': None, 'ocr': None}
    output = 'pages'

    def run(self, inputs, ctx):
        output = {}
        for key, native in inputs['native'].items():
            usability = inputs['usability'].get(key)
            if is_failure(native) or usability is None or is_failure(usability):
                output[key] = native if is_failure(native) else usability or Failure('select', 'no usability', ctx.path)
                continue
            ocr = inputs['ocr'].get(key)
            status, source, reason = choose(usability['decision'], native, ocr)
            text = native['text'] if source == 'native' else ocr['text'] if source == 'ocr' else None
            output[key] = {'doc': native['doc'], 'page': native['page'], 'status': status, 'source': source,
                           'reason': reason, 'text': text,
                           'ocr_confidence': ocr['mean_confidence'] if ocr and ocr['status'] == 'complete' else None}
        return output
