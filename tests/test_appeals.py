"""Legacy evidence rules, extraction recovery, and the legacy run exporter and viewer; no network."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from appeal_evidence import apply_review, interval
from appeal_runtime import save
from appeal_viewer import run_folder
from appeals import export
import group_scout as gs
from pipeline.legacy.extract import normalize_extract


def extract_fixture():
    return {'schema_version': 'v6', 'dockets': [{
        'docket': 'A', 'related_dockets': [],
        'project': {'description': None, 'municipality': None, 'proposed_units': None, 'proposed_lots': None, 'evidence': []},
        'municipal_decision': {'description': None, 'date': None, 'outcome': 'unclear', 'evidence': []},
        'appellants': [],
        'discretion': {'description': None, 'evidence': [], 'involved': 'unclear', 'central': 'unclear', 'central_evidence': []},
        'timeline': [],
        'observed_status': {'litigation': 'unclear', 'permission': 'unclear', 'description': None, 'evidence': []},
        'restrictions': [], 'housing_changes': []}]}


def recover(value, pages):
    """What extraction does with an answer that fails validation: relocate, then validate again."""
    normalized, notes = normalize_extract(value)
    candidate, errors = gs.validate_extract(json.dumps(normalized), pages, version='v6')
    if candidate is None:
        raise ValueError(str(errors))
    return normalized, notes


class LegacyRuleTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_review_validation_does_not_mutate_raw_answer_on_failure(self):
        review = {'schema_version': 'v6-review-1', 'scope': {'verdict': 'agree'}, 'flags': [],
                  'changes': [{'docket': 'A', 'field': 'project', 'severity': 'minor',
                               'evidence': [{'doc': 1, 'page': 1, 'quote': 'A source fact'}], 'replacement': {}}]}
        before = copy.deepcopy(review)
        with self.assertRaisesRegex(ValueError, 'citation corrected'):
            apply_review(extract_fixture(), review, {1: {1: 'Other text', 2: 'A source fact'}})
        self.assertEqual(review, before)

    def test_recovery_relocates_misplaced_fields(self):
        malformed = extract_fixture()
        docket = malformed['dockets'][0]
        for field in ('timeline', 'observed_status', 'restrictions', 'housing_changes'):
            malformed[field] = docket.pop(field)
        docket['central'] = docket['discretion'].pop('central')
        normalized, notes = recover(malformed, {})
        self.assertEqual(normalized, extract_fixture())
        self.assertEqual(len(notes), 5)

    def test_relocation_refuses_ambiguous_or_conflicting_ownership(self):
        value = extract_fixture()
        value['housing_changes'] = [{'description': 'Root observation', 'evidence': []}]
        normalized, notes = normalize_extract(value)
        self.assertEqual(normalized, value)
        self.assertFalse(notes)
        with self.assertRaises(ValueError):
            recover(value, {})
        value['dockets'][0].pop('housing_changes')
        value['dockets'].append(copy.deepcopy(value['dockets'][0]))
        self.assertEqual(normalize_extract(value), (value, []))
        with self.assertRaises(ValueError):
            recover(value, {})

    def test_relocation_does_not_invent_evidence_or_enum_values(self):
        for damage in ('evidence', 'kind'):
            value = extract_fixture()
            if damage == 'evidence':
                value['dockets'][0]['observed_status'].pop('evidence')
            else:
                value['dockets'][0]['timeline'] = [{'kind': 'unsupported'}]
            with self.assertRaises(ValueError):
                recover(value, {})

    def test_partial_drafts_are_not_exported_and_feedback_removed(self):
        save(self.root/'manifest.json', {'name': 'test', 'groups': [{'group_id': 'a', 'documents': []},
                                                                  {'group_id': 'b', 'documents': []}]})
        draft = {'dockets': [{'docket': 'A', 'scouting': {'suggestions': ['x']}}]}
        result = {'status': 'duration_failed', 'candidate': draft, 'triage': {'housing': 'yes', 'municipal_appeal': 'yes'}}
        save(self.root/'results/a.json', result)
        result.update(status='complete', duration_bounds=[{'docket': 'A', 'start': None, 'end': None, 'note': 'Unknown',
                                                           'duration': {'kind': 'unknown', 'days': None}}])
        save(self.root/'results/b.json', result)
        summary = export(self.root)
        self.assertEqual(summary['records'], 1)
        row = json.loads((self.root/'export/records.jsonl').read_text())
        self.assertEqual(row['group'], 'b')
        self.assertNotIn('scouting', row['record'])
        self.assertEqual(row['record']['duration']['kind'], 'unknown')

    def test_viewer_disallows_path_traversal(self):
        for name in ['../secrets', 'a/b', '/tmp', '']:
            with self.assertRaises(ValueError):
                run_folder(self.root, name)

    def test_citation_correction_provenance_does_not_break_review(self):
        old = {'dockets': [{'docket': 'A', 'project': {'evidence': [{'doc': 1, 'page': 2, 'quote': 'Fact',
                                                                     'cited_as': {'doc': 1, 'page': 1}}]}}]}
        review = {'schema_version': 'v6-review-1', 'scope': {'verdict': 'agree'}, 'changes': [], 'flags': []}

        def validate(raw, *args, **kwargs):
            value = json.loads(raw)
            self.assertNotIn('cited_as', value['dockets'][0]['project']['evidence'][0])
            return value, []
        with patch('appeal_evidence.gs.validate_extract', side_effect=validate):
            candidate, warnings = apply_review(old, review, {1: {2: 'Fact'}})
        self.assertIn('cited_as', old['dockets'][0]['project']['evidence'][0])
        self.assertFalse(warnings)

    def test_bounds_are_conservative_and_do_not_mutate_input(self):
        pages = {1: {1: 'A real event'}}

        def point(day, relation, quote='A real event'):
            return {'date': day, 'relation': relation, 'description': 'Event', 'evidence': [{'doc': 1, 'page': 1, 'quote': quote}]}
        record = {'start': point('2020-01-01', 'on_or_before'), 'end': point('2020-01-11', 'exact')}
        before = copy.deepcopy(record)
        value, warnings = interval(record, pages)
        self.assertEqual(value['duration'], {'kind': 'at_least', 'days': 10})
        self.assertEqual(record, before)
        self.assertFalse(warnings)
        record['start']['relation'] = 'exact'
        self.assertEqual(interval(record, pages)[0]['duration']['kind'], 'exact')
        record['start'] = point('2020-01-11', 'on_or_before')
        self.assertEqual(interval(record, pages)[0]['duration'], {'kind': 'unknown', 'days': None})
        record['start'] = point('2020-02-01', 'exact')
        self.assertIsNone(interval(record, pages)[0]['duration']['days'])
        record['start'] = point('2020-01-01', 'exact', 'Invented')
        self.assertIsNone(interval(record, pages)[0]['start'])


if __name__ == '__main__':
    unittest.main()
