"""Edge cases for the community extraction's Python checks (kb/build_community.py): quote_in, supported,
numbers_in, locate, validate. These checks are what keeps a model's wrong reading out of the database.
    python -m pytest kb/tests/test_quote_checks.py
Threads are synthetic; part names and codes are made up, so no car or cause is special. No data files needed.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, os.path.join(ROOT, "kb"))
import build_community as B  # noqa: E402

TWELVE = "one two three four five six seven eight nine ten eleven twelve"   # 12 words, 9 four-word runs


def seg(text, kind="reply", score=None):
    return {"k": kind, "s": score, "t": text}


class QuoteIn(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(B.quote_in("the pump whines", "Since Monday the pump whines at idle."))

    def test_normalised_case_punctuation_space(self):
        self.assertTrue(B.quote_in("The PUMP, whines!", "since monday the   pump whines at idle"))

    def test_absent(self):
        self.assertFalse(B.quote_in("the belt squeals", "Since Monday the pump whines at idle."))

    def test_empty_quote_never_matches(self):
        self.assertFalse(B.quote_in("", "anything at all"))
        self.assertFalse(B.quote_in("  ,;  ", "anything at all"))

    def test_short_quote_must_be_exact(self):
        # 7 words, one changed: below 8 words there is no fuzzy pass
        self.assertFalse(B.quote_in("one two three four five six SEVEN", "one two three four five six 7even"))

    def test_long_quote_at_threshold_passes(self):
        # change word 1 and word 12: grams 1 and 9 lost, 7/9 = 0.78 >= 0.6
        text = TWELVE.replace("one", "uno").replace("twelve", "doce")
        self.assertTrue(B.quote_in(TWELVE, text))

    def test_long_quote_exactly_60_percent(self):
        # 8 words -> 5 four-word runs; change word 1 and word 8 -> runs 1 and 5 lost, 3/5 = 0.6 passes
        q = "one two three four five six seven eight"
        self.assertTrue(B.quote_in(q, "uno two three four five six seven ocho"))

    def test_long_quote_below_threshold_fails(self):
        # 8 words, word 4 changed: runs 1-4 lost, 1/5 = 0.2
        q = "one two three four five six seven eight"
        self.assertFalse(B.quote_in(q, "one two three FOUR five six seven eight".replace("FOUR", "cuatro")))

    def test_long_quote_just_under_60_percent(self):
        # 12 words -> 9 runs; words 4 and 9 changed lose runs 1-4 and 6-9 (8 runs), 1/9 kept
        text = TWELVE.replace("four", "cuatro").replace("nine", "nueve")
        self.assertFalse(B.quote_in(TWELVE, text))
        # words 1 and 5 changed: runs 1-5 lost, 4/9 = 0.44 < 0.6
        text = TWELVE.replace("one", "uno").replace("five", "cinco")
        self.assertFalse(B.quote_in(TWELVE, text))


class Supported(unittest.TestCase):
    def test_shares_stem(self):
        self.assertTrue(B.supported("leaking water pump", "the water pumps were leaking"))

    def test_shares_nothing(self):
        self.assertFalse(B.supported("alternator", "the water pump was leaking"))

    def test_shares_only_stop_word(self):
        # 'failed' and 'replaced' are on the STOP list: they can't carry support on their own
        self.assertFalse(B.supported("failed alternator", "the pump failed"))
        self.assertFalse(B.supported("replaced alternator", "I replaced the pump"))

    def test_value_of_only_stop_words_has_nothing_to_check(self):
        self.assertTrue(B.supported("replaced it", "something else entirely"))

    def test_one_shared_word_is_not_enough(self):
        self.assertFalse(B.supported("MAF sensor failure", "Replaced the MAF"))
        self.assertFalse(B.supported("coolant leak", "well, it's definitely not coolant"))
        self.assertFalse(B.supported("timing belt", "it's possible the timing is out"))

    def test_two_shared_words_pass(self):
        self.assertTrue(B.supported("MAF sensor failure", "the MAF sensor died on the highway"))

    def test_head_noun_alone_passes(self):
        self.assertTrue(B.supported("water pump", "the pump was leaking from the weep hole"))

    def test_single_word_value_needs_that_word(self):
        self.assertTrue(B.supported("thermostat", "swapped the thermostat"))
        self.assertFalse(B.supported("thermostat", "swapped the radiator"))

    def test_short_words_ignored(self):
        # words under 3 letters are not content words
        self.assertFalse(B.supported("ox valve", "an ox was here"))


class NumbersIn(unittest.TestCase):
    def test_number_present(self):
        self.assertTrue(B.numbers_in("reads 0.45 V", "it read 0.45 volts"))

    def test_number_absent(self):
        self.assertFalse(B.numbers_in("reads 0.45 V", "it read 0.5 volts"))

    def test_no_partial_number_match(self):
        self.assertFalse(B.numbers_in("45 psi", "about 145 psi"))
        self.assertFalse(B.numbers_in("1.4", "it was 11.45"))

    def test_thousands_separator(self):
        self.assertTrue(B.numbers_in("120000 miles", "at 120,000 miles"))
        self.assertTrue(B.numbers_in("120,000 miles", "at 120000 miles"))
        self.assertFalse(B.numbers_in("120000 miles", "at 120,500 miles"))
        self.assertTrue(B.numbers_in("1,2", "codes 1,2 and 3"))     # a list, not a thousands separator

    def test_no_numbers_is_true(self):
        self.assertTrue(B.numbers_in("good", "anything"))


class Locate(unittest.TestCase):
    SEGS = [seg("My car stalls when warm.", "op"), seg("Check the flux valve first."), seg("Flux valve was it.", "op_reply")]

    def test_named_segment_holds_quote(self):
        self.assertEqual(B.locate({"seg": 1, "quote": "check the flux valve"}, self.SEGS, None), (1, False))

    def test_wrong_segment_repaired(self):
        self.assertEqual(B.locate({"seg": 0, "quote": "flux valve was it"}, self.SEGS, None), (2, True))

    def test_out_of_range_segment_repaired(self):
        self.assertEqual(B.locate({"seg": 9, "quote": "stalls when warm"}, self.SEGS, None), (0, True))

    def test_quote_nowhere(self):
        self.assertEqual(B.locate({"seg": 1, "quote": "replace the gizmo"}, self.SEGS, None), (None, False))


def empty_raw(**kw):
    raw = {"relevant": True, "vehicle": {}, "codes": [], "symptoms": [], "tests": [], "ruled_out": [],
           "suggestions": [], "outcome": {}, "chains": [], "refs": []}
    raw.update(kw)
    return raw


class Validate(unittest.TestCase):
    SEGS = [seg("Code U2345 on my 2009 Examplecar with 98k miles. It stalls at idle once warm.", "op"),
            seg("Test the sprocket relay with a meter, should read 4.5 ohms.", "reply", 30),
            seg("Swapped the sprocket relay and the stalling is gone, thanks!", "op_reply"),
            seg("Glad it worked. My guess was the gizmo harness.", "reply", 2)]

    def test_wrong_segment_is_repaired_and_counted(self):
        raw = empty_raw(symptoms=[{"symptom": "stalls at idle", "seg": 2, "quote": "It stalls at idle once warm"}])
        rec, ck = B.validate(raw, self.SEGS, p90=10)
        self.assertEqual(rec["symptoms"][0]["seg"], 0)
        self.assertEqual(ck["seg_fixed"], 1)
        self.assertEqual(rec["symptoms"][0]["who"], "op")

    def test_quote_not_in_thread_dropped(self):
        raw = empty_raw(symptoms=[{"symptom": "rough idle", "seg": 0, "quote": "idles rough when cold"}])
        rec, ck = B.validate(raw, self.SEGS, p90=10)
        self.assertEqual(rec["symptoms"], [])
        self.assertEqual(ck["quote_fail"], 1)

    def test_right_quote_wrong_value_dropped(self):
        raw = empty_raw(suggestions=[{"component": "alternator", "reason": "", "seg": 1,
                                      "quote": "Test the sprocket relay with a meter"}])
        rec, ck = B.validate(raw, self.SEGS, p90=10)
        self.assertEqual(rec["suggestions"], [])
        self.assertEqual(ck["unsupported"], 1)

    def test_result_number_absent_blanks_result_keeps_item(self):
        raw = empty_raw(tests=[{"test": "resistance", "tool": "meter", "component": "sprocket relay",
                                "result": "6.2 ohms", "status": "suggested", "seg": 1,
                                "quote": "Test the sprocket relay with a meter, should read 4.5 ohms"}])
        rec, ck = B.validate(raw, self.SEGS, p90=10)
        self.assertEqual(len(rec["tests"]), 1)
        self.assertEqual(rec["tests"][0]["result"], "")
        self.assertEqual(ck["num_fail"], 1)

    def test_result_number_present_kept(self):
        raw = empty_raw(tests=[{"test": "resistance", "tool": "meter", "component": "sprocket relay",
                                "result": "4.5 ohms", "status": "suggested", "seg": 1,
                                "quote": "Test the sprocket relay with a meter, should read 4.5 ohms"}])
        rec, _ = B.validate(raw, self.SEGS, p90=10)
        self.assertEqual(rec["tests"][0]["result"], "4.5 ohms")

    def test_confirmed_by_non_op_demoted(self):
        raw = empty_raw(outcome={"fix": "replace", "component": "gizmo harness", "status": "confirmed",
                                 "came_back": "unknown", "seg": 3, "quote": "My guess was the gizmo harness"})
        rec, _ = B.validate(raw, self.SEGS, p90=10)
        self.assertEqual(rec["outcome"]["status"], "claimed")
        self.assertEqual(rec["outcome"]["op_confirmed"], 0)
        self.assertEqual(rec["outcome"]["who"], "reply")

    def test_confirmed_by_op_kept(self):
        raw = empty_raw(outcome={"fix": "swapped", "component": "sprocket relay", "status": "confirmed",
                                 "came_back": "no", "seg": 2, "quote": "Swapped the sprocket relay and the stalling is gone"})
        rec, _ = B.validate(raw, self.SEGS, p90=10)
        self.assertEqual(rec["outcome"]["status"], "confirmed")
        self.assertEqual(rec["outcome"]["op_confirmed"], 1)

    def test_who_comes_from_segment_not_model(self):
        # the model claims the OP said it; the segment is a reply
        raw = empty_raw(outcome={"fix": "replace", "component": "gizmo harness", "status": "confirmed", "who": "op",
                                 "came_back": "unknown", "seg": 3, "quote": "My guess was the gizmo harness"})
        rec, _ = B.validate(raw, self.SEGS, p90=10)
        self.assertEqual(rec["outcome"]["who"], "reply")
        self.assertEqual(rec["outcome"]["status"], "claimed")

    def test_code_must_be_in_its_quote(self):
        raw = empty_raw(codes=[{"code": "U2345", "seg": 0, "quote": "Code U2345 on my 2009 Examplecar"},
                               {"code": "P0999", "seg": 0, "quote": "Code U2345 on my 2009 Examplecar"},
                               {"code": "not-a-code", "seg": 0, "quote": "Code U2345"}])
        rec, ck = B.validate(raw, self.SEGS, p90=10)
        self.assertEqual([c["code"] for c in rec["codes"]], ["U2345"])
        self.assertEqual(rec["codes"][0]["op_had"], 1)
        self.assertEqual(ck["unsupported"], 1)

    def test_vehicle_year_and_mileage_checked_against_quote(self):
        raw = empty_raw(vehicle={"model": "Examplecar", "year": "2010", "engine": "", "transmission": "",
                                 "mileage": "98k", "seg": 0, "quote": "my 2009 Examplecar with 98k miles"})
        rec, ck = B.validate(raw, self.SEGS, p90=10)
        self.assertEqual(rec["vehicle"].get("model"), "Examplecar")
        self.assertNotIn("year", rec["vehicle"])        # 2010 is not in the quote
        self.assertEqual(rec["vehicle"].get("mileage"), "98k")
        self.assertEqual(ck["num_fail"], 1)

    def test_reply_score_and_high_score_from_segment(self):
        raw = empty_raw(suggestions=[{"component": "sprocket relay", "reason": "", "seg": 1,
                                      "quote": "Test the sprocket relay with a meter"}])
        rec, _ = B.validate(raw, self.SEGS, p90=10)
        self.assertEqual(rec["suggestions"][0]["score"], 30)
        self.assertEqual(rec["suggestions"][0]["high_score"], 1)
        rec, _ = B.validate(raw, self.SEGS, p90=50)
        self.assertEqual(rec["suggestions"][0]["high_score"], 0)

    def test_chain_needs_two_roles_and_all_quotes(self):
        good = {"steps": [{"role": "test", "seg": 1, "quote": "Test the sprocket relay"},
                          {"role": "result", "seg": 2, "quote": "the stalling is gone"}]}
        one_role = {"steps": [{"role": "test", "seg": 1, "quote": "Test the sprocket relay"},
                              {"role": "test", "seg": 2, "quote": "the stalling is gone"}]}
        bad_quote = {"steps": [{"role": "test", "seg": 1, "quote": "Test the sprocket relay"},
                               {"role": "result", "seg": 2, "quote": "engine exploded"}]}
        rec, _ = B.validate(empty_raw(chains=[good, one_role, bad_quote]), self.SEGS, p90=10)
        self.assertEqual(len(rec["chains"]), 1)

    def test_ref_value_must_be_in_quote(self):
        raw = empty_raw(refs=[{"kind": "cost", "value": "$450", "seg": 2, "quote": "Swapped the sprocket relay"}])
        rec, ck = B.validate(raw, self.SEGS, p90=10)
        self.assertEqual(rec["refs"], [])
        self.assertEqual(ck["unsupported"], 1)

    def test_counters_add_up(self):
        raw = empty_raw(symptoms=[{"symptom": "stalls at idle", "seg": 0, "quote": "It stalls at idle"},
                                  {"symptom": "smoke", "seg": 0, "quote": "blue smoke on startup"}],
                        suggestions=[{"component": "alternator", "reason": "", "seg": 1, "quote": "Test the sprocket relay"}])
        _, ck = B.validate(raw, self.SEGS, p90=10)
        self.assertEqual(ck["items"], 3)
        self.assertEqual(ck["kept"] + ck["quote_fail"] + ck["unsupported"], ck["items"])


if __name__ == "__main__":
    unittest.main()
