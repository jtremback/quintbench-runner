#!/usr/bin/env python3
"""Reconstitute an agent trial's repo filesystem from its model.patch.

model.patch is `git diff` against the task's base commit, so the agent's final
/app state = (repo @ base_commit) + model.patch. This clones the task repo at
the base commit, applies the trial's patch, and (optionally) also materializes
the gold solution alongside for diffing.

Usage:
  python scripts/reconstitute.py <trial-dir>          # one trial
  python scripts/reconstitute.py <trial-dir> --gold   # also build gold/ sibling

<trial-dir> is a jobs/.../<task>__<id> dir (or a review/.../<...> dir that has
model.patch). Output goes to recon/<task>/<trial>/{repo, gold?, specs?}.
Base clones are cached under recon/_base/<task>@<sha> and reused across trials.
"""
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT.parent / "deepswe-datacurve" / "tasks"
OUT = ROOT / "recon"


def sh(cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, text=True,
                          capture_output=True)


def find_patch(trial_dir: Path) -> Path:
    for p in (trial_dir / "artifacts" / "model.patch", trial_dir / "model.patch"):
        if p.is_file():
            return p
    raise SystemExit(f"no model.patch under {trial_dir}")


def task_of(trial_dir: Path) -> str:
    """Canonical task dir name (trial names are truncated, e.g. -pri vs -priority)."""
    raw = trial_dir.name.split("__")[0]
    if (TASKS / raw).is_dir():
        return raw
    cands = [d.name for d in TASKS.iterdir() if d.is_dir() and d.name.startswith(raw)]
    if len(cands) == 1:
        return cands[0]
    raise SystemExit(f"can't resolve task for {raw!r} (candidates: {cands})")


def base_clone(task: str) -> Path:
    """Clone the task repo at its base commit (cached, blobless)."""
    meta = tomllib.loads((TASKS / task / "task.toml").read_text())["metadata"]
    url, sha = meta["repository_url"], meta["base_commit_hash"]
    cache = OUT / "_base" / f"{task}@{sha[:12]}"
    if (cache / ".git").is_dir():
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    print(f"  cloning {url} @ {sha[:12]} (blobless) ...")
    sh(["git", "clone", "--filter=blob:none", url, str(cache)])
    r = sh(["git", "checkout", sha], cwd=cache, check=False)
    if r.returncode != 0:  # fallback: fetch the exact sha
        sh(["git", "fetch", "--depth", "1", "origin", sha], cwd=cache)
        sh(["git", "checkout", sha], cwd=cache)
    return cache


def reconstitute(trial_dir: Path, gold: bool):
    task = task_of(trial_dir)
    patch = find_patch(trial_dir)
    base = base_clone(task)
    dest = OUT / task / trial_dir.name
    repo = dest / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    repo.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(base, repo, symlinks=True)
    # apply the agent's patch
    r = sh(["git", "apply", "--whitespace=nowarn", str(patch.resolve())],
           cwd=repo, check=False)
    if r.returncode != 0:
        r2 = sh(["git", "apply", "--3way", "--whitespace=nowarn", str(patch.resolve())],
                cwd=repo, check=False)
        status = "applied (3way)" if r2.returncode == 0 else f"FAILED: {r.stderr.strip()[:200]}"
    else:
        status = "applied"
    print(f"  {task}/{trial_dir.name}: patch {status} -> {repo}")
    if gold:
        goldp = TASKS / task / "solution" / "solution.patch"
        if goldp.is_file():
            g = dest / "gold"
            if g.exists():
                shutil.rmtree(g)
            shutil.copytree(base, g, symlinks=True)
            gr = sh(["git", "apply", "--whitespace=nowarn", str(goldp.resolve())],
                    cwd=g, check=False)
            print(f"    gold: {'applied' if gr.returncode==0 else 'FAILED'} -> {g}")
    print(f"  → open {repo}  |  diff agent vs gold: diff -ru {dest}/gold {repo}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    gold = "--gold" in sys.argv
    if not args:
        raise SystemExit(__doc__)
    reconstitute(Path(args[0]).resolve(), gold)
