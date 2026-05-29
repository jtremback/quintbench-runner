# quintbench-runner

A [Pier](https://github.com/datacurve-ai/pier) subclass of `mini-swe-agent`
that installs [Quint](https://github.com/informalsystems/quint) into the
benchmark sandbox and ships the
[Quint-assisted-implementation skill](https://github.com/jtremback/quint-skill)
into the prompt — so an autonomous coding agent can use formal modelling as a
reasoning tool while solving a long-horizon SWE task.

Target benchmark: [DeepSWE](https://deepswe.datacurve.ai) (113 Harbor-format
tasks across Go / Python / TS / JS / Rust). The runner is harness-agnostic on
the Pier side and only assumes the task format Pier already supports.

## Layout

The runner is deliberately decoupled from the skill itself. Two repos, one-way
dependency (runner → skill):

```
~/projects/quintbench/
├── quint-skill/          (github.com/jtremback/quint-skill — markdown + .qnt)
└── quintbench-runner/    (this repo — Python package, depends on datacurve-pier)
```

The runner reads SKILL.md and `guidelines/` from the skill checkout at agent
construction time, embeds their contents into the install script, and
materializes them at `/opt/quint-skill/` inside the sandbox during install.
**No `git clone` happens inside the sandbox**, so the skill repo can stay
private.

## Install

```bash
pip install datacurve-pier
pip install -e ./quintbench-runner
```

## Run

```bash
pier run -p ./deepswe-datacurve/tasks/pebble-durability-wait-apis \
  --agent quintbench_runner.pier_agent:QuintMiniSweAgent \
  --model anthropic/claude-opus-4-7
```

By default the runner expects the skill at `../quint-skill/` (sibling
directory of this checkout). Override with:

- `QUINT_SKILL_PATH=/path/to/quint-skill` env var, or
- `skill_path=` kwarg via Pier's `AgentConfig.kwargs`.

## What the agent does that vanilla mini-swe-agent doesn't

1. **Install phase** — after mini-swe-agent's normal install, runs two
   additional root steps: (a) `apt/apk/yum/dnf` install Node + npm, then
   `npm install -g @informalsystems/quint`; (b) heredoc every skill file
   into `/opt/quint-skill/`.
2. **Network allowlist** — extends mini-swe-agent's with `registry.npmjs.org`
   and `nodejs.org` so the install can reach npm.
3. **Prompt** — overrides `render_instruction` to prepend SKILL.md to every
   task instruction. The skill is in the agent's context from turn 1.
