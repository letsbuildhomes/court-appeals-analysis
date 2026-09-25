"""Focused checks for the exploratory native-text QA measurements."""

import unittest

from qa_triage import compare_ocr, parse_bbox, parse_image_list, pgm_ink_bands, tsv_text


class QAParsingTests(unittest.TestCase):
    def test_bbox_discards_pdf_control_characters(self):
        source = (b'<html><page width="100" height="200">'
                  b'<word yMin="50" yMax="60">Alpha</word>'
                  b'<word yMin="70" yMax="80">\x01Beta</word>'
                  b'</page></html>')
        self.assertEqual(parse_bbox(source),
                         [(100.0, 200.0, [("Alpha", 0.275), ("Beta", 0.375)])])

    def test_image_list_uses_display_resolution_columns(self):
        output = ("page num type width height color comp bpc enc interp object ID x-ppi y-ppi size ratio\n"
                  "---\n"
                  "1 0 image 1200 1600 rgb 3 8 image no 5 0 144 144 1M 10%\n")
        self.assertEqual(parse_image_list(output), {1: [(1200, 1600, 144.0, 144.0)]})

    def test_ink_bands_use_pixel_values(self):
        # Five rows of two pixels: only the middle row contains ink.
        pgm = b"P5\n2 5\n255\n" + bytes([255] * 4 + [0, 255] + [255] * 4)
        overall, bands = pgm_ink_bands(pgm)
        self.assertEqual(overall, 0.1)
        self.assertEqual(bands, [0, 0, 0.5, 0, 0])


class OCRComparisonTests(unittest.TestCase):
    def test_confident_ocr_passage_missing_from_native_is_flagged(self):
        heading = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
                   "left\ttop\twidth\theight\tconf\ttext\n")
        words = "one two three four five six seven eight nine ten eleven twelve".split()
        tsv = heading + "".join(
            f"5\t1\t1\t1\t1\t1\t0\t1000\t30\t20\t95\t{word}\n" for word in words
        )
        result = compare_ocr(tsv, "unrelated native text", 612, 792, 0.25, False)
        self.assertEqual(result["category"], "ocr_disagreement")
        # The suspected-omission rule requires at least 20 confident words.
        tsv += "".join(
            f"5\t1\t1\t1\t1\t1\t0\t1000\t30\t20\t95\t{word}\n" for word in words
        )
        result = compare_ocr(tsv, "unrelated native text", 612, 792, 0.25, False)
        self.assertEqual(result["category"], "suspected_native_omission")

    def test_unescaped_quote_does_not_absorb_following_tsv_rows(self):
        heading = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
                   "left\ttop\twidth\theight\tconf\ttext\n")
        tsv = (heading
               + '5\t1\t1\t1\t1\t1\t0\t1000\t30\t20\t95\t"quoted\n'
               + '5\t1\t1\t1\t1\t2\t30\t1000\t30\t20\t95\tfollowing\n')
        self.assertEqual(tsv_text(tsv), '"quoted following')
        result = compare_ocr(tsv, 'quoted following', 612, 792, 0, False)
        self.assertEqual(result["high_conf_words"], 2)
        self.assertEqual(result["missing_words"], 0)


if __name__ == "__main__":
    unittest.main()
