"""Offline tests: stock-line regexes validated against lines captured
from a real engine (scrubbed), plus the expected-missing page math.
Stdlib unittest."""
import unittest

import supersession_repro as sr

REAL_RETRIEVE_MISS = (
    "2026-09-26T00:24:07.407098557Z [2026-09-26 00:24:07,407] LMCache "
    "INFO: Checkpoint retrieve of 131225 tokens for rank 0 missed; its "
    "checkpoint is no longer listed after 0.0 s: 0 of 51 pages were "
    "readable (checkpoint_storage.py:715)")
REAL_RESTORE_FAIL = (
    "2026-09-26T00:24:07.410503145Z (EngineCore pid=442) [2026-09-26 "
    "00:24:07,410] LMCache INFO: Recurrent checkpoint restore of 131225 "
    "tokens failed for request cmpl-scrubbed-example on ranks [0, 1] after 0.0 s "
    "(checkpoint_scheduler.py:618)")
REAL_RESTORE_MISS = (
    "2026-09-26T00:24:07.410557428Z (EngineCore pid=442) [2026-09-26 "
    "00:24:07,410] LMCache INFO: Recurrent checkpoint restore of 131225 "
    "tokens missed for request cmpl-scrubbed-example; looking up a shorter "
    "checkpoint (attempt 2) (checkpoint_scheduler.py:634)")
REAL_EVICT = (
    "2026-09-26T00:00:00.750795623Z [2026-09-26 00:00:00,750] LMCache "
    "INFO: L1 memory usage 0.83 above watermark 0.80; triggering "
    "eviction. (eviction_controller.py:456)")
REAL_FLUSH_START = (
    "2026-09-26T00:07:51.973341875Z [2026-09-26 00:07:51,973] LMCache "
    "INFO: Writing 66 current checkpoint pages to L2 before shutdown "
    "(budget 30 s) (storage_manager.py:632)")
REAL_FLUSH_DONE = (
    "2026-09-26T00:03:50.820010864Z [2026-09-26 00:03:50,819] LMCache "
    "INFO: Shutdown checkpoint flush completed in 1.4 s "
    "(storage_manager.py:652)")


class TestStockLines(unittest.TestCase):
    def test_retrieve_miss(self):
        m = sr.RE_RETRIEVE_MISS.search(REAL_RETRIEVE_MISS)
        self.assertIsNotNone(m)
        self.assertEqual((m.group(1), m.group(2), m.group(3), m.group(4)),
                         ("131225", "0", "0", "51"))
        self.assertIsNone(sr.RE_RETRIEVE_MISS.search(REAL_RESTORE_FAIL))

    def test_restore_fail(self):
        m = sr.RE_RESTORE_FAIL.search(REAL_RESTORE_FAIL)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "131225")
        self.assertEqual(m.group(2), "cmpl-scrubbed-example")
        self.assertEqual(m.group(4), "0.0")
        # "missed" (walk-down) must NOT count as a failed restore
        self.assertIsNone(sr.RE_RESTORE_FAIL.search(REAL_RESTORE_MISS))

    def test_evict(self):
        m = sr.RE_EVICT.search(REAL_EVICT)
        self.assertIsNotNone(m)
        self.assertEqual((m.group(1), m.group(2)), ("0.83", "0.80"))

    def test_flush(self):
        self.assertEqual(
            sr.RE_FLUSH_START.search(REAL_FLUSH_START).group(1), "66")
        self.assertEqual(
            sr.RE_FLUSH_DONE.search(REAL_FLUSH_DONE).group(1), "1.4")

    def test_parse_ts(self):
        self.assertEqual(sr.parse_ts(REAL_FLUSH_START).year, 2026)
        self.assertIsNone(sr.parse_ts("[2026-09-26 00:07:51] no docker ts"))

    def test_ansi_stripped(self):
        self.assertEqual(sr.ANSI.sub("", "a\x1b[32;20mb"), "ab")


REAL_STALL = (
    "2026-09-26T20:02:18.750729047Z INFO 09-26 20:02:18 "
    "[stall_diagnostics.py:189] Request cmpl-<scrubbed>-0 finished after "
    "0.44 s (arrived 20:02:18.302), HTTP receipt to body read 0.00 s, "
    "body read to rendering 0.00 s, arrival to generator start 0.00 s, "
    "input processing 0.00 s, engine submission 0.00 s, submission to "
    "first output 0.43 s, first to final output 0.00 s, final output to "
    "stream end 0.01 s, engine queue 0.22 s, prompt 40241 tokens (40239 "
    "cached), 1 generated, finish reason length")


class TestStallLine(unittest.TestCase):
    """The stock per-request line carries the serve-path discriminator:
    an external restore parks the request at the LMCache lookup and
    shows ~0.2 s of engine queue; a GPU-local hit shows ~0.02 s."""

    def test_fields(self):
        self.assertEqual(sr.RE_STALL_QUEUE.search(REAL_STALL).group(1),
                         "0.22")
        self.assertEqual(sr.RE_STALL_FO.search(REAL_STALL).group(1), "0.43")
        m = sr.RE_STALL_PROMPT.search(REAL_STALL)
        self.assertEqual((m.group(1), m.group(2)), ("40241", "40239"))

    def test_parse_stall(self):
        self.assertEqual(sr.parse_stall_line(REAL_STALL),
                         {"engine_queue_s": 0.22, "first_output_s": 0.43,
                          "prompt_tokens": 40241, "cached_tokens": 40239})
        self.assertIsNone(sr.parse_stall_line(REAL_RESTORE_FAIL))


class TestPageMath(unittest.TestCase):
    """    Page model verified against real retrieves: 131,225 tokens ->
    51 total pages; 39,966 -> 22; 36,864 -> 20.
    total = ceil(tokens / page_size) + recurrent_groups + 1 (auxiliary);
    missing = recurrent_groups + 1 (+1 partial tail when non-aligned)."""

    @staticmethod
    def total_pages(tokens, page, groups):
        return -(-tokens // page) + groups + 1

    def test_model_matches_real_lines(self):
        self.assertEqual(self.total_pages(131225, 3072, 7), 51)
        self.assertEqual(self.total_pages(39966, 3072, 7), 22)
        self.assertEqual(self.total_pages(36864, 3072, 7), 20)

    def test_expected_missing(self):
        r = sr.Runner.__new__(sr.Runner)  # no engine needed
        r.a = type("A", (), {"page_size": 3072, "recurrent_groups": 7})()
        self.assertEqual(r.expected_missing(40062), 9)   # non-aligned
        self.assertEqual(r.expected_missing(36864), 8)   # block-aligned


class TestCellProblems(unittest.TestCase):
    """The validity judgment is pure; the --serve-queue-threshold path
    must work without an engine (regression: run_cell once referenced
    `verdict` before it was assigned when the flag was set)."""

    def base(self, **kw):
        args = dict(supersede=False, pressure=False, restart=False,
                    pressured=False, evict_lines=0, flush_done=[4.0],
                    flush_left=[], restored=True, failed=False,
                    stall={"engine_queue_s": 0.22, "first_output_s": 0.43},
                    serve_queue_threshold=None)
        args.update(kw)
        return sr.cell_problems(**args)

    def test_clean_control(self):
        self.assertEqual(self.base(), [])

    def test_threshold_external_restored(self):
        self.assertEqual(
            self.base(supersede=True, pressure=True, pressured=True,
                      evict_lines=1,
                      serve_queue_threshold=0.1), [])

    def test_threshold_gpu_served_flagged(self):
        p = self.base(serve_queue_threshold=0.1,
                      stall={"engine_queue_s": 0.02})
        self.assertEqual(len(p), 1)
        self.assertIn("GPU-local", p[0])

    def test_threshold_off_no_flag(self):
        self.assertEqual(self.base(stall={"engine_queue_s": 0.02}), [])

    def test_threshold_unclear_restored_not_flagged(self):
        # only restored cells are flagged; unclear cells get the
        # serve-path hint instead
        p = self.base(restored=False, serve_queue_threshold=0.1,
                      stall={"engine_queue_s": 0.02})
        self.assertEqual(len(p), 1)
        self.assertIn("serve path", p[0])

    def test_preconditions(self):
        self.assertIn("crossing", self.base(pressure=True, pressured=False,
                                            evict_lines=0)[0])
        self.assertIn("unintended", self.base(pressure=False,
                                              evict_lines=2)[0])
        self.assertIn("flush", self.base(restart=True, flush_done=[])[0])


if __name__ == "__main__":
    unittest.main()
