# lmcache-supersession-repro

Standalone reproducer for an LMCache recurrent-checkpoint data-loss class:

**Supersession marks non-ancestor checkpoints — including response
checkpoints — as superseded while their manifests stay listed. Two
triggers then destroy the marked lineage's unique pages: L1 pressure
(`_drop_superseded()`, deleted before any LRU victim, never written to
L2) and a clean shutdown's flush (`storage_manager.py:627`, skips
superseded pages). Supersession alone is harmless. Fingerprint: shared
KV pages stay readable; the checkpoint's unique pages are gone —
7 recurrent-state + 1 auxiliary (+ 1 partial-tail attention page; 8
unique pages when the checkpoint is page-aligned).**

Public report context: the "8 state keys missing, KV 100% readable"
signature after clean restarts and after live evictions.

Uses only `/v1/completions` with token-ID prompts, `/metrics`, and
stock upstream log lines. No patched builds, no instrumentation.

## Provenance of the verified run

- Engine image: `ghcr.io/local-inference-lab/vllm:karmic-kraken-beta`,
  digest `sha256:c7bd92618bd0e0c0e2feb6f4b145cbf574c2c62325aa293781d98f99b1aed125`.
- Upstream tips the reviewer checked (mechanism unchanged):
  vLLM `integration/karmic-kraken-beta` = `04c30fa98e79`;
  LMCache `integration/local-inference-lab` = `2915c9d3cd7e`.
- Before the run, the container's full `lmcache` and `vllm` trees under
  `/opt/venv/lib/python3.12/site-packages` were compared against a
  throwaway container from the same image: exactly three files differed
  (a runtime instrumentation layer, unrelated to the mechanism). Those
  three were reverted to the image's stock bytes for the run:

  | file | stock sha256 (prefix) |
  |---|---|
  | `lmcache/integration/vllm/checkpoint_scheduler.py` | `25a42f3e74e238ba` |
  | `lmcache/v1/distributed/storage_controllers/eviction_controller.py` | `7f1555b148fd8b21` |
  | `vllm/v1/core/sched/scheduler.py` | `264988b3803d5a70` |

  The run window's log contains zero instrumentation lines. The
  instrumented files were restored and sha-verified afterwards.
- Stock line numbers below are as deployed in the image; the cited
  upstream tips shift a few lines (`supersede()` 197–265 with the
  sibling loop at :236, `_drop_superseded()` :214 with the eviction
  pass in `_run_eviction_pass`, `storage_manager.py:627` unchanged,
  retrieve-miss near `checkpoint_storage.py:715`).

## Engine requirements

Observed on the verified run (a vLLM + LMCache recurrent-checkpoint
deployment):

- `CACHE_MODE=lmcache`, `LMCACHE_L1_GB=24` (watermark 0.80),
  `LMCACHE_L2_ENABLED=1`, `LMCACHE_L2_GB=128`,
  `LMCACHE_L2_CHECKPOINT_WRITES=on-evict`, `LMCACHE_L2_PRUNE_STALE=1`.
- vLLM flag `--recurrent-checkpoint-policy request_boundaries`.
- No other traffic while the script runs (it asserts the engine is
  idle before every cell and fails INVALID otherwise).

## Running

```sh
# pressure mode (P0..P3): supersession + L1 watermark crossing
python3 supersession_repro.py --model <model> --container <engine> \
    --mode pressure --page-size 3072 --recurrent-groups 7

# restart mode (R0, R1): supersession + clean stop/restart
python3 supersession_repro.py --model <model> --container <engine> \
    --mode restart --page-size 3072 --recurrent-groups 7

# both, with a fresh L1 per cell (the verified configuration)
python3 supersession_repro.py --model <model> --container <engine> \
    --mode all --page-size 3072 --recurrent-groups 7 \
    --restart-between-cells
```

Per cell the script builds a fresh random prompt `P` (token IDs), sends
`A = P` with a 240-token greedy response (publishes A's prompt and
response checkpoints), optionally sends `B = P + [x]` (publishes and
supersedes A's lineage — the response checkpoint is NOT an ancestor of
B, exercising the sibling marking), optionally applies the trigger
(pressure: completing 40k-token fillers until the stock line
"L1 memory usage ... above watermark ...; triggering eviction";
restart: `docker stop -t 60` + `docker start`), clears A's copy from
the GPU KV pool (completing warm filler + two evictors aborted on the
first output token — full prefill allocates their blocks and cycles
the pool's LRU; aborting at the first token publishes nothing), then
sends `A2 = P + response + [y]`.

Exit codes: 0 all cells match, 1 some cell mismatched, 2 some cell
INVALID (precondition not observed — rerun, don't reinterpret).

## Expected output

From the verified pristine run (`result.json`, seed 20260927,
2026-09-26T19:15-20:08 UTC; `result-v1-evictor-hold-diagnosis.json`
records the first attempt whose evict step aborted too early — 10 s
holds allocated only ~33k of the 260k evictor blocks — and diagnosed
it via the stock per-request timing lines):

```
cell                     expected  observed  valid     a2_cached  retrieve misses
P0-control               restored  restored  True          40239  -
P1-supersede             restored  restored  True          40239  -
P2-pressure              restored  restored  True          40239  -
P3-supersede+pressure    failed    failed    True              0  [(40239, 13, 22), (40239, 13, 22), (40000, 13, 22), (40000, 13, 22), (36864, 12, 20), (36864, 12, 20)]
R0-restart-control       restored  restored  True          40239  -
R1-supersede+restart     failed    failed    True              0  [(40239, 13, 22), (40239, 13, 22), (40000, 13, 22), (40000, 13, 22), (36864, 12, 20), (36864, 12, 20)]
```

Serve path per cell, from A2's stock stall-diagnostics line
(`a2_stall` in the result JSON; the discriminator is documented under
Serve-path honesty below):

| cell | engine queue | first output | path |
|---|---|---|---|
| P0 | 0.02 s | 0.19 s | GPU-local |
| P1 | 0.02 s | 0.18 s | GPU-local |
| P2 | 0.21 s | 0.38 s | external |
| R0 | 0.22 s | 0.43 s | external (by construction) |
| P3 / R1 | 0.21 / 0.24 s | (recompute) | external offer |

**P0 and P1 were GPU-served in this run** — the evict step did not
push A's lineage out of the GPU KV pool there — so they are NOT L1
controls in this result, and the supersede-only control ("supersession
alone is harmless") is **not demonstrated on a stock engine by this
run**; it is shown on the instrumented deployment in the main report
(round-15 arms with an external-serve guard). Reading the fingerprint:
a retrieve reports `K of M pages were readable` per rank. At ~40k
tokens the response checkpoint totals 22 pages (14 attention +
7 recurrent-state + 1 auxiliary); the finding cells report 13/22 —
exactly the 9 unique pages missing (7 recurrent + 1 auxiliary +
1 partial-tail). The walk-down then retries 40,000 (13/22) and 36,864
(12/20; block-aligned, 8 unique). A2 recomputes the full prompt from
zero (~9 s vs ~0.4 s restored).

Expected per cell:

| cell | supersede | trigger | expected | fingerprint if failed |
|---|---|---|---|---|
| P0 | no | none | restored | — |
| P1 | yes | none | restored | — |
| P2 | no | pressure | restored | — |
| P3 | yes | pressure | **failed** | 9 unique pages/rank missing (8 if aligned) |
| R0 | no | clean restart | restored | — |
| R1 | yes | clean restart | **failed** | 9 unique pages/rank missing (8 if aligned) |

## Serve-path honesty

On stock builds a *successful* external restore is silent: no log
line, and `vllm:external_prefix_cache_hits_total` does not count
recurrent restores (verified: `cached_tokens=40000` with the counter
flat); `vllm:prefix_cache_hits_total` moves identically for a
GPU-local hit and an external restore. The stock discriminator is the
per-request stall-diagnostics line's **engine queue** field: an
external restore parks the request at the LMCache lookup (~0.2 s
queue, ~0.4 s to first output), while a GPU-local hit shows ~0.02 s /
~0.2 s. The script records both as `a2_stall` per cell;
`--serve-queue-threshold 0.1` marks a restored cell INVALID when its
queue is near zero (observed values: see the table under Expected
output). Control cells therefore cannot always prove which path
served A2:

- R0/R1 cells are structurally external (fresh process, empty prefix
  cache after the restart).
- P2/P3: the ~27 completing fillers cycle the 479k-token KV pool, so
  A's resident lineage is LRU-evicted long before A2.
- P0/P1 relied on the evict step's pool arithmetic, and in the
  verified run it did NOT clear the GPU: both cells were served from
  the engine's own copy (engine queue 0.02 s). Fixing the evict step
  so P0/P1 show the external signature is a known follow-up, not part
  of this result. The finding cells (P3, R1) are immune: their
  expected result — a failed restore — is loud (retrieve-miss +
  restore-failure lines, cached 0).

## Files

- `supersession_repro.py` — the reproducer (stdlib only).
- `test_supersession_repro.py` — stock-line regexes validated against
  captured real lines + the page-count model (`ceil(tokens/page) + 8`;
  131,225 → 51, 39,966 → 22, 36,864 → 20 pages).
- `result.json` — the verified pristine run.
- `result-v1-evictor-hold-diagnosis.json` — first attempt + diagnosis.
- `log-excerpts.md` — trimmed, scrubbed stock log lines per cell.
