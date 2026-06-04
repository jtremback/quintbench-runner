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
    """Install Node + npm + quint globally via whichever package manager is
    available. Mirrors the apt/apk/yum/dnf fan-out pattern from
    MiniSweAgent.install_spec() for portability across base images."""
    # On Debian/Ubuntu images with NodeSource preconfigured (the case for the
    # DeepSWE base images we've seen), `apt-get install -y nodejs` pulls
    # NodeSource's nodejs which bundles npm. Trying to install Debian's npm
    # package separately on top conflicts ("nodejs : Conflicts: npm"), so
    # don't. We verify npm exists after; if it doesn't, the image isn't using
    # NodeSource and the run fails loudly — fix in a follow-up iteration.
    return r"""
set -euo pipefail

if command -v apt-get >/dev/null 2>&1; then
  apt-get update && apt-get install -y nodejs
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache nodejs npm
elif command -v yum >/dev/null 2>&1; then
  yum install -y nodejs npm
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y nodejs npm
else
  echo "ERROR: no known package manager (apt/apk/yum/dnf) to install Node" >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: npm not on PATH after installing nodejs (image may lack NodeSource)" >&2
  exit 1
fi

npm install -g @informalsystems/quint

# Sanity check; loud failure if quint isn't on PATH.
quint --version

# --- Force the TypeScript evaluator backend ---------------------------------
# quint's default `rust` backend downloads a native binary that needs a recent
# glibc and GitHub access at runtime — neither exists in the air-gapped sandbox
# (it dies with GLIBC_2.39-not-found / EAI_AGAIN). The agent also reliably omits
# `--backend=typescript` on its first `quint run`, wasting a turn. Shadow the
# quint entrypoint with a wrapper that injects `--backend=typescript` for
# `run`/`test` whenever the caller didn't already pass a --backend. Idempotent
# with the skill examples (won't double the flag).
QUINT_BIN="$(command -v quint)"
if [ -z "$QUINT_BIN" ]; then
  echo "ERROR: quint not on PATH after install; cannot install backend wrapper" >&2
  exit 1
fi
QUINT_REAL="${QUINT_BIN}-real"
mv "$QUINT_BIN" "$QUINT_REAL"
cat > "$QUINT_BIN" <<WRAP
#!/usr/bin/env bash
real="\$(dirname "\$0")/$(basename "$QUINT_REAL")"
if { [ "\$1" = "run" ] || [ "\$1" = "test" ]; } && ! printf '%s\n' "\$@" | grep -q -- '--backend'; then
  exec "\$real" "\$@" --backend=typescript
fi
exec "\$real" "\$@"
WRAP
chmod +x "$QUINT_BIN"
# Verify the wrapper resolves and the real binary still works.
quint --version
""".strip()


def _materialize_skill_files_script(skill_root: Path) -> str:
    """Generate a shell script that materializes the skill at ``SKILL_DEST``.

    Pier inlines each install step as a single ``RUN ["/bin/bash","-c","..."]``
    line in the Dockerfile, and BuildKit caps Dockerfile lines at 64 KB. A
    naive heredoc-per-file approach hit that ceiling (the skill is ~140 KB
    raw). Instead we tar+gzip the skill on the host, base64-encode the blob,
    and have the sandbox decode-and-extract it in one shot. ~140 KB raw
    compresses to ~30 KB gzipped and ~40 KB base64 — well under 64 KB.
    """
    # Build a tar.gz of the skill in memory. Members are stored with paths
    # relative to SKILL_DEST so we can extract straight into it.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        skill_md = skill_root / "SKILL.md"
        tar.add(skill_md, arcname="SKILL.md")
        guidelines_dir = skill_root / "guidelines"
        for md in sorted(guidelines_dir.glob("*.md")):
            tar.add(md, arcname=f"guidelines/{md.name}")
        examples_dir = guidelines_dir / "examples"
        if examples_dir.is_dir():
            for qnt in sorted(examples_dir.glob("*.qnt")):
                tar.add(qnt, arcname=f"guidelines/examples/{qnt.name}")

    blob_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    dest_q = shlex.quote(SKILL_DEST)
    return (
        "set -euo pipefail\n"
        f"mkdir -p {dest_q}\n"
        f'echo "{blob_b64}" | base64 -d | tar -xzf - -C {dest_q}\n'
        f"test -f {dest_q}/SKILL.md && test -d {dest_q}/guidelines "
        "|| (echo 'ERROR: skill materialization failed' >&2; exit 1)\n"
    )


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
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        # Resolve at construction time, not at module import. Pier may load
        # this module before the skill is available (e.g. CLI inspection).
        self._skill_root = _resolve_skill_path(skill_path)
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
                InstallStep(
                    user="root",
                    env=None,
                    run=_materialize_skill_files_script(self._skill_root),
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
        return (
            f"{skill_md}\n\n"
            "---\n\n"
            "# Task\n\n"
            f"{instruction}"
        )
