# sv-waveform-debug

[![Smoke test](https://github.com/L1Ban404/sv-waveform-debug/actions/workflows/smoke.yml/badge.svg)](https://github.com/L1Ban404/sv-waveform-debug/actions/workflows/smoke.yml)

A Codex skill for debugging Verilog and SystemVerilog from FST/VCD waveform evidence and HDL source.

It discovers waveform and source inputs, infers likely top modules, converts incompatible FST traces when needed, builds an RTL hierarchy database, and extracts bounded debug packets that an AI coding agent can correlate with RTL and testbench behavior.

## Features

- Verilog (`.v`) and SystemVerilog (`.sv`) source discovery
- FST and VCD waveform discovery and querying
- Automatic top-module candidate inference
- Optional FST-to-VCD compatibility conversion through `fst2vcd`
- Waveform signal to RTL hierarchy/source mapping
- Bounded time-window packets and point-in-time signal queries
- Reusable debugging guidance for sequential logic, FSMs, protocols, pipelines, memories, reset, CDC, and X propagation

## Install as a project skill

From the target project's root:

```bash
git submodule add git@github.com:L1Ban404/sv-waveform-debug.git \
  .codex/skills/sv-waveform-debug
git submodule update --init --recursive
```

For an HTTPS checkout:

```bash
git submodule add https://github.com/L1Ban404/sv-waveform-debug.git \
  .codex/skills/sv-waveform-debug
git submodule update --init --recursive
```

Codex discovers the skill from `.codex/skills/sv-waveform-debug/SKILL.md`.

## Requirements

- Python 3.12 for the binary bundled by the pinned upstream engine, or a compatible installed `pywellen`
- `fst2vcd` for FST files that use attributes unsupported by `pywellen` (commonly provided by GTKWave or OSS CAD Suite)
- Git submodules initialized recursively

No Python package installation is required when the bundled Python 3.12 `pywellen` binary is compatible with the host.

## Usage

Run commands from the HDL workspace root.

Discover inputs:

```bash
python .codex/skills/sv-waveform-debug/scripts/wave_debug.py inspect
```

Resolve ambiguity explicitly:

```bash
python .codex/skills/sv-waveform-debug/scripts/wave_debug.py inspect \
  --waveform build/run.fst --source-root rtl --top tb_top
```

Build RTL hierarchy ownership:

```bash
python .codex/skills/sv-waveform-debug/scripts/wave_debug.py authority \
  --waveform build/run.fst --source-root . --top tb_top
```

Query a bounded window:

```bash
python .codex/skills/sv-waveform-debug/scripts/wave_debug.py packet \
  --window 42 --window-len 1000 --focus-scope TOP.tb_top.dut
```

Query one signal:

```bash
python .codex/skills/sv-waveform-debug/scripts/wave_debug.py signal \
  --signal TOP.tb_top.dut.valid_o --time 42000
```

Generated caches and packets are written to `build/wave-debug/` by default.

## How it works

The project-specific wrapper provides discovery, compatibility handling, cache placement, and source-path restoration. The pinned [`trace1729/hardware-debug-skill`](https://github.com/trace1729/hardware-debug-skill) submodule provides waveform parsing and RTL authority extraction.

The adapter intentionally does not encode a specific processor, pipeline, simulator, or test framework.

## Contributing

Keep changes simulator- and architecture-independent. Test both explicit input paths and automatic discovery. For waveform parser changes, contribute upstream when the behavior belongs to the underlying engine.

Run the repository smoke test with:

```bash
python tests/test_smoke.py
```

## License

Apache-2.0. See [LICENSE](LICENSE).
