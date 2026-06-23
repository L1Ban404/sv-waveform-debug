# Verilog/SystemVerilog debug methodology

## Evidence order

1. Confirm waveform provenance: simulation command, seed, test, simulator, timescale, and source revision.
2. Confirm clock/reset behavior and testbench sampling discipline.
3. Locate the first divergence, not merely the final visible failure.
4. Trace from interface signal to owning instance, local signal, procedural block, and upstream dependency.
5. Test one causal hypothesis with a smaller time window and signal set.

## Sequential logic

- Evaluate nonblocking assignments using pre-edge right-hand-side values.
- Check reset polarity, synchronous/asynchronous semantics, enable priority, and overlapping assignments.
- Distinguish a register's sampled input at the active edge from its visible post-edge output.
- Inspect counter width, wraparound, enum encoding, and unreachable/default state handling.

## Combinational logic

- Check incomplete assignment, unintended latch behavior, case coverage, priority, and signed/unsigned width extension.
- Track `X/Z` propagation through conditions, case equality, reductions, array indexes, and packed structs.
- Look for delta-cycle/testbench races when values change at the same timestamp.

## Ready/valid and request/response protocols

- Count a transfer only when `valid && ready` is true in the same cycle.
- Require `valid` and payload stability while `valid && !ready`.
- Check whether state advances on intent (`valid`) instead of acceptance (`valid && ready`).
- For responses, correlate tags/IDs, ordering guarantees, outstanding entries, and cancellation/flush rules.
- Trace backpressure from the blocked sink toward the source before calling a stall a deadlock.

## Pipelines, flushes, and hazards

- Track one transaction by stable identity: PC, tag, ID, opcode, address, or payload hash.
- Check stage entry/exit handshakes, hold behavior, bubble insertion, flush priority, and payload alignment.
- Separate the redirecting/exception-producing transaction from younger transactions that must be killed.
- Verify forwarding availability, selection priority, load-use timing, and writeback visibility.

## Memories and buses

- Separate request acceptance from response completion.
- Check byte enables, alignment, burst length, sign extension, response error, and address/data pairing.
- For multiple outstanding operations, verify allocation, tag reuse, response routing, and retirement order.

## Clock/reset and CDC

- Identify all clock domains and reset release edges.
- Do not infer CDC safety from a clean digital waveform alone; inspect synchronizers and constraints.
- Look for pulse loss, reconvergence, unsynchronized multi-bit data, and reset-domain crossing behavior.

## Source navigation

Search `.sv` and `.v` for the authority result's module type and local signal. Include packages, interfaces, typedefs, parameters, generate blocks, assertions, bind files, and the testbench. Treat preprocessed/generated RTL as authoritative only for the simulation actually producing the waveform.

## Confidence

- High: the waveform shows the violating transition and the source contains the exact enabling condition.
- Medium: evidence isolates a module/condition but one internal dependency is absent from the trace.
- Low: only downstream symptoms are visible or source/waveform revisions may differ.
