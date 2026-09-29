"""Offline checks for deterministic docket grouping."""

import unittest

import groups


def doc(sha, listing, caption, posted="2020-05-01T12:00:00Z"):
    basis, key = groups.classify(listing, caption)
    return {"sha": sha, "title": sha, "posted": posted, "listing_raw": None,
            "listing_dockets": listing, "caption_dockets": caption, "key_dockets": key,
            "basis": basis, "flags": groups.document_flags(listing, caption, key, posted),
            "pages": 1, "chars": 100, "housing_terms": 0, "municipal_terms": 0}


class GroupingTests(unittest.TestCase):
    def test_normalizes_docket_formats(self):
        self.assertEqual(groups.normalize_dockets("154-07-07 Vtec; 22ENV00012 & 23-ENV-000131 eo10-70 25-AP-060 99-7-15Vfec"),
                         ["154-7-07 Vtec", "22-ENV-00012", "23-ENV-00131", "eo10-070",
                          "25-AP-00060", "99-7-15 Vtec"])

    def test_caption_ignores_citations_and_keeps_continuation_lines(self):
        page = ("VERMONT SUPERIOR COURT\nEnvironmental Division      Docket No. 25-ENV-00071\n"
                "32 Cherry St, 2nd Floor,       Docket No. 25-ENV-00078\nBurlington\n"
                "In re Saman ROW Approval, No. 176-10-10 Vtec, slip op. at 1 (Vt. Super. Ct.)")
        self.assertEqual(groups.caption_dockets(page), ["25-ENV-00071", "25-ENV-00078"])
        stacked = "Docket Nos. 3-1-19 Vtec\n            4-1-19 Vtec\n  In re Something Appeal"
        self.assertEqual(groups.caption_dockets(stacked), ["3-1-19 Vtec", "4-1-19 Vtec"])
        self.assertEqual(groups.caption_dockets("See No. 1-1-11 Vtec, slip op."), [])

    def test_classify_and_flags(self):
        self.assertEqual(groups.classify(["1-1-20 Vtec"], ["1-1-20 Vtec"]), ("agree", ["1-1-20 Vtec"]))
        self.assertEqual(groups.classify(["1-1-20 Vtec"], ["1-1-20 Vtec", "2-1-20 Vtec"])[0], "caption_extra")
        self.assertEqual(groups.classify([], ["2-1-20 Vtec"])[0], "caption_only")
        self.assertEqual(groups.classify(["1-1-20 Vtec"], []), ("listing_only", ["1-1-20 Vtec"]))
        self.assertEqual(groups.classify([], []), ("none", []))
        mistyped = doc("a", ["169-12-17 Vtec"], ["169-12-16 Vtec"], "2017-03-17T12:00:00Z")
        self.assertEqual(mistyped["basis"], "disagree")
        self.assertIn("typo_like:169-12-16 Vtec", mistyped["flags"])
        future = doc("b", ["169-12-17 Vtec"], [], "2017-03-17T12:00:00Z")
        self.assertIn("filed_after_posting:169-12-17 Vtec", future["flags"])

    def test_groups_join_only_through_key_dockets(self):
        docs = [doc("a", ["1-1-20 Vtec"], ["1-1-20 Vtec"], "2020-02-01T12:00:00Z"),
                doc("b", ["2-1-20 Vtec"], ["1-1-20 Vtec", "2-1-20 Vtec"], "2020-03-01T12:00:00Z"),
                doc("c", ["2-1-20 Vtec"], ["2-1-20 Vtec"], "2020-01-01T12:00:00Z"),
                # The listing says 1-1-20 but the caption disagrees: the caption decides.
                doc("d", ["1-1-20 Vtec"], ["9-9-20 Vtec"]),
                doc("e", [], [])]
        built = {g["group_id"]: g for g in groups.build_groups(docs)}
        linked = built["g-1-1-20-vtec"]
        self.assertEqual([d["sha"] for d in linked["documents"]], ["c", "a", "b"])
        self.assertEqual(linked["composition"], "linked")
        self.assertEqual(built["g-9-9-20-vtec"]["composition"], "single")
        self.assertEqual(built["g-9-9-20-vtec"]["weak_documents"], 1)
        self.assertIn("g-pdf-e", built)


if __name__ == "__main__":
    unittest.main()
