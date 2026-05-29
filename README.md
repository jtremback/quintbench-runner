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
  --agent-import-path quintbench_runner.pier_agent:QuintMiniSweAgent \
  --model anthropic/claude-opus-4-7 \
  --env docker
```

Or use the bundled one-task smoke script:

```bash
ANTHROPIC_API_KEY=<your-key> ./scripts/smoke.sh
```

By default the runner expects the skill at `../quint-skill/` (sibling
directory of this checkout). Override with:

- `QUINT_SKILL_PATH=/path/to/quint-skill` env var, or
- `skill_path=` kwarg via Pier's `AgentConfig.kwargs`.

## What the agent does that vanilla mini-swe-agent doesn't

1. **Install phase** — after mini-swe-agent's normal install, runs two
   additional root steps: (a) install `nodejs` via the host's package
   manager (NodeSource's bundle includes a compatible `npm`), then
   `npm install -g @informalsystems/quint`; (b) decode-and-extract a
   tar.gz of the skill files into `/opt/quint-skill/`.
2. **Network allowlist** — extends mini-swe-agent's with `registry.npmjs.org`
   and `nodejs.org` so the install can reach npm.
3. **Prompt** — overrides `render_instruction` to prepend SKILL.md to every
   task instruction. The skill is in the agent's context from turn 1.

## Smoke test status

End-to-end install verified against `pebble-durability-wait-apis` on
`--env docker` (May 2026). The full chain — apt-get nodejs, npm install
quint, base64+tar skill materialization, mini-swe-agent install, prompt
template injection, agent launch — completes cleanly. The agent then
fails predictably at the LLM call only because we ran with a fake API key.

## Known install caveats

Real gotchas the smoke loop surfaced; the current code handles them, but
keep these in mind if you adapt to new base images.

- **Dockerfile-line size limit (64 KB).** Pier inlines each `InstallStep`
  as a single `RUN ["/bin/bash","-c","..."]` line, and BuildKit caps
  individual lines at 64 KB. The skill is ~140 KB raw, so we tar+gzip on
  the host, base64-encode the blob, and decode-and-extract in the sandbox.
  ~52 KB on the wire after compression.
- **Debian `nodejs` vs. `npm` conflict on NodeSource-preconfigured images.**
  The DeepSWE base images ship NodeSource's `nodejs 24.x`, which bundles a
  compatible `npm`. Trying to additionally install Debian's `npm` package
  fails with `nodejs : Conflicts: npm`. We install only `nodejs` on apt-get
  systems and verify `npm` ends up on PATH; loud failure if not.

## Known limitations

- **Single base image verified.** Only `mars-base` (the pebble task's base)
  has been smoke-tested. Other DeepSWE images may surface install issues
  we haven't seen yet — fail-loud messages will tell us what's wrong.
- **Skill embedded at construction time.** Edits to the skill during a long
  batch only affect fresh agent instances, not running ones.
- **No automatic NodeSource setup** for images that lack it preconfigured.
  We'll add it the first time we hit such an image.
