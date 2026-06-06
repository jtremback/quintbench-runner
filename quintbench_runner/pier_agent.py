"""Quint-augmented mini-swe-agent for Pier.

Subclass of `MiniSweAgent` that:
  1. installs `quint` (via apt/apk/yum + npm) into the sandbox during install;
  2. materializes the Quint skill files into /opt/quint-skill/ in the sandbox;
  3. prepends SKILL.md content to every task instruction so the agent sees the
     skill in its prompt without needing a separate template file.

The skill itself lives in a separate repo (github.com/jtremback/quint-skill).
This runner reads the skill files from a local checkout — by default the
sibling directory ``../quint-skill/`` relative to this package's source
checkout, or anywhere else if ``QUINT_SKILL_PATH`` is set or ``skill_path=``
is passed to the constructor.

Loaded via Pier's import-path mechanism (no fork of Pier required):

    pier run -p path/to/tasks \\
        --agent quintbench_runner.pier_agent:QuintMiniSweAgent \\
        --model anthropic/claude-opus-4-7 \\
        --env modal
"""

from __future__ import annotations

import base64
import io
import os
import shlex
import tarfile
from pathlib import Path

from pier.agents.installed.mini_swe_agent import MiniSweAgent
from pier.models.agent.install import AgentInstallSpec, InstallStep
from pier.models.agent.network import NetworkAllowlist


# ---------------------------------------------------------------------------
# Where the skill lives inside the sandbox after the install phase.
# Must match the absolute paths embedded in SKILL.md (`/opt/quint-skill/...`).
# ---------------------------------------------------------------------------
SKILL_DEST = "/opt/quint-skill"

# ---------------------------------------------------------------------------
# Quint + its rust evaluator.
# quint is pinned so the evaluator version it expects matches the musl binary
# we pre-place. The musl (statically-linked) evaluator avoids the default
# rust binary's two showstoppers in the sandbox: it's normally downloaded from
# GitHub on first run (air-gapped at task time) and is glibc-linked against a
# newer glibc than the base images have. Static + pre-placed = fast rust
# backend, offline, on any base image. QUINT_HOME points quint at it.
# ---------------------------------------------------------------------------
QUINT_VERSION = "0.32.0"
EVALUATOR_VERSION = "v0.6.0"
QUINT_HOME_DIR = "/opt/quint-home"
EVALUATOR_URL = (
    "https://github.com/jtremback/quintbench-runner/releases/download/"
    "quint-evaluator-v0.6.0-musl/quint_evaluator_musl"
)

# Prepended to the instruction when force_quint=True. This is the "forced" arm:
# it removes the agent's triage choice so every trial actually exercises Quint
# (eliminating the invocation-noise and self-selection confound that make the
# voluntary-use arm unmeasurable). Intended for the temporal-amenable task
# subset only — forcing a spec on a task with no real state/ordering property
# just wastes the budget.
_FORCE_QUINT_PREAMBLE = """\
# REQUIRED: model this task in Quint before writing code

This task has been pre-selected as one where formal modeling is expected to
help. For this task the "decide whether to model" guidance below does **not**
apply — modeling is mandatory. Before writing any implementation code you MUST:

1. Write a Quint spec capturing the core state / ordering / concurrency
   property of the behavior you are implementing (the subtle slice — not the
   whole feature).
2. `quint typecheck` it, then check its invariant(s) and/or witness(es) with
   `quint run`.
3. Only then implement the code, following the design the model validated.

Do not skip the Quint step, and do not treat it as optional. Follow the skill
below for how to write and run the spec.

---

"""


# ---------------------------------------------------------------------------
# Locating the skill on the host.
# ---------------------------------------------------------------------------
_PACKAGE_DIR = Path(__file__).resolve().parent

# Default layout assumes the runner repo and the skill repo are siblings:
#
#     ~/projects/quintbench/
#     ├── quint-skill/         (skill content)
#     └── quintbench-runner/   (this package)
#
# Override via the ``QUINT_SKILL_PATH`` env var or ``skill_path=`` kwarg.
_DEFAULT_SKILL_PATH = _PACKAGE_DIR.parent.parent / "quint-skill"


def _resolve_skill_path(explicit: str | Path | None) -> Path:
    """Find the skill repo on disk. Loud failure if it isn't where we expect."""
    if explicit is not None:
        path = Path(explicit).expanduser().resolve()
    elif env_value := os.environ.get("QUINT_SKILL_PATH"):
        path = Path(env_value).expanduser().resolve()
    else:
        path = _DEFAULT_SKILL_PATH.resolve()

    if not (path / "SKILL.md").is_file():
        raise RuntimeError(
            f"Quint skill not found at {path}. Either clone "
            "github.com/jtremback/quint-skill as a sibling of this runner repo, "
            "set QUINT_SKILL_PATH, or pass skill_path= to QuintMiniSweAgent."
        )
    if not (path / "guidelines").is_dir():
        raise RuntimeError(
            f"Quint skill at {path} is missing guidelines/. Likely an "
            "incomplete checkout."
        )
    return path


# ---------------------------------------------------------------------------
# Install script builders.
# ---------------------------------------------------------------------------


def _install_node_and_quint_script() -> str:
    """Install Node + quint, then place a musl-static rust evaluator so
    `quint run` works fast and offline on any base image.

    Config (quint version, evaluator URL, QUINT_HOME) is injected as shell
    variables in a header so the body can stay a plain raw string (it contains
    shell and Quint braces that would clash with str.format/f-strings).
    """
    header = (
        f"QUINT_VERSION={shlex.quote(QUINT_VERSION)}\n"
        f"QUINT_HOME_DIR={shlex.quote(QUINT_HOME_DIR)}\n"
        f"EVALUATOR_VERSION={shlex.quote(EVALUATOR_VERSION)}\n"
        f"EVALUATOR_URL={shlex.quote(EVALUATOR_URL)}\n"
    )
    body = r"""
# Install Node (+ curl). On Debian/Ubuntu images with NodeSource preconfigured
# (the DeepSWE base images), `apt-get install -y nodejs` pulls NodeSource's
# nodejs which bundles npm; installing Debian's npm on top conflicts, so don't.
if command -v apt-get >/dev/null 2>&1; then
  apt-get update && apt-get install -y nodejs curl
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache nodejs npm curl
elif command -v yum >/dev/null 2>&1; then
  yum install -y nodejs npm curl
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y nodejs npm curl
else
  echo "ERROR: no known package manager (apt/apk/yum/dnf) to install Node" >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: npm not on PATH after installing nodejs (image may lack NodeSource)" >&2
  exit 1
fi

# Pin quint so the evaluator version it expects matches the binary we place.
npm install -g "@informalsystems/quint@${QUINT_VERSION}"
quint --version

# Place the musl-static rust evaluator where quint looks (QUINT_HOME). Static
# => no glibc dependency; pre-placed => no runtime download; quint uses a
# present binary with no checksum, so our self-built one is accepted.
EVAL_DIR="${QUINT_HOME_DIR}/rust-evaluator-${EVALUATOR_VERSION}"
mkdir -p "$EVAL_DIR"
curl -fsSL "$EVALUATOR_URL" -o "$EVAL_DIR/quint_evaluator"
chmod +x "$EVAL_DIR/quint_evaluator"
chmod -R a+rX "$QUINT_HOME_DIR"

# Verify the evaluator actually runs on THIS base image, via the default rust
# backend, with no network — fail loud at build, not at task time.
VSPEC="$(mktemp -d)/v.qnt"
cat > "$VSPEC" <<'QNT'
module v {
  var x: int
  action init = { x' = 0 }
  action step = { x' = x + 1 }
  val inv = x >= 0
}
QNT
QUINT_HOME="$QUINT_HOME_DIR" quint run "$VSPEC" --invariant=inv --max-steps=3 --max-samples=3
echo "quint rust evaluator verified at $QUINT_HOME_DIR"

# Persist Quint specs natively. The skill writes specs to /tmp/quint-specs
# (harness-agnostic). Symlink that to the bind-mounted /logs/agent so the
# specs land in the host trial dir (jobs/.../agent/quint-specs/) instead of
# being lost with /tmp. They stay outside /app, so they're still never part
# of the submitted diff. The mount/target appears at task time; ln only needs
# to create the link now.
ln -sfn /logs/agent/quint-specs /tmp/quint-specs
"""
    return ("set -euo pipefail\n" + header + body).strip()


def _materialize_skill_files_steps(skill_root: Path) -> list[str]:
    """Scripts that materialize the skill at ``SKILL_DEST``, as a *list* of
    install-step bodies.

    Pier inlines each install step as a single ``RUN ["/bin/bash","-c","..."]``
    line, and BuildKit caps Dockerfile lines at 64 KB. We tar+gzip the whole
    skill (SKILL.md + the guidelines/ tree, including the vendored choreo/
    framework), base64 the blob, and append it to a file in fixed-size chunks
    across multiple steps — each step's line stays well under 64 KB regardless
    of how large the skill grows. A final step decodes and extracts.
    """
    def _skip_dotfiles(ti: tarfile.TarInfo):
        base = ti.name.rsplit("/", 1)[-1]
        return None if base.startswith(".") else ti

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(skill_root / "SKILL.md", arcname="SKILL.md")
        tar.add(
            skill_root / "guidelines",
            arcname="guidelines",
            filter=_skip_dotfiles,
        )

    blob_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    dest_q = shlex.quote(SKILL_DEST)
    b64_path = "/tmp/quint-skill.b64"

    # base64 is [A-Za-z0-9+/=] only, so single-quoting each chunk is safe.
    chunk_size = 48_000
    chunks = [
        blob_b64[i : i + chunk_size] for i in range(0, len(blob_b64), chunk_size)
    ]

    steps: list[str] = []
    for idx, chunk in enumerate(chunks):
        redir = ">" if idx == 0 else ">>"
        steps.append(
            "set -euo pipefail\n"
            f"printf '%s' '{chunk}' {redir} {b64_path}\n"
        )
    steps.append(
        "set -euo pipefail\n"
        f"mkdir -p {dest_q}\n"
        f"base64 -d {b64_path} | tar -xzf - -C {dest_q}\n"
        f"rm -f {b64_path}\n"
        f"test -f {dest_q}/SKILL.md && test -d {dest_q}/guidelines "
        "|| (echo 'ERROR: skill materialization failed' >&2; exit 1)\n"
    )
    return steps


# Extra domains needed during the install phase, beyond the LLM-provider ones
# the parent class already grants.
_QUINT_INSTALL_DOMAINS: tuple[str, ...] = (
    # npm registry for `npm install -g @informalsystems/quint`
    "registry.npmjs.org",
    # nodejs / node-gyp may reach these during npm install or apt post-install
    "nodejs.org",
)


# AWS variables to forward into the sandbox for Bedrock models. SSO / temporary
# credentials need the secret + session token in addition to the access key.
_BEDROCK_CRED_VARS: tuple[str, ...] = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_REGION_NAME",
)


def _is_bedrock_model(model_name: str | None) -> bool:
    return bool(model_name) and model_name.startswith("bedrock/")


# ---------------------------------------------------------------------------
# The agent.
# ---------------------------------------------------------------------------


class QuintMiniSweAgent(MiniSweAgent):
    """mini-swe-agent + the Quint-assisted-implementation skill.

    Behaves identically to the upstream `MiniSweAgent` except for four things:
      1. the install phase adds Node + `quint` and writes the skill files
         into ``/opt/quint-skill/`` inside the sandbox;
      2. the task instruction is prepended with SKILL.md so the agent sees
         the skill from turn 1 without needing a separate prompt template;
      3. the network allowlist is extended for npm during install;
      4. for Bedrock models, the full AWS SSO credential set is forwarded into
         the sandbox (upstream only forwards AWS_ACCESS_KEY_ID, which is
         insufficient for temporary/SSO credentials).
    """

    def __init__(
        self,
        *args,
        skill_path: str | Path | None = None,
        force_quint: bool | str = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        # Resolve at construction time, not at module import. Pier may load
        # this module before the skill is available (e.g. CLI inspection).
        self._skill_root = _resolve_skill_path(skill_path)
        # `force_quint` arrives as a string via --agent-kwarg; coerce.
        self._force_quint = str(force_quint).strip().lower() in (
            "1", "true", "yes", "on",
        )
        # Point quint at the pre-placed musl evaluator at task time. _extra_env
        # propagates into the agent's bash subprocesses. setdefault so an
        # explicit --agent-env QUINT_HOME wins.
        self._extra_env.setdefault("QUINT_HOME", QUINT_HOME_DIR)
        # Per-call LLM timeout + retries. A single hung Bedrock/litellm call was
        # observed eating ~80 min of the agent wall-clock (a wedged trial). Bound
        # each call so a stall fails fast and retries instead of burning the
        # whole budget. setdefault so explicit model_kwargs win.
        self._model_kwargs.setdefault("timeout", 600)
        self._model_kwargs.setdefault("num_retries", 2)
        if _is_bedrock_model(self.model_name):
            self._forward_bedrock_credentials()

    # ---- bedrock credentials ----------------------------------------------

    def _forward_bedrock_credentials(self) -> None:
        """Pull AWS credentials from the host env into ``_extra_env`` so they
        reach the sandbox.

        mini-swe-agent's run() only auto-forwards ``AWS_ACCESS_KEY_ID`` for
        bedrock (Pier's ``PROVIDER_KEYS["bedrock"]``), but SSO / temporary
        credentials also require ``AWS_SECRET_ACCESS_KEY`` and
        ``AWS_SESSION_TOKEN``. Everything in ``_extra_env`` is merged into the
        sandbox env and is visible to the parent's API-key check, so adding the
        full set here is sufficient. Explicit ``--agent-env`` values win.
        """
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        for var in _BEDROCK_CRED_VARS:
            if var in self._extra_env:
                continue  # explicit --agent-env wins
            val = os.environ.get(var)
            # litellm/boto each look for different region aliases; backfill
            # them from whatever region we do have.
            if val is None and var in ("AWS_DEFAULT_REGION", "AWS_REGION_NAME"):
                val = region
            if val:
                self._extra_env[var] = val

        missing = [
            v
            for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
            if v not in self._extra_env
        ]
        if missing:
            raise RuntimeError(
                f"Bedrock model {self.model_name!r} selected but {missing} "
                "not found in the environment. Export your SSO credentials "
                "first, e.g.:\n"
                '  eval "$(aws configure export-credentials '
                '--profile bedrock --format env)"'
            )

    @staticmethod
    def name() -> str:
        # Not a member of Pier's AgentName enum. That only matters for
        # `AgentFactory.create_agent_from_name`; we're loaded via
        # `create_agent_from_import_path`, which doesn't look at the enum.
        # This string surfaces in trajectories and the install-spec cache key.
        return "quint-mini-swe-agent"

    # ---- install ----------------------------------------------------------

    @property
    def _install_python_packages(self) -> list[str]:
        """Add boto3 (→ botocore) for Bedrock models.

        litellm's Bedrock path imports botocore to handle AWS credentials, but
        mini-swe-agent's sandbox venv doesn't ship it (manifests as a
        mislabelled `APIConnectionError: No module named 'botocore'` on the
        first LLM call). Mirrors the parent's google-auth-for-vertex pattern.
        """
        packages = list(super()._install_python_packages)
        if _is_bedrock_model(self.model_name):
            packages.append("boto3")
        return list(dict.fromkeys(packages))

    def install_spec(self) -> AgentInstallSpec:
        """Extend the parent's install steps with quint + skill materialization."""
        base = super().install_spec()
        return AgentInstallSpec(
            agent_name=self.name(),
            version=self._version,
            steps=[
                *base.steps,
                InstallStep(
                    user="root",
                    env={"DEBIAN_FRONTEND": "noninteractive"},
                    run=_install_node_and_quint_script(),
                ),
                *(
                    InstallStep(user="root", env=None, run=s)
                    for s in _materialize_skill_files_steps(self._skill_root)
                ),
            ],
            verification_command=base.verification_command,
        )

    # ---- network ----------------------------------------------------------

    def network_allowlist(self) -> NetworkAllowlist:
        """Add npm/node domains to whatever the parent allows."""
        base = super().network_allowlist()
        merged = sorted(set(base.domains) | set(_QUINT_INSTALL_DOMAINS))
        return NetworkAllowlist(domains=merged)

    # ---- prompt -----------------------------------------------------------

    def render_instruction(self, instruction: str) -> str:
        """Prepend SKILL.md to the task instruction.

        The base-class behaviour is to run a Jinja2 ``prompt_template_path``
        through the instruction; we instead bake the skill content directly
        into the preamble. Keeps the agent self-contained — no separate
        template file to manage, no Jinja escaping concerns for SKILL.md.
        """
        skill_md = (self._skill_root / "SKILL.md").read_text(encoding="utf-8")
        forced = _FORCE_QUINT_PREAMBLE if self._force_quint else ""
        return (
            f"{forced}"
            f"{skill_md}\n\n"
            "---\n\n"
            "# Task\n\n"
            f"{instruction}"
        )
