# Verilog/SystemVerilog debug methodology

## Evidence discipline

1. Record simulator, test, seed, source revision, waveform path, timescale, and dump scope.
2. Find the earliest divergence or violated invariant, not only the final failure.
3. Label waveform values as observed, RTL interpretation as inferred, and causal explanations as hypotheses.
4. Test one hypothesis with the smallest additional signal set and time window.
5. Prefer an exact contradiction over many supporting correlations.

## Time and sequential semantics

- Interpret nonblocking assignment right-hand sides using pre-edge values and outputs using post-NBA values.
- Distinguish clock-edge sampling, delta-cycle ordering, and testbench observation regions.
- Check reset polarity, synchronous/asynchronous behavior, release edge, enable priority, and multiple assignments.
- Verify counter width, wraparound, enum encoding, default states, and simultaneous set/clear conditions.
- When edge-sampled output is ambiguous, inspect values immediately before and after the edge.

## Combinational logic and unknowns

- Check incomplete assignment, latch inference, case coverage, priority, signedness, truncation, and extension.
- Track `X/Z` through conditions, reductions, case equality, array indexes, packed types, and tri-state connections.
- Treat an unknown condition as evidence of missing initialization or conflicting drive until disproven.
- Look for testbench/DUT races when drive and sample events share a timestamp.

## Ready/valid and request/response

- Count a transfer only when `valid && ready` is true on the same sampling edge.
- Require valid and payload stability while `valid && !ready` unless the protocol explicitly says otherwise.
- Check whether state advances on intent (`valid`) rather than acceptance (`valid && ready`).
- Separate request acceptance from response completion; correlate tags, ordering, cancellation, and outstanding entries.
- Trace backpressure from the blocked consumer toward the producer before declaring deadlock.

## Pipelines, FSMs, and flushes

- Track one transaction using PC, tag, ID, opcode, address, or a payload fingerprint.
- Check stage fire, hold, bubble insertion, payload/control alignment, redirect priority, and kill boundaries.
- Separate the transaction causing a redirect or exception from younger transactions that must be discarded.
- For hazards, verify availability timing and selection priority, not just register-number matches.
- For FSMs, identify the transition predicate, pre-state, post-state, and outputs derived from each.

## Memories and buses

- Check alignment, byte enables, burst size, sign extension, response errors, and address/data pairing.
- For multiple outstanding operations, verify allocation, full/empty behavior, tag reuse, response routing, and retirement order.
- Distinguish accepted stores from architecturally committed stores when precise exceptions matter.
- Inspect memory initialization and out-of-range indexes before attributing `X` to datapath logic.

## Clock, reset, and CDC

- Identify every clock and reset domain and the release sequence between them.
- Look for missing synchronizers, pulse loss, reconvergence, unsynchronized buses, and reset-domain crossings.
- A clean digital waveform cannot prove metastability safety. Confirm CDC structures and timing constraints in source.
- Do not compare raw ticks between traces with different timescales; compare physical time.

## Source correlation and confidence

- Include packages, interfaces, typedefs, parameters, generate blocks, assertions, bind files, and testbench drive code.
- Use the exact compiled/generated RTL when it differs from handwritten source.
- Treat authority matches as hierarchy ownership, not proof that the named signal caused the failure.
- High confidence requires the violating transition and its enabling source condition.
- Medium confidence isolates a module/condition but lacks an internal dependency.
- Low confidence means only downstream symptoms are visible or waveform/source provenance is uncertain.

## Repair verification

- Add a regression that reproduces the earliest incorrect behavior, not merely the final error message.
- Make the smallest change consistent with the evidence.
- Run the focused regression, neighboring protocol or stage tests, and then the appropriate broader suite.
- Re-open the post-fix waveform when the fix changes timing, arbitration, reset, or handshake behavior.
