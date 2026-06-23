---
name: sv-waveform-debug
description: Debug and explain Verilog or SystemVerilog hardware using FST/VCD waveform evidence together with RTL and testbench source. Use for simulation failures, protocol violations, pipeline stalls, state-machine bugs, data/control mismatches, X propagation, reset/clock issues, or any request to locate a hardware root cause from waveforms. Automatically discover waveform and HDL inputs when possible, query bounded time windows, map signals back to RTL hierarchy, and support both .fst and .vcd files.
---

# SystemVerilog Waveform Debug

Debug from observable behavior toward the earliest causal RTL transition. Keep waveform facts, source interpretation, and hypotheses distinct.

## Discover inputs

Run from the HDL workspace root. Prefer explicit paths when the user provides them; otherwise let the wrapper discover the newest FST/VCD, Verilog/SystemVerilog sources, and candidate top modules.

```bash
python .codex/skills/sv-waveform-debug/scripts/wave_debug.py inspect
```

Use explicit inputs to resolve ambiguity:

```bash
python .codex/skills/sv-waveform-debug/scripts/wave_debug.py inspect \
  --waveform build/run.fst --source-root rtl --top tb_top
```

Do not guess when multiple plausible waveforms or top modules remain. Report the candidates and ask for the missing discriminator.

## Build RTL authority

Build hierarchy ownership before correlating waveform paths with source declarations:

```bash
python .codex/skills/sv-waveform-debug/scripts/wave_debug.py authority \
  --waveform build/run.fst --source-root . --top tb_top
```

The wrapper scans `.sv` and `.v`, caches artifacts under `build/wave-debug/`, and reuses them until inputs change. FST is accepted directly; when `fst2vcd` is available, it is converted to a cached VCD to handle parser-incompatible FST attributes.

## Query evidence

Query a bounded time window rather than dumping the whole trace:

```bash
python .codex/skills/sv-waveform-debug/scripts/wave_debug.py packet \
  --window 42 --window-len 1000 --focus-scope TOP.tb_top.dut
```

Query one known signal at an exact simulation timestamp:

```bash
python .codex/skills/sv-waveform-debug/scripts/wave_debug.py signal \
  --signal TOP.tb_top.dut.valid_o --time 42000
```

Pass the same `--waveform`, `--source-root`, `--top`, and `--workspace` options when auto-discovery is ambiguous.

## Analyze

Read [references/debug-methodology.md](references/debug-methodology.md) for protocol-specific probes and evidence rules.

1. Establish timescale, clocks, reset polarity, and the first divergence time.
2. Start at the failing output or assertion and trace its combinational and sequential fan-in backward.
3. Compare waveform transitions with the exact `always_ff`, `always_comb`, assignment, and instance connections that own them.
4. Separate testbench drive errors from DUT errors; inspect stimulus and sampling edges.
5. Check unknown values explicitly. Never coerce `X/Z` to zero in reasoning.
6. Narrow the window and signal set after each hypothesis. Prefer evidence that can falsify the hypothesis.
7. Recommend an RTL fix only when waveform timing and source semantics agree; otherwise identify the next bounded probe.

## Report

Provide:

- `Phenomenon`: first incorrect or missing transition, with timestamp/cycle.
- `Root cause`: module, state/condition, and bug class; label unproven conclusions as hypotheses.
- `Evidence`: minimal signal relationships plus exact RTL/testbench paths.
- `Fix or next probe`: concrete RTL change at high confidence, otherwise a precise signal/window query.

Avoid exhaustive signal inventories, raw cycle dumps, and large RTL excerpts.

## Engine boundary

Use `vendor/hardware-debug-skill` as the pinned parsing and RTL-authority engine derived from `trace1729/hardware-debug-skill`. Keep generic adapter behavior in this skill and do not modify the submodule.
