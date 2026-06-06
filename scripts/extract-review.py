#!/usr/bin/env python3
"""Extract a browsable review tree from Pier job artifacts.

For every trial under jobs/, writes:
  review/<task>/<job>__<trial>/
    RESULT.txt        model, reward, exit, turns, pass/fail summary
    model.patch       the code the agent submitted
    specs/*.qnt       every Quint spec the agent wrote (final version per path),
                      reconstructed from the trajectory's cat-heredoc commands

Quint specs are written to /tmp inside the sandbox and never persisted as
files, so the only record is the `cat > foo.qnt <<EOF ... EOF` command in the
trajectory — this pulls them back out.

Usage:  python scripts/extract-review.py          # all jobs
        python scripts/extract-review.py kombu     # filter task substring
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "jobs"
OUT = ROOT / "review"

# cat > PATH <<'MARKER' \n BODY \n MARKER   (and the cat <<'MARKER' > PATH form)
_HEREDOC = re.compile(
    r"cat\s*(?:>\s*(?P<p1>\S+?\.qnt)\s*)?<<\s*'?(?P<marker>\w+)'?\s*"
    r"(?:>\s*(?P<p2>\S+?\.qnt)\s*)?\n(?P<body>.*?)\n(?P=marker)",
    re.S,
)


def _cmds(traj: dict) -> list[str]:
    out = []
    for m in traj.get("messages", []):
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            a = (tc.get("function") or {}).get("arguments", "")
            try:
                a = json.loads(a).get("command", a)
            except Exception:
                pass
            out.append(a)
    return out


def _specs_from_traj(traj: dict) -> dict[str, str]:
    """path -> final spec body (last write wins)."""
    specs: dict[str, str] = {}
    for c in _cmds(traj):
        if ".qnt" not in c:
            continue
        for m in _HEREDOC.finditer(c):
            path = m.group("p1") or m.group("p2")
            if path and path.endswith(".qnt"):
                specs[Path(path).name] = m.group("body")
    return specs


def main() -> None:
    filt = sys.argv[1] if len(sys.argv) > 1 else ""
    n = 0
    for traj_path in sorted(JOBS.glob("**/agent/mini-swe-agent.trajectory.json")):
        trial_dir = traj_path.parent.parent
        job = trial_dir.parent.name
        trial = trial_dir.name
        if filt and filt not in trial:
            continue
        task = trial.split("__")[0]
        try:
            traj = json.loads(traj_path.read_text())
        except Exception:
            continue
        info = traj.get("info", {})
        model = (info.get("config", {}).get("model", {}) or {}).get("model_name", "?")
        reward_f = trial_dir / "verifier" / "reward.txt"
        reward = reward_f.read_text().strip() if reward_f.exists() else "?"
        test_f = trial_dir / "verifier" / "test-stdout.txt"
        summary = ""
        if test_f.exists():
            mm = re.findall(r"\d+ (?:passed|failed)[^\n]*", test_f.read_text())
            summary = mm[-1] if mm else ""
        turns = sum(1 for m in traj.get("messages", []) if m.get("role") == "assistant")

        dest = OUT / task / f"{job}__{trial.split('__')[-1]}"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "RESULT.txt").write_text(
            f"task:   {task}\nmodel:  {model}\nreward: {reward}\n"
            f"exit:   {info.get('exit_status') or 'TIMEOUT'}\nturns:  {turns}\n"
            f"tests:  {summary}\njob:    {job}\ntrial:  {trial}\n"
        )
        patch = trial_dir / "artifacts" / "model.patch"
        if patch.exists():
            (dest / "model.patch").write_text(patch.read_text(errors="replace"))
        specs = _specs_from_traj(traj)
        if specs:
            (dest / "specs").mkdir(exist_ok=True)
            for name, body in specs.items():
                (dest / "specs" / name).write_text(body)
        n += 1
        print(f"  {task}/{dest.name}  reward={reward} specs={len(specs)} {summary}")
    print(f"\nwrote {n} trials to {OUT}/")
    print(f"task definitions live in: ../deepswe-datacurve/tasks/<task>/")


if __name__ == "__main__":
    main()
