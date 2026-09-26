# Pristine-run log excerpts (scrubbed)

Engine log 2026-09-26T19:15-20:08 UTC, stock image files only (three runtime-patched files reverted for the run; the window contains zero instrumentation lines). Request IDs and host paths scrubbed. Line numbers as deployed in image `karmic-kraken-beta`.

## P0-control

```
2026-09-26T19:23:11.828910835Z INFO 09-26 19:23:11 [stall_diagnostics.py:189] Request cmpl-<scrubbed> finished after 0.20 s (arrived 19:23:11.629), HTTP receipt to body read 0.00 s, body read to rendering 0.00 s, arrival to generator start 0.00 s, input processing 0.00 s, engine submission 0.00 s, submission to first output 0.19 s, first to final output 0.00 s, final output to stream end 0.00 s, engine queue 0.02 s, prompt 40241 tokens (40239 cached), 1 generated, finish reason length
```

## P1-supersede

```
2026-09-26T19:30:53.936492428Z INFO 09-26 19:30:53 [stall_diagnostics.py:189] Request cmpl-<scrubbed> finished after 0.18 s (arrived 19:30:53.752), HTTP receipt to body read 0.00 s, body read to rendering 0.00 s, arrival to generator start 0.00 s, input processing 0.00 s, engine submission 0.00 s, submission to first output 0.18 s, first to final output 0.00 s, final output to stream end 0.00 s, engine queue 0.02 s, prompt 40241 tokens (40239 cached), 1 generated, finish reason length
```

## P2-pressure

```
2026-09-26T19:41:23.440017682Z L1 memory usage 0.82 above watermark 0.80; triggering eviction.
2026-09-26T19:43:40.615570596Z INFO 09-26 19:43:40 [stall_diagnostics.py:189] Request cmpl-<scrubbed> finished after 0.39 s (arrived 19:43:40.226), HTTP receipt to body read 0.00 s, body read to rendering 0.00 s, arrival to generator start 0.00 s, input processing 0.00 s, engine submission 0.00 s, submission to first output 0.38 s, first to final output 0.00 s, final output to stream end 0.00 s, engine queue 0.21 s, prompt 40241 tokens (40239 cached), 1 generated, finish reason length
```

## P3-supersede+pressure

```
2026-09-26T19:54:32.313051396Z L1 memory usage 0.83 above watermark 0.80; triggering eviction.
2026-09-26T19:56:49.895712648Z Checkpoint retrieve of 40239 tokens for rank 1 missed; its checkpoint is no longer listed after 0.1 s: 13 of 22 pages were readable
2026-09-26T19:56:49.899790824Z Checkpoint retrieve of 40239 tokens for rank 0 missed; its checkpoint is no longer listed after 0.1 s: 13 of 22 pages were readable
2026-09-26T19:56:49.902250730Z Recurrent checkpoint restore of 40239 tokens failed for request cmpl-<scrubbed> on ranks [0, 1] after 0.1 s
2026-09-26T19:56:49.902344277Z Recurrent checkpoint restore of 40239 tokens missed for request cmpl-<scrubbed>; looking up a shorter checkpoint (attempt 2)
2026-09-26T19:56:59.012674319Z INFO 09-26 19:56:59 [stall_diagnostics.py:189] Request cmpl-<scrubbed> finished after 9.22 s (arrived 19:56:49.791), HTTP receipt to body read 0.00 s, body read to rendering 0.00 s, arrival to generator start 0.00 s, input processing 0.00 s, engine submission 0.00 s, submission to first output 9.21 s, first to final output 0.00 s, final output to stream end 0.00 s, engine queue 0.21 s, prompt 40241 tokens (0 cached), 1 generated, finish reason length
```

## R0-restart-control

```
2026-09-26T19:57:18.812335278Z Writing 292 current checkpoint pages to L2 before shutdown (budget 30 s)
2026-09-26T19:57:22.822478412Z Shutdown checkpoint flush completed in 4.0 s
2026-09-26T20:02:18.750729047Z INFO 09-26 20:02:18 [stall_diagnostics.py:189] Request cmpl-<scrubbed> finished after 0.44 s (arrived 20:02:18.308), HTTP receipt to body read 0.00 s, body read to rendering 0.00 s, arrival to generator start 0.00 s, input processing 0.00 s, engine submission 0.00 s, submission to first output 0.43 s, first to final output 0.00 s, final output to stream end 0.00 s, engine queue 0.22 s, prompt 40241 tokens (40239 cached), 1 generated, finish reason length
```

## R1-supersede+restart

```
2026-09-26T20:02:43.814094081Z Writing 66 current checkpoint pages to L2 before shutdown (budget 30 s)
2026-09-26T20:02:45.215529373Z Shutdown checkpoint flush completed in 1.4 s
2026-09-26T20:07:45.178711746Z Checkpoint retrieve of 40239 tokens for rank 1 missed; its checkpoint is no longer listed after 0.1 s: 13 of 22 pages were readable
2026-09-26T20:07:45.182266599Z Checkpoint retrieve of 40239 tokens for rank 0 missed; its checkpoint is no longer listed after 0.1 s: 13 of 22 pages were readable
2026-09-26T20:07:45.184888252Z Recurrent checkpoint restore of 40239 tokens failed for request cmpl-<scrubbed> on ranks [0, 1] after 0.1 s
2026-09-26T20:07:54.282770968Z INFO 09-26 20:07:54 [stall_diagnostics.py:189] Request cmpl-<scrubbed> finished after 9.25 s (arrived 20:07:45.034), HTTP receipt to body read 0.00 s, body read to rendering 0.00 s, arrival to generator start 0.00 s, input processing 0.00 s, engine submission 0.00 s, submission to first output 9.24 s, first to final output 0.00 s, final output to stream end 0.00 s, engine queue 0.24 s, prompt 40241 tokens (0 cached), 1 generated, finish reason length
```

