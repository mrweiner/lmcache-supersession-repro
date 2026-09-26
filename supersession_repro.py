#!/usr/bin/env python3
"""supersession_repro.py - standalone reproducer: LMCache recurrent-checkpoint
supersession + (L1 pressure | clean restart) destroys a listed checkpoint's
unique pages.

Mechanism (LMCache integration/local-inference-lab 2915c9d3cd7e, vLLM
integration/karmic-kraken-beta 04c30fa98e79):
  * modules/checkpoint.py supersede() 197-265: when a request publishes a
    *prompt* checkpoint, older checkpoints on its prefix - and every sibling
    of their owning request (:236), ancestor or not - are marked superseded.
    Their manifests stay listed.
  * eviction_controller.py _drop_superseded() :214 (eviction pass refactored
    into _run_eviction_pass, same order): above the L1 watermark, superseded
    pages are deleted before any LRU victim, never written to L2.
  * storage_manager.py:627: the shutdown flush skips superseded pages.
  => a later request that extends the *main line* is offered the dead
     manifest; the retrieve reports "K of M pages were readable" (~
     checkpoint_storage.py:715; the checkpoint's unique pages - recurrent
     state + auxiliary (+ partial tail) - are gone), walks down, and
     recomputes from zero.

Uses ONLY: /v1/completions with token-ID prompts, /metrics, and stock
upstream INFO log lines. No patches or instrumentation required.

Cells (each with a fresh random prompt P; A publishes prompt + response
checkpoints; A2 = P + A's response + [y] targets A's RESPONSE checkpoint,
which is NOT an ancestor of B = P + [x] -> exercises sibling marking):

  pressure mode:  P0 control | P1 supersede | P2 pressure | P3 supersede+pressure
  restart mode:   R0 control (clean restart) | R1 supersede + clean restart

Expected: only P3 and R1 fail. A cell is INVALID (not a finding) if its
preconditions are not met in the log/metrics.

Serve-path honesty: on stock builds a *successful* external restore is
silent - no log line, and vllm:external_prefix_cache_hits_total does not
count recurrent restores (verified: cached=40000 with the counter flat);
vllm:prefix_cache_hits_total moves identically for a GPU-local hit and an
external restore. So a control cell cannot prove its restore took the
external path. Mitigations, per cell:
  * restart cells (R0/R1): structurally external - A2 runs on a fresh
    process with an empty prefix cache.
  * P2/P3: the ~30+ completing fillers cycle the 479k-token KV pool
    (39 x 40k >> 479k), so A's resident lineage is LRU-evicted long
    before A2.
  * P0/P1: the evict step sizes itself against the pool arithmetic
    (see evict_gpu). In the verified run both came back GPU-LOCAL
    (stock stall line: engine queue ~0.02 s, first output ~0.19 s,
    vs ~0.2 s / ~0.4 s on the external path) - they are NOT L1
    controls; the supersede-only control is not demonstrated on a
    stock engine by this run. Pass --serve-queue-threshold 0.1 to
    fail such cells loudly; the finding cells (P3, R1) are immune
    because their expected result - a failed restore - is loud.

Requirements on the engine: LMCACHE_L2_CHECKPOINT_WRITES=on-evict (or the
checkpoint_on_evict L2 store policy), --recurrent-checkpoint-policy
request_boundaries, no other traffic while the script runs.

Stdlib only. Run `--help` for options.
"""
import argparse
import datetime as dt
import http.client
import json
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- stock log lines
ANSI = re.compile(r"\x1b\[[0-9;]*m")
RE_RETRIEVE_MISS = re.compile(
    r"Checkpoint retrieve of (\d+) tokens for rank (\d+) missed.*?"
    r"(\d+) of (\d+) pages were readable")
RE_RESTORE_FAIL = re.compile(
    r"Recurrent checkpoint restore of (\d+) tokens failed for request (\S+) "
    r"on ranks \[([\d, ]+)\] after ([\d.]+) s")
RE_EVICT = re.compile(
    r"L1 memory usage ([\d.]+) above watermark ([\d.]+); triggering eviction")
RE_FLUSH_START = re.compile(
    r"Writing (\d+) current checkpoint pages to L2 before shutdown")
RE_FLUSH_DONE = re.compile(r"Shutdown checkpoint flush completed in ([\d.]+) s")
RE_FLUSH_LEFT = re.compile(r"flush left (\d+) pages without an L2 copy")
RE_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)Z?\s")
RE_STALL_QUEUE = re.compile(r"engine queue ([\d.]+) s")
RE_STALL_FO = re.compile(r"submission to first output ([\d.]+) s")
RE_STALL_PROMPT = re.compile(r"prompt (\d+) tokens \((\d+) cached\)")


def parse_stall_line(line):
    """The stock per-request stall_diagnostics line carries the
    serve-path discriminator: an external restore parks the request at
    the LMCache lookup (~0.2 s engine queue); a GPU-local hit shows
    ~0.02 s. Returns None for non-stall lines."""
    if "stall_diagnostics" not in line and "engine queue" not in line:
        return None
    q, fo, pr = (RE_STALL_QUEUE.search(line), RE_STALL_FO.search(line),
                 RE_STALL_PROMPT.search(line))
    if not (q and fo and pr):
        return None
    return {"engine_queue_s": float(q.group(1)),
            "first_output_s": float(fo.group(1)),
            "prompt_tokens": int(pr.group(1)),
            "cached_tokens": int(pr.group(2))}

METRIC_EXT_HITS = "vllm:external_prefix_cache_hits_total"
METRIC_PREFIX_HITS = "vllm:prefix_cache_hits_total"
METRIC_PREFIX_QUERIES = "vllm:prefix_cache_queries_total"
METRIC_RUNNING = "vllm:num_requests_running"
METRIC_WAITING = "vllm:num_requests_waiting"


def log(msg):
    print(f"[{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')}] {msg}",
          flush=True)


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(line):
    m = RE_TS.match(line)
    if not m:
        return None
    s = m.group(1)
    if "." in s:  # docker emits nanoseconds; keep microseconds
        head, frac = s.split(".")
        s = f"{head}.{frac[:6]}"
    try:
        return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------- engine access
class Engine:
    def __init__(self, args):
        self.a = args
        u = urllib.parse.urlparse(args.base_url)
        self.host, self.port = u.hostname, u.port or 80

    # -- http
    def _post(self, path, body, timeout):
        req = urllib.request.Request(
            self.a.base_url + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def complete(self, prompt_ids, max_tokens=1, want_ids=False):
        body = {"model": self.a.model, "prompt": prompt_ids,
                "max_tokens": max_tokens, "temperature": 0,
                "ignore_eos": True}
        if want_ids:
            body.update({"logprobs": 1, "return_tokens_as_token_ids": True})
        r = self._post("/v1/completions", body, timeout=self.a.http_timeout)
        out_ids = None
        if want_ids:
            toks = r["choices"][0]["logprobs"]["tokens"]
            out_ids = [int(t.split(":", 1)[1]) for t in toks]
        usage = r.get("usage") or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        return {"id": r.get("id"), "prompt_tokens": usage.get("prompt_tokens"),
                "cached_tokens": cached, "out_ids": out_ids,
                "finish": r["choices"][0].get("finish_reason")}

    def abort_after(self, prompt_ids, hold_s):
        """Full-prefill evictor: stream until the FIRST output token
        arrives, then drop the connection. The completed prefill fully
        allocates its blocks, forcing the pool's LRU to cycle (older
        lineages are evicted); aborting on the first token happens
        before the prompt-checkpoint publish, so the evictor stores
        nothing. hold_s is a cap on how long to wait for the first
        token."""
        body = json.dumps({"model": self.a.model, "prompt": prompt_ids,
                           "max_tokens": 1, "stream": True}).encode()
        c = http.client.HTTPConnection(self.host, self.port, timeout=30)
        c.request("POST", "/v1/completions", body,
                  {"Content-Type": "application/json"})
        t0 = time.time()
        try:
            buf = b""
            while time.time() - t0 < hold_s:
                chunk = c.sock.recv(65536) if c.sock else b""
                if not chunk:
                    break
                buf += chunk
                if b'"content"' in buf or b"'content'" in buf:
                    break
        except OSError:
            pass
        finally:
            c.close()

    def metrics(self):
        with urllib.request.urlopen(self.a.base_url + "/metrics", timeout=30) as r:
            text = r.read().decode()
        vals = {}
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            name = line.split("{", 1)[0].split(" ", 1)[0]
            try:
                v = float(line.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                continue
            vals[name] = vals.get(name, 0.0) + v
        return vals

    def metric_delta(self, name, before, after):
        return (after.get(name, 0.0) - before.get(name, 0.0),
                before.get(name, 0.0), after.get(name, 0.0))

    def assert_idle(self, tries=12, gap=5):
        for i in range(tries):
            m = self.metrics()
            busy = m.get(METRIC_RUNNING, 0) + m.get(METRIC_WAITING, 0)
            if busy <= 0:
                return
            time.sleep(gap)
        raise SystemExit(f"engine busy ({busy:.0f} requests running/waiting); "
                         "other traffic confounds the result - stop it first")

    def wait_ready(self, timeout=900):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                with urllib.request.urlopen(self.a.base_url + "/v1/models",
                                            timeout=10):
                    pass
                self.complete([self.a.vocab_lo + 7] * 16, max_tokens=1)
                return
            except Exception:
                time.sleep(5)
        raise SystemExit("engine did not become ready")

    # -- logs
    def log_lines(self, since):
        if self.a.log_file:
            with open(self.a.log_file, errors="replace") as f:
                lines = f.read().splitlines()
        else:
            out = subprocess.run(
                ["docker", "logs", "--timestamps", "--since",
                 since.strftime("%Y-%m-%dT%H:%M:%S"), self.a.container],
                capture_output=True, text=True)
            lines = (out.stdout + out.stderr).splitlines()
        res = []
        for ln in lines:
            ln = ANSI.sub("", ln)
            ts = parse_ts(ln)
            if ts is None or ts >= since:
                res.append((ts, ln))
        return res

    # -- restart
    def clean_restart(self):
        if self.a.manual_restart:
            input("Stop the engine CLEANLY (e.g. docker stop -t 60), start it, "
                  "wait until it serves, then press Enter... ")
            self.wait_ready()
            return {"exit_code": None}
        subprocess.run(["docker", "stop", "-t", str(self.a.stop_timeout),
                        self.a.container], check=True, capture_output=True)
        code = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.ExitCode}}", self.a.container],
            capture_output=True, text=True).stdout.strip()
        subprocess.run(["docker", "start", self.a.container], check=True,
                       capture_output=True)
        self.wait_ready()
        return {"exit_code": code}


# ---------------------------------------------------------------- the cells
class Runner:
    def __init__(self, eng, args):
        self.e, self.a = eng, args
        self.rng = random.Random(args.seed)

    def rand_ids(self, n):
        return [self.rng.randrange(self.a.vocab_lo, self.a.vocab_hi)
                for _ in range(n)]

    def settle(self):
        time.sleep(self.a.publish_wait)

    def evict_gpu(self):
        """Clear the engine's own copy of A's KV from the GPU pool.

        Pool arithmetic (this engine: 479k-token KV pool): an aborted
        evictor only forces eviction when resident + incoming exceed the
        pool. A completing warm filler first raises the resident set
        (~260k with A), then two aborted evictors (260k each) overflow it
        (260k + 260k > 479k) twice, so A's lineage is LRU-evicted. Aborts
        never finish prefill, so they publish no prompt checkpoint and
        add no L1 pressure."""
        self.e.complete(self.rand_ids(self.a.warm_tokens), max_tokens=1)
        time.sleep(2)
        for _ in range(self.a.evict_count):
            self.e.abort_after(self.rand_ids(self.a.evict_tokens),
                               self.a.evict_hold)
            time.sleep(2)

    def apply_pressure(self, since):
        for i in range(self.a.max_fillers):
            self.e.complete(self.rand_ids(self.a.filler_tokens), max_tokens=1)
            time.sleep(1)
            if any(RE_EVICT.search(l) for _, l in self.e.log_lines(since)):
                log(f"  L1 crossed the watermark after {i + 1} fillers")
                return True
        return False

    def expected_missing(self, ckpt_len):
        partial = 1 if ckpt_len % self.a.page_size else 0
        return self.a.recurrent_groups + 1 + partial

    def run_cell(self, name, supersede, pressure, restart):
        log(f"cell {name}: supersede={supersede} pressure={pressure} "
            f"restart={restart}")
        if self.a.restart_between_cells and not restart and not self.a.manual_restart:
            self.e.clean_restart()           # fresh L1 headroom per cell
        self.e.assert_idle()
        t0 = utcnow()
        P = self.rand_ids(self.a.p_tokens)
        x, y = self.rand_ids(2)
        while y == x:
            y = self.rand_ids(1)[0]

        a = self.e.complete(P, max_tokens=self.a.response_tokens, want_ids=True)
        resp = a["out_ids"]
        resp_ckpt = len(P) + len(resp) - 1
        self.settle()
        t_b = utcnow()
        if supersede:
            self.e.complete(P + [x], max_tokens=1)
            self.settle()
        pressured = False
        restart_info = None
        if pressure:
            pressured = self.apply_pressure(t_b)
        if restart:
            restart_info = self.e.clean_restart()
        else:
            self.evict_gpu()
        self.e.assert_idle()
        t_a2 = utcnow()
        m_before = self.e.metrics()
        a2 = self.e.complete(P + resp + [y], max_tokens=1)
        time.sleep(2)
        m_after = self.e.metrics()
        stall = None
        for ts, l in self.e.log_lines(t_a2):
            if a2["id"] and a2["id"] in l:
                s = parse_stall_line(l)
                if s:
                    stall = s
                    break
        ph_d, _, _ = self.e.metric_delta(METRIC_PREFIX_HITS, m_before, m_after)
        pq_d, _, _ = self.e.metric_delta(METRIC_PREFIX_QUERIES, m_before, m_after)
        ex_d, _, _ = self.e.metric_delta(METRIC_EXT_HITS, m_before, m_after)

        lines = self.e.log_lines(t0)
        a2_id = (a2["id"] or "").strip()
        evict_lines = [l for ts, l in lines if RE_EVICT.search(l)
                       and (ts is None or t_b <= ts <= t_a2)]
        fails = [RE_RESTORE_FAIL.search(l) for _, l in lines]
        fails = [m for m in fails if m and a2_id and m.group(2).startswith(a2_id)]
        misses = [RE_RETRIEVE_MISS.search(l) for ts, l in lines
                  if ts is None or ts >= t_a2]
        misses = [m for m in misses if m]
        flush = {"start": [int(RE_FLUSH_START.search(l).group(1))
                           for _, l in lines if RE_FLUSH_START.search(l)],
                 "done_s": [float(RE_FLUSH_DONE.search(l).group(1))
                            for _, l in lines if RE_FLUSH_DONE.search(l)],
                 "left": [int(RE_FLUSH_LEFT.search(l).group(1))
                          for _, l in lines if RE_FLUSH_LEFT.search(l)]}

        restored = (a2["cached_tokens"] or 0) >= 0.9 * resp_ckpt and not fails
        failed = bool(fails)
        # ---- validity (preconditions observed, not intended)
        problems = []
        if pressure and not (pressured and evict_lines):
            problems.append("no watermark crossing logged between B and A2")
        if not pressure and evict_lines:
            problems.append("unintended L1 pressure between B and A2")
        if restart and (not flush["done_s"] or flush["left"]):
            problems.append("shutdown flush did not complete cleanly")
        if not restored and not failed:
            problems.append("A2 cached below threshold with no restore "
                            "failure lines (GPU-local hit or no offer; "
                            "stock builds cannot discriminate - see "
                            "README 'Serve-path honesty')")
        if (self.a.serve_queue_threshold is not None and stall
                and verdict == "restored"
                and stall["engine_queue_s"] < self.a.serve_queue_threshold):
            problems.append(
                f"restored cell has engine queue "
                f"{stall['engine_queue_s']} s (< "
                f"{self.a.serve_queue_threshold}): A2 was GPU-local, "
                "not served from LMCache (observed external restores "
                "park ~0.2 s at the lookup; GPU hits ~0.02 s)")
        expect_fail = supersede and (pressure or restart)
        verdict = "failed" if failed else "restored" if restored else "unclear"
        want_missing = self.expected_missing(resp_ckpt)
        fingerprint = [(int(m.group(1)), int(m.group(3)), int(m.group(4)))
                       for m in misses]
        first = [f for f in fingerprint if f[0] == resp_ckpt]
        fp_ok = (not expect_fail) or any(
            total - readable == want_missing for _, readable, total in first)
        return {
            "cell": name, "supersede": supersede, "pressure": pressure,
            "restart": restart, "p_tokens": len(P),
            "response_ckpt": resp_ckpt,
            "a_cached": a["cached_tokens"], "a2_cached": a2["cached_tokens"],
            "a2_prefix_hits_delta": ph_d, "a2_prefix_queries_delta": pq_d,
            "a2_external_hits_delta": ex_d,
            "a2_stall": stall,
            "evict_lines_b_to_a2": len(evict_lines),
            "restore_failures": [(int(m.group(1)), float(m.group(4)))
                                 for m in fails],
            "retrieve_misses_tokens_readable_total": fingerprint,
            "expected_missing_per_rank": want_missing if expect_fail else 0,
            "flush": flush, "restart_info": restart_info,
            "verdict": verdict, "expected": "failed" if expect_fail else "restored",
            "valid": not problems, "problems": problems,
            "matches": (not problems) and verdict == (
                "failed" if expect_fail else "restored") and fp_ok,
        }


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--model", required=True)
    p.add_argument("--container", help="docker container (logs + restarts)")
    p.add_argument("--log-file", help="read engine log from a file instead")
    p.add_argument("--manual-restart", action="store_true",
                   help="pause and let you restart the engine yourself")
    p.add_argument("--mode", choices=["pressure", "restart", "all"],
                   default="all")
    p.add_argument("--p-tokens", type=int, default=40000)
    p.add_argument("--response-tokens", type=int, default=240)
    p.add_argument("--page-size", type=int, default=3072,
                   help="checkpoint page size in tokens")
    p.add_argument("--recurrent-groups", type=int, default=7,
                   help="recurrent-state KV groups (GLM-5.3-Flash: 7)")
    p.add_argument("--vocab-lo", type=int, default=1000)
    p.add_argument("--vocab-hi", type=int, default=100000)
    p.add_argument("--publish-wait", type=float, default=5.0)
    p.add_argument("--warm-tokens", type=int, default=220000,
                   help="completing warm filler before the aborted evictors "
                        "(raises the resident set so the evictors overflow "
                        "the KV pool)")
    p.add_argument("--evict-tokens", type=int, default=260000)
    p.add_argument("--evict-count", type=int, default=2)
    p.add_argument("--evict-hold", type=float, default=300.0,
                   help="cap on seconds to wait for the evictor's first "
                        "token (the abort happens at the first token, "
                        "after the full prefill allocated its blocks)")
    p.add_argument("--filler-tokens", type=int, default=40000)
    p.add_argument("--max-fillers", type=int, default=60)
    p.add_argument("--stop-timeout", type=int, default=60)
    p.add_argument("--restart-between-cells", action="store_true",
                   help="clean restart before each non-restart cell "
                        "(fresh L1 headroom; slower)")
    p.add_argument("--serve-queue-threshold", type=float, default=None,
                   help="flag a restored cell INVALID when A2's stock "
                        "stall line shows engine queue below this "
                        "(observed: ~0.2 s external lookup park, "
                        "~0.02 s GPU hit); default off")
    p.add_argument("--seed", type=int, default=int(time.time()))
    p.add_argument("--http-timeout", type=float, default=900)
    p.add_argument("--out", default="supersession_repro_result.json")
    a = p.parse_args()
    if not (a.container or a.log_file):
        p.error("need --container or --log-file")
    if a.mode in ("restart", "all") and not (a.container or a.manual_restart):
        p.error("restart mode needs --container or --manual-restart")

    eng = Engine(a)
    eng.wait_ready()
    r = Runner(eng, a)
    cells = []
    if a.mode in ("pressure", "all"):
        cells += [("P0-control", False, False, False),
                  ("P1-supersede", True, False, False),
                  ("P2-pressure", False, True, False),
                  ("P3-supersede+pressure", True, True, False)]
    if a.mode in ("restart", "all"):
        cells += [("R0-restart-control", False, False, True),
                  ("R1-supersede+restart", True, False, True)]
    results = [r.run_cell(*c) for c in cells]

    print("\n{:<24} {:<9} {:<9} {:<6} {:>12}  {}".format(
        "cell", "expected", "observed", "valid", "a2_cached", "retrieve misses"))
    for x in results:
        print("{:<24} {:<9} {:<9} {:<6} {:>12}  {}".format(
            x["cell"], x["expected"], x["verdict"], str(x["valid"]),
            x["a2_cached"],
            x["retrieve_misses_tokens_readable_total"] or "-"))
        for pr in x["problems"]:
            print(f"    INVALID: {pr}")
    with open(a.out, "w") as f:
        json.dump({"seed": a.seed, "args": vars(a), "cells": results}, f,
                  indent=1)
    print(f"\nwritten {a.out}")
    if not all(x["valid"] for x in results):
        sys.exit(2)
    sys.exit(0 if all(x["matches"] for x in results) else 1)


if __name__ == "__main__":
    main()
