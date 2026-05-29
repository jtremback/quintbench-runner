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

import os
import shlex
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
    return r"""
set -euo pipefail

if command -v apt-get >/dev/null 2>&1; then
  apt-get update && apt-get install -y nodejs npm
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

npm install -g @informalsystems/quint

# Sanity check; loud failure if quint isn't on PATH.
quint --version
""".strip()


def _materialize_skill_files_script(skill_root: Path) -> str:
    """Generate a shell script that creates ``SKILL_DEST`` and writes every
    skill file into it via heredoc. Contents are embedded at agent-construction
    time so the skill repo can stay private (no `git clone` inside the
    sandbox), and so the skill version that runs matches this checkout
    exactly."""
    lines = [
        "set -euo pipefail",
        f"mkdir -p {shlex.quote(SKILL_DEST)}/guidelines/examples",
    ]

    # Collect every file we need to ship.
    files: list[tuple[str, Path]] = [("SKILL.md", skill_root / "SKILL.md")]
    guidelines_dir = skill_root / "guidelines"
    for md in sorted(guidelines_dir.glob("*.md")):
        files.append((f"guidelines/{md.name}", md))
    examples_dir = guidelines_dir / "examples"
    if examples_dir.is_dir():
        for qnt in sorted(examples_dir.glob("*.qnt")):
            files.append((f"guidelines/examples/{qnt.name}", qnt))

    for idx, (rel_dest, src) in enumerate(files):
        dest_path = f"{SKILL_DEST}/{rel_dest}"
        content = src.read_text(encoding="utf-8")

        # Pick a heredoc marker that can't collide with file content.
        marker = f"QUINT_SKILL_EOF_{idx:03d}"
        while marker in content:
            marker += "_X"

        lines.append(
            # Single-quoted heredoc => no shell expansion of the body.
            f"cat > {shlex.quote(dest_path)} <<'{marker}'\n"
            f"{content}\n"
            f"{marker}"
        )

    # Final sanity check.
    lines.append(
        f"test -f {shlex.quote(SKILL_DEST)}/SKILL.md "
        f"&& test -d {shlex.quote(SKILL_DEST)}/guidelines "
        "|| (echo 'ERROR: skill materialization failed' >&2; exit 1)"
    )
    return "\n".join(lines)


# Extra domains needed during the install phase, beyond the LLM-provider ones
# the parent class already grants.
_QUINT_INSTALL_DOMAINS: tuple[str, ...] = (
    # npm registry for `npm install -g @informalsystems/quint`
    "registry.npmjs.org",
    # nodejs / node-gyp may reach these during npm install or apt post-install
    "nodejs.org",
)


# ---------------------------------------------------------------------------
# The agent.
# ---------------------------------------------------------------------------


class QuintMiniSweAgent(MiniSweAgent):
    """mini-swe-agent + the Quint-assisted-implementation skill.

    Behaves identically to the upstream `MiniSweAgent` except for three things:
      1. the install phase adds Node + `quint` and writes the skill files
         into ``/opt/quint-skill/`` inside the sandbox;
      2. the task instruction is prepended with SKILL.md so the agent sees
         the skill from turn 1 without needing a separate prompt template;
      3. the network allowlist is extended for npm during install.
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

    @staticmethod
    def name() -> str:
        # Not a member of Pier's AgentName enum. That only matters for
        # `AgentFactory.create_agent_from_name`; we're loaded via
        # `create_agent_from_import_path`, which doesn't look at the enum.
        # This string surfaces in trajectories and the install-spec cache key.
        return "quint-mini-swe-agent"

    # ---- install ----------------------------------------------------------

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
