# Traffic generation and measurement (Stage 3)

Code: `backend/app/traffic/` (measurement only; it knows nothing about mobility protocols).
Schemas: `schemas/`. Example raw data: `examples/results/`.

## Tools
```bash
# receiver (echoes by default); writes received.csv + receiver.meta.json
python -m app.traffic.receiver --scenario-id S --run-id R --out-dir data/results --bind 10.10.1.2 --port 5005
# sender; writes sent.csv, echoes.csv, sender.meta.json
python -m app.traffic.sender 10.10.1.2:5005 --scenario-id S --run-id R --out-dir data/results --rate 100 --duration 20
# metrics from the raw files -> <run>/metrics.json
python -m app.traffic analyze data/results/S/R          # add --json for everything
# handoff scenario inside the Stage 2 topology (needs sudo; see PRIVILEGED_OPERATIONS.md)
python -m app.traffic scenario config/scenarios/handoff_a_to_b.yaml --execute
```
(run with `PYTHONPATH=backend`, or `cd backend`). Sender and receiver of one run share
`--scenario-id/--run-id/--out-dir`; files are created exclusively and **never overwritten**.
Both stop on `SIGINT/SIGTERM` or when a `--stop-file` appears, and finalise their metadata.

## Raw data (schema version 1; `schemas/csv_columns.json`, `run_meta.schema.json`)
| File | Written by | One row per |
|---|---|---|
| `sent.csv` | sender | probe sent (`seq, scheduled_mono_ns, send_mono_ns, send_wall_ns, size_bytes, send_errno`) |
| `echoes.csv` | sender | echo received (`seq, recv_mono_ns, recv_wall_ns, size_bytes`) |
| `received.csv` | receiver | valid probe received, duplicates kept (`seq, session_id, recv_*, hdr_send_*, size, src`) |
| `events.csv` | orchestrator / proxy | `scenario_start/end`, `handoff_start/end` markers |
| `sender.meta.json`, `receiver.meta.json` | each tool | scenario_id, run_id, session_id, params, counters, clock info, status, start/end times |
| `scenario.json` | orchestrator | spec + clock domain (`boot_id`) used to trust `events.csv` |
| `metrics.json` | `analyze` | derived values only |

All timestamps are integers in ns. Every packet carries `seq` (from 0), `session_id`, and the sender's monotonic and
wall stamps. Meta `status` is `running` until finalised, so a killed process is recognisable (`completed`,
`interrupted` = clean stop by signal/stop-file, `failed`).

## Metric definitions
- **Loss** = probes the sender logged as sent OK (`send_errno == 0`) whose `seq` never reached the receiver, over probes sent OK.
  Local send errors are reported separately. Without a sender log only a *lower bound* from receiver-side gaps is given
  (`basis: receiver_gaps`, `exact: false`). `exact: true` requires both sides finalised, a complete sender log and no
  received seq missing from it. Also: loss bursts (runs of consecutive lost seqs), duplicates, reordered packets.
- **Inter-arrival gaps**: differences between consecutive first-arrival times at the receiver (monotonic), with p50/p95/p99/max and
  the seqs around the largest gap.
- **RTT** = echo receive time - send time, both read from the *sender's* monotonic clock (single clock, always valid).
- **One-way delay** = receiver monotonic - sender header monotonic. Reported **only** when both metadata files carry the same
  `boot_id` (same kernel boot, hence the same CLOCK_MONOTONIC); otherwise `owd_ms` is null with a reason.
- **Jitter**: RFC 3550 interarrival jitter on transit-time differences (a constant clock offset cancels), plus mean |ΔRTT| of consecutive probes.
- **Throughput**: offered rate (sender) and goodput (receiver) = bytes / time span, and a per-bin goodput time series between the first and last arrival.
  With fixed-rate probes this is *load*, not link capacity.
- **Handoffs** (needs `events.csv` and a proven shared clock domain): the loss burst overlapping `[handoff_start - margin, handoff_end + margin]`
  (default margin 250 ms), `interruption_ms` = arrival time of the first delivered seq after the burst minus that of the last delivered seq before it,
  largest inter-arrival gap in the window, median RTT of up to 10 probes before/after, `recovered`. `action_duration_ms` is how long the
  command took, *not* the service interruption.

## Not fabricating data
Missing or unprovable values are `null` (with `warnings`/`*_unavailable_reason`), never zero or interpolated. Unterminated last CSV lines
(partial writes) and malformed rows are dropped and counted in `metrics.json -> inputs`. Empty throughput bins are reported only *between*
the first and last arrival (silence that was actually measured). Late arrivals after a receiver stopped, or probes whose log rows were lost
in a crash, make loss inexact; this is flagged, not corrected.

## Clock and measurement limitations
1. Elapsed times use `time.monotonic_ns()` (CLOCK_MONOTONIC): unaffected by wall-clock steps, but NTP may *slew* its rate (tiny, ~ppm).
   It is shared by all processes and network namespaces of one kernel boot, and **not** comparable across machines.
2. Wall-clock stamps (`time.time_ns()`) are for lining runs up with calendar time. They can jump and differ between hosts; no delay is derived from them.
3. One-way delay therefore only holds for same-host experiments (this netns lab). On separate hosts use RTT, or jitter (offset-free).
4. Stamps are taken in user space (Python): they include scheduling and interpreter latency (typically tens of µs, more under load) and the
   resolution of the clock (`clock.monotonic_resolution_ns` in the metadata). No kernel/NIC timestamping is used.
5. Sender pacing uses absolute deadlines (no cumulative drift), but a busy host can send late; `scheduled_mono_ns` vs `send_mono_ns`
   (`sender_schedule_slip_ms`) exposes this. Python limits practical rates to a few thousand packets/s.
6. UDP gives no delivery feedback: a probe that is "lost" may simply be late beyond the receiver's stop time; echo loss and probe loss are different paths.
7. Handoff times are orchestrator-side instants (before/after the `ip link set` command); the kernel acts somewhere in between.
8. The loopback proxy (`loopback_proxy.py`) models loss/delay/blackout in user space for tests and examples; its numbers say nothing about `tc netem`.
9. Crash behaviour: CSVs are flushed every 0.2 s, so `SIGKILL` can lose up to ~0.2 s of the *sender's* log while the receiver still logged those
   packets (counted as `received_not_in_sent_log`). Metadata stays `running`.

## Separation from protocols
`traffic/` contains no protocol logic. Later protocol code may run the sender/receiver as processes (or read their files); nothing in `traffic/`
imports from it. `scenario.py` is the only module that touches `netem` (to flip interfaces and start the tools inside namespaces).
