# sv-waveform-debug

[![Smoke test](https://github.com/L1Ban404/sv-waveform-debug/actions/workflows/smoke.yml/badge.svg)](https://github.com/L1Ban404/sv-waveform-debug/actions/workflows/smoke.yml)

A Codex skill and portable CLI for evidence-driven Verilog/SystemVerilog debugging from VCD or FST waveforms.

Version 0.2 turns waveform analysis into an iterative investigation: discover hierarchy and signals, query compact windows, compare good and bad traces, map activity to RTL ownership, test hypotheses, and close the loop with an authorized RTL fix and regression.

## Capabilities

- Pure Python 3.10+ streaming VCD metadata and change queries
- FST through compatible `pywellen` or cached `fst2vcd` conversion
- Waveform-only scope, signal, point, and bounded-window queries
- Physical time units and clock-edge samples
- Good/bad trace first-divergence analysis
- Verilog/SystemVerilog discovery plus `.f/.flist`, include, define, and exclude inputs
- RTL hierarchy authority and source-navigation context
- Bounded JSON evidence designed for an LLM context window

The adapter is simulator- and architecture-independent.

## Install as a project skill

```bash
git submodule add https://github.com/L1Ban404/sv-waveform-debug.git \
  .codex/skills/sv-waveform-debug
git submodule update --init --recursive
```

SSH works as well:

```bash
git submodule add git@github.com:L1Ban404/sv-waveform-debug.git \
  .codex/skills/sv-waveform-debug
git submodule update --init --recursive
```

## Quick start

```bash
CLI=.codex/skills/sv-waveform-debug/scripts/wave_debug.py

python "$CLI" doctor
python "$CLI" inspect --json
python "$CLI" scopes --json
python "$CLI" signals --scope tb.dut --match valid --json
python "$CLI" probe --around 420ns --radius 30ns \
  --scope tb.dut --signal tb.dut.clk --clock tb.dut.clk
```

Map selected activity back to RTL:

```bash
python "$CLI" authority --waveform build/fail.fst \
  --filelist sim/files.f --top tb_top
python "$CLI" probe --waveform build/fail.fst --around 420ns --radius 20ns \
  --scope tb_top.dut --match state --filelist sim/files.f --top tb_top
```

Compare traces:

```bash
python "$CLI" compare passing.vcd failing.vcd --scope tb.dut
```

Run `python "$CLI" <command> --help` for all options. Times accept integer waveform ticks or physical values such as `42ns` and `1.5us`.

## Backends

VCD requires only Python 3.10 or newer. FST uses the first available path:

1. a compatible installed `pywellen`;
2. the pinned engine's compatible bundled binary;
3. `fst2vcd`, commonly provided by GTKWave or OSS CAD Suite.

`doctor --json` reports exact capabilities and remediation. The pinned [`trace1729/hardware-debug-skill`](https://github.com/trace1729/hardware-debug-skill) submodule remains the RTL-authority engine.

## Development

```bash
python -m unittest discover -s tests -p 'test_*.py'
python tests/test_smoke.py
python -m py_compile scripts/wave_debug.py scripts/wave_debug_lib/*.py
python -m pip install -r requirements-dev.txt
python tests/validate_skill.py
```

CI checks the portable VCD path on Python 3.10–3.13 and exercises both direct and converted FST paths on Python 3.12.

## License

Apache-2.0. See [LICENSE](LICENSE).
