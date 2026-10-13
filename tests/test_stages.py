"""The stages' rules: grouping, case text, page usability, OCR parsing and text selection."""
import unittest

from court.config import ROOT
from court.core.library import Library

LIBRARY = Library()
GROUP = LIBRARY.module('cases/group:caption-dockets')
RENDER = LIBRARY.module('cases/render:page-tagged')
USABILITY = LIBRARY.module('text/usability:ink-check')
OCR = LIBRARY.module('text/ocr:tesseract')
SELECT = LIBRARY.module('text/select:prefer-native')
DOCKETS = LIBRARY.module('cases/group:docket-cases')
HEADINGS = LIBRARY.module('cases/group:docket-headings')
TRIAGE = LIBRARY.module('triage:docket-target')
DRAFT = LIBRARY.module('draft:docket-target')


def doc(sha, listing, caption, posted='2020-05-01T12:00:00Z'):
    basis, key = GROUP.classify(listing, caption)
    return {'sha256': sha, 'title': sha, 'posted': posted, 'listing_dockets': listing, 'caption_dockets': caption,
            'key_dockets': key, 'basis': basis, 'flags': GROUP.flags(listing, caption, key, posted),
            'pages': 1, 'chars': 100}


class GroupingTests(unittest.TestCase):
    def test_normalizes_docket_formats(self):
        self.assertEqual(GROUP.normalize_dockets('154-07-07 Vtec; 22ENV00012 & 23-ENV-000131 eo10-70 25-AP-060 99-7-15Vfec'),
                         ['154-7-07 Vtec', '22-ENV-00012', '23-ENV-00131', 'eo10-070', '25-AP-00060', '99-7-15 Vtec'])

    def test_caption_ignores_citations_and_keeps_continuation_lines(self):
        page = ('VERMONT SUPERIOR COURT\nEnvironmental Division      Docket No. 25-ENV-00071\n'
                '32 Cherry St, 2nd Floor,       Docket No. 25-ENV-00078\nBurlington\n'
                'In re Saman ROW Approval, No. 176-10-10 Vtec, slip op. at 1 (Vt. Super. Ct.)')
        self.assertEqual(GROUP.caption_dockets(page), ['25-ENV-00071', '25-ENV-00078'])
        stacked = 'Docket Nos. 3-1-19 Vtec\n            4-1-19 Vtec\n  In re Something Appeal'
        self.assertEqual(GROUP.caption_dockets(stacked), ['3-1-19 Vtec', '4-1-19 Vtec'])
        self.assertEqual(GROUP.caption_dockets('See No. 1-1-11 Vtec, slip op.'), [])

    def test_classify_and_flags_in_a_stable_order(self):
        self.assertEqual(GROUP.classify(['1-1-20 Vtec'], ['1-1-20 Vtec']), ('agree', ['1-1-20 Vtec']))
        self.assertEqual(GROUP.classify(['1-1-20 Vtec'], ['1-1-20 Vtec', '2-1-20 Vtec'])[0], 'caption_extra')
        self.assertEqual(GROUP.classify([], ['2-1-20 Vtec'])[0], 'caption_only')
        self.assertEqual(GROUP.classify(['1-1-20 Vtec'], []), ('listing_only', ['1-1-20 Vtec']))
        mistyped = doc('a', ['169-12-17 Vtec'], ['169-12-16 Vtec'], '2017-03-17T12:00:00Z')
        self.assertEqual(mistyped['basis'], 'disagree')
        self.assertIn('typo_like:169-12-16 Vtec', mistyped['flags'])
        self.assertIn('filed_after_posting:169-12-17 Vtec',
                      doc('b', ['169-12-17 Vtec'], [], '2017-03-17T12:00:00Z')['flags'])
        # Several near misses are flagged in caption order, never set order.
        for _ in range(5):
            self.assertEqual(GROUP.flags(['2-6-09 Vtec'], ['101-6-09 Vtec', '102-6-09 Vtec', '6-1-11 Vtec'], [], None),
                             ['typo_like:101-6-09 Vtec', 'typo_like:102-6-09 Vtec'])

    def test_groups_join_only_through_key_dockets(self):
        docs = [doc('a', ['1-1-20 Vtec'], ['1-1-20 Vtec'], '2020-02-01T12:00:00Z'),
                doc('b', ['2-1-20 Vtec'], ['1-1-20 Vtec', '2-1-20 Vtec'], '2020-03-01T12:00:00Z'),
                doc('c', ['2-1-20 Vtec'], ['2-1-20 Vtec'], '2020-01-01T12:00:00Z'),
                doc('d', ['1-1-20 Vtec'], ['9-9-20 Vtec']),  # The caption decides over the listing.
                doc('e', [], [])]
        built = GROUP.join(docs)
        linked = built['g-1-1-20-vtec']
        self.assertEqual([d['sha256'] for d in linked['documents']], ['c', 'a', 'b'])
        self.assertEqual(linked['composition'], 'linked')
        self.assertEqual(built['g-9-9-20-vtec']['composition'], 'single')
        self.assertIn('g-pdf-e', built)

    def test_headings_name_dockets_and_body_mentions_dont(self):
        bracket = ('In re: Tenney Notices of Violation         }       Docket Nos. 169-9-03 Vtec,\n'
                   '        (Appeals of Tenney)                }       186\u20109\u201007 Vtec, and 70-4-08 Vtec\n'
                   '                                           }\n'
                   'Town of Lincoln,           }         Docket Nos. 226-12-03 Vtec and 261-11-07 Vtec\n'
                   '       Two other cases related to this property: Docket Nos. 170-9-03 Vtec and 217-11-04\n'
                   'Vtec were resolved, as was Appeal of Doyle, Docket No. 100-5-02 Vtec (Vt. Envtl. Ct. 2003).')
        self.assertEqual(DOCKETS.heading_dockets(bracket),
                         ['169-9-03 Vtec', '186-9-07 Vtec', '70-4-08 Vtec', '226-12-03 Vtec', '261-11-07 Vtec'])
        stacked = ('Environmental Division                       Docket Nos. 25-ENV-00030\n'
                   '32 Cherry St, 2nd Floor, Suite 303,                     25-ENV-OOO31\n'
                   'Burlington, VT 05401                                     25-ENV-00032\n'
                   'Bellows Falls Hydroelectric Project')
        self.assertEqual(DOCKETS.heading_dockets(stacked), ['25-ENV-00030', '25-ENV-00031', '25-ENV-00032'])
        supreme = ('ENTRY ORDER\n        SUPREME COURT DOCKET NO. 2005-020\n'
                   'APPEALED FROM:\n   Environmental Court\n   DOCKET NO. 54-4-03 Vtec\n')
        self.assertEqual(DOCKETS.heading_dockets(supreme), ['54-4-03 Vtec'])
        self.assertEqual(DOCKETS.heading_dockets('Appeal of X     }   Docket No. 147-10-13 Vtec147-10-13 Vtec1'),
                         ['147-10-13 Vtec'])

    def test_a_citation_that_wraps_onto_a_new_line_is_not_a_heading(self):
        page = ('Appeal of Michael and Mary       }\n                                 }    Docket No. 132-8-03 Vtec\n'
                '                                        Decision and Order\n'
                'In re Petition of Town of Sherburne, 154 Vt. 596 (1990); Appeal of Lussier and Noe,\n'
                'Docket No. 116-5-02 Vtec (Vt. Envtl. Ct., Sept. 16, 2002). If there is\n')
        self.assertEqual(DOCKETS.heading_dockets(page), ['132-8-03 Vtec', '116-5-02 Vtec'])
        self.assertEqual(HEADINGS.heading_dockets(page), ['132-8-03 Vtec'])
        stacked = ('Appeals of Madden      }     Docket Nos. 105-6-04 Vtec\n'
                   '                       }     (from 4/27/04 ZBA decision)\n'
                   '                       }     119-7-04 Vtec (from 6/3/04\n')
        self.assertEqual(HEADINGS.heading_dockets(stacked), ['105-6-04 Vtec', '119-7-04 Vtec'])

    def test_one_case_per_docket_with_shared_documents(self):
        def described(sha, listing, heading, posted='2020-05-01T12:00:00Z'):
            key = heading or listing
            return {'sha256': sha, 'title': sha, 'posted': posted, 'listing_dockets': listing,
                    'caption_dockets': heading, 'key_dockets': key, 'basis': DOCKETS.classify(listing, heading),
                    'flags': DOCKETS.flags(listing, heading, key, posted),
                    'checks': DOCKETS.checks(listing, heading), 'pages': 1, 'chars': 100}
        docs = [described('a', ['1-1-20 Vtec'], ['1-1-20 Vtec'], '2020-02-01T12:00:00Z'),
                described('b', ['2-1-20 Vtec'], ['1-1-20 Vtec', '2-1-20 Vtec'], '2020-03-01T12:00:00Z'),
                described('c', ['3-1-20 Vtec'], []),
                described('d', [], [])]
        built = DOCKETS.cases(docs)
        self.assertEqual(sorted(built), ['g-1-1-20-vtec', 'g-2-1-20-vtec', 'g-3-1-20-vtec', 'g-pdf-d'])
        self.assertEqual([d['sha256'] for d in built['g-1-1-20-vtec']['documents']], ['a', 'b'])
        self.assertEqual([d['sha256'] for d in built['g-2-1-20-vtec']['documents']], ['b'])
        self.assertEqual(built['g-1-1-20-vtec']['composition'], 'shared')
        self.assertEqual(built['g-3-1-20-vtec']['composition'], 'single')
        # A listing docket the heading doesn't name is flagged, never made into a case.
        self.assertEqual(DOCKETS.checks(['9-9-20 Vtec', 'eo20-001'], ['1-1-20 Vtec']), ['listing_only:9-9-20 Vtec'])
        self.assertEqual(DOCKETS.checks(['3-1-20 Vtec'], []), ['no_heading_docket'])
        self.assertEqual(DOCKETS.checks(['eo20-001'], []), ['no_docket'])
        # Checks stay out of the flags, which the case text shows.
        self.assertEqual(DOCKETS.flags(['9-9-20 Vtec'], ['1-1-20 Vtec'], ['1-1-20 Vtec'], None), [])
        self.assertFalse(DOCKETS.trial_court('22-AP-00296'))

    def test_extraction_is_told_a_shared_cases_docket_and_keeps_only_its_records(self):
        shared = {'dockets': ['1-1-20 Vtec'], 'composition': 'shared',
                  'documents': [{'caption_dockets': ['1-1-20 Vtec', '2-1-20 Vtec'], 'listing_dockets': []}]}
        self.assertIn('This case is docket 1-1-20 Vtec', TRIAGE.target_note(shared))
        self.assertIn('2-1-20 Vtec', TRIAGE.target_note(shared))
        self.assertIsNone(TRIAGE.target_note({**shared, 'composition': 'uniform'}))
        draft = {'dockets': [{'docket': '1-1-20 Vtec'}, {'docket': 'Docket No. 2-1-20 Vtec'}]}
        self.assertEqual(DRAFT.own_records(draft, ['1-1-20 Vtec']), ({'dockets': [{'docket': '1-1-20 Vtec'}]},
                                                                     ['Docket No. 2-1-20 Vtec']))
        self.assertEqual(DRAFT.own_records(draft, ['7-7-20 Vtec']), (draft, None))
        self.assertEqual(DRAFT.docket_identity('23-ENV-00039'), DRAFT.docket_identity('23-ENV-39'))

    def test_case_text_marks_documents_and_pages(self):
        documents = [{'doc': 1, 'sha256': 'x', 'basis': 'agree', 'caption_dockets': ['1-1-20 Vtec'],
                      'listing_dockets': ['1-1-20 Vtec'], 'flags': []}]
        pages = {'x': [{'page': 1, 'status': 'usable', 'source': 'native', 'text': 'A & B'},
                       {'page': 2, 'status': 'blank', 'source': None, 'text': None}]}
        text = RENDER.render(documents, pages)
        self.assertIn('<document doc="1" basis="agree" caption_dockets="1-1-20 Vtec"', text)
        self.assertIn('<page number="1" status="usable" source="native">\nA &amp; B\n</page>', text)
        self.assertIn('[No selected text: blank]', text)


class TextTests(unittest.TestCase):
    def test_blank_and_scan_decisions_remain_distinct(self):
        decide = lambda alnum, bad, ink, sizes: USABILITY.decide(alnum, bad, ink, sizes, None, 80, 20, 0.0005)[0]
        self.assertEqual(decide(0, 0, 0.0, []), 'blank')
        self.assertEqual(decide(0, 0, 0.15, [(2550, 3300)]), 'ocr_needed')
        self.assertEqual(decide(5, 0, 0.03, []), 'ocr_candidate')
        self.assertEqual(decide(200, 0, None, []), 'native_ok')

    def test_pgm_dark_pixel_ratio(self):
        self.assertEqual(USABILITY.pgm_ink_ratio(b'P5\n2 2\n255\n\xff\x00\xff\xff'), 0.25)

    def test_tesseract_tsv_lines_and_confidence(self):
        header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        self.assertEqual(OCR.tesseract_text(header), ('', []))
        text, scores = OCR.tesseract_text(header + '5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t91.5\tVermont\n'
                                          + '5\t1\t1\t1\t1\t2\t45\t20\t30\t12\t72\tCourt\n')
        self.assertEqual((text, scores), ('Vermont Court', [0.915, 0.72]))
        text, _ = OCR.tesseract_text(header + '5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t91\t"quoted\n'
                                     + '5\t1\t1\t1\t1\t2\t45\t20\t30\t12\t92\tfollowing\n')
        self.assertEqual(text, '"quoted following')
        self.assertEqual(OCR.mean_confidence([0.9, 0.3]), 60.0)

    def test_text_selection_is_conservative(self):
        native = lambda text, alnum: {'text': text, 'alnum': alnum, 'bad': 0}
        ocr = lambda text, alnum, words, confidence: {'status': 'complete', 'text': text, 'alnum': alnum,
                                                      'words': words, 'bad': 0, 'mean_confidence': confidence}
        self.assertEqual(SELECT.choose('native_ok', native('Court order', 10), None)[:2], ('usable', 'native'))
        self.assertEqual(SELECT.choose('blank', native('', 0), None)[0], 'blank')
        self.assertEqual(SELECT.choose('ocr_needed', native('', 0), ocr('The court issues a decision.', 23, 5, 90))[:2],
                         ('usable', 'ocr'))
        self.assertEqual(SELECT.choose('ocr_candidate', native('Judge', 5), {'status': 'failed'})[0], 'unresolved')
        self.assertEqual(SELECT.choose('ocr_candidate', native('Court order', 10), ocr('x', 1, 1, 95))[0], 'unresolved')
        self.assertEqual(SELECT.choose('ocr_candidate', native('Environmental Judge', 20),
                                       ocr('Environmental Judge', 20, 2, 90))[:2], ('usable', 'native'))


if __name__ == '__main__':
    unittest.main()
