"""
One-time local setup: install the CI workflows and make this folder a git repo.

    python setup.py

Does only the safe local part - it never talks to GitHub, Fly or Supabase, and
never pushes. It finishes by printing the exact commands for the next step.

Run it as many times as you like; every step checks before acting.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
WORKFLOWS = ROOT / ".github" / "workflows"
DEPLOY = ROOT / "deploy"

GH_USER = "kongper"
REPO = "masseberegning"
FLY_APP = "masseberegning-api"

# Files that must never reach GitHub, checked rather than assumed.
NEVER_COMMIT = [".env", ".env.local"]


def say(msg: str = "") -> None:
    print(msg)


def step(n: int, title: str) -> None:
    say()
    say(f"[{n}] {title}")


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, check=check, capture_output=True, text=True)


def have_git() -> bool:
    return shutil.which("git") is not None


def install_workflows() -> None:
    """Move deploy/*.yml into .github/workflows/.

    They ship under deploy/ because the tool that wrote this project to disk
    cannot write into .github/ - GitHub Actions files are protected from
    remote writes. Nothing about them is special once they are in place.
    """
    WORKFLOWS.mkdir(parents=True, exist_ok=True)
    for name in ("api.yml", "pages.yml"):
        src, dst = DEPLOY / name, WORKFLOWS / name
        if not src.exists():
            if dst.exists():
                say(f"    .github/workflows/{name} already installed")
            else:
                say(f"    !! missing both deploy/{name} and .github/workflows/{name}")
            continue
        if dst.exists() and dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8"):
            say(f"    .github/workflows/{name} already up to date")
            src.unlink()
            continue
        # Strip the "install this at" preamble; it is an instruction to the
        # human, not to Actions.
        lines = src.read_text(encoding="utf-8").splitlines(keepends=True)
        preamble = ("# Install this at", "# (setup.py moves it there")
        while lines and lines[0].startswith(preamble):
            lines.pop(0)
        if lines and lines[0].strip() == "#":
            lines.pop(0)
        dst.write_text("".join(lines), encoding="utf-8")
        src.unlink()
        say(f"    wrote .github/workflows/{name}")

    if DEPLOY.exists() and not any(DEPLOY.iterdir()):
        DEPLOY.rmdir()
        say("    removed the now-empty deploy/ folder")


def check_secrets_not_staged() -> list[str]:
    found = [n for n in NEVER_COMMIT if (ROOT / n).exists()]
    return found


def init_repo() -> bool:
    """Returns True if a commit exists at the end."""
    if (ROOT / ".git").exists():
        say("    already a git repository")
    else:
        run("git", "init", "-b", "main")
        say("    git init (branch: main)")

    # Identity, only if not already configured, so we never override a global.
    for key, fallback in (("user.name", "Per Henning Johansen"),
                          ("user.email", "per@prosit.no")):
        got = run("git", "config", key, check=False)
        if not got.stdout.strip():
            run("git", "config", key, fallback)
            say(f"    set local {key} = {fallback}")

    run("git", "add", "-A")

    staged = run("git", "diff", "--cached", "--name-only").stdout.split()
    leaked = [f for f in staged if f in NEVER_COMMIT or f.startswith(".venv")]
    if leaked:
        say(f"    !! refusing to commit: {', '.join(leaked)}")
        say("       check .gitignore, then re-run")
        return False

    if not staged:
        say("    nothing new to commit")
        return bool(run("git", "log", "-1", "--oneline", check=False).stdout.strip())

    say(f"    staged {len(staged)} files")
    run("git", "commit", "-m",
        "Masseberegning: invite-only auth, split deployment\n\n"
        "Static frontend for GitHub Pages, FastAPI container for Fly.io,\n"
        "Supabase Auth for sign-in and Postgres for membership.\n"
        "LOCAL_SINGLE_USER=1 preserves the original local workflow.")
    say("    committed")
    return True


def main() -> int:
    say("Masseberegning - local setup")
    say("=" * 60)

    if not have_git():
        say()
        say("git is not installed, or not on PATH.")
        say("Install it from https://git-scm.com/download/win and re-run.")
        return 1

    step(1, "Installing the CI workflows")
    install_workflows()

    leaked = check_secrets_not_staged()
    if leaked:
        step(2, "Checking for files that must not be committed")
        say(f"    {', '.join(leaked)} exists locally - .gitignore covers it, "
            "just don't force-add it")

    step(3, "Setting up the git repository")
    committed = init_repo()

    say()
    say("=" * 60)
    if not committed:
        say("Stopped before committing. Fix the note above and re-run.")
        return 1

    say("Local setup done. Next, create the repo on GitHub and push.")
    say()
    say("  Option A - with the GitHub CLI (gh):")
    say(f"    gh repo create {REPO} --private --source=. --remote=origin --push")
    say()
    say("  Option B - without it: create an EMPTY private repo named")
    say(f"    '{REPO}' at https://github.com/new  (no README, no .gitignore)")
    say("  then:")
    say(f"    git remote add origin https://github.com/{GH_USER}/{REPO}.git")
    say("    git push -u origin main")
    say()
    say("The workflows will fail on this first push - that is expected. They")
    say("need repository variables and secrets that do not exist yet.")
    say("Carry on with README-DEPLOY.md section 3.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
