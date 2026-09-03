"""One command that runs every gate this repo has, and reports the facts.

WHY THIS EXISTS
---------------
Verification here has been a sequence of hand-typed shell one-liners, which has
cost real time three separate ways:

  1. Commands were shell-specific. A `&&`-chained one-liner works in Git Bash and
     fails in Windows PowerShell 5.1, where `&&` is a parser error — so a
     verification step could "fail" for reasons having nothing to do with the code.
  2. Flags had to be remembered. `python -m pytest` (module form) is mandatory
     because nothing puts `backend/` on `sys.path`; bare `pytest` dies with
     `ModuleNotFoundError: No module named 'app'`, which reads like a code
     regression rather than an invocation mistake. That has already burned a
     verification cycle.
  3. Tool versions were never reported, so "ruff clean" could not be reproduced
     later — the config used to live on one machine (fixed by `pyproject.toml`,
     roadmap D13) and the *versions* are still unbounded (roadmap D26).

This script is the single, shell-agnostic entry point: same command in PowerShell,
CMD and Git Bash, no flags to remember, and it prints the version facts that make
a result reproducible instead of merely green.

USAGE, from ``backend/``::

    python scripts/verify_repo.py              # versions + ruff + targeted tests
    python scripts/verify_repo.py --full       # ...plus the full 1700-test suite
    python scripts/verify_repo.py --versions   # facts only, runs no gate

Exit code is 0 only if every gate that ran passed, so this is CI-usable as-is
(roadmap M8).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: Fast gate: the tests covering the most recently changed behaviour. Kept
#: explicit rather than inferred from git, so the command is deterministic and
#: means the same thing on every machine and every day.
TARGETED_TESTS = (
    "tests/connectors/test_live_pipeline.py",
    "tests/discovery/test_leadership_discovery.py",
    "tests/discovery/test_people_parser.py",
    "tests/discovery/test_pdf_plan_holder_parser.py",
)

#: Directories ruff is pointed at. `scripts/` is included deliberately: the
#: diagnostics in it import production internals, and a diagnostic that no longer
#: compiles is worse than none at all, because it gets trusted.
LINT_PATHS = ("app", "tests", "scripts")


def _run(label: str, args: list[str], *, capture: bool) -> tuple[bool, str]:
    """Run one subprocess and report whether it succeeded.

    Args:
        label: Human-readable gate name for the report.
        args: Argument list, always launched via ``sys.executable`` so the
            interpreter running this script is the one running the gate — this is
            what makes the module-form pytest requirement impossible to get wrong.
        capture: When True the output is returned instead of streamed, for short
            version probes. Long gates stream so progress is visible live.

    Returns:
        ``(ok, output)``. ``output`` is ``""`` when streaming.
    """
    print(f"\n>>> {label}")
    print(f"    {' '.join([Path(sys.executable).name, *args])}")
    if capture:
        proc = subprocess.run(
            [sys.executable, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        text = (proc.stdout + proc.stderr).strip()
        print(f"    {text}" if text else "    (no output)")
        return proc.returncode == 0, text
    proc = subprocess.run([sys.executable, *args], check=False)
    return proc.returncode == 0, ""


def check_cwd() -> bool:
    """Verify the script is being run from ``backend/``.

    Every gate below uses paths relative to ``backend/``, and pytest additionally
    needs the current directory on ``sys.path``. Running from the repo root fails
    with a confusing collection error, so it is caught here with a clear message.
    """
    if Path("app").is_dir() and Path("tests").is_dir():
        return True
    print("ERROR: run this from the backend/ directory, e.g.")
    print("    cd backend")
    print("    python scripts/verify_repo.py")
    print(f"(current directory: {Path.cwd()})")
    return False


def report_versions() -> None:
    """Print the tool versions that make a pass/fail result reproducible.

    Deliberately not a gate — it never fails the run. Its job is to put the four
    numbers that determine the outcome into the same output as the outcome, so a
    result pasted into a log stays meaningful after the tools move (roadmap D26).
    """
    print("=" * 70)
    print("TOOL VERSIONS (roadmap D26 — none of these are pinned yet)")
    print("=" * 70)
    print(f"\n>>> python\n    {sys.version.splitlines()[0]}")
    for label, args in (
        ("ruff", ["-m", "ruff", "--version"]),
        ("black", ["-m", "black", "--version"]),
        ("pytest", ["-m", "pytest", "--version"]),
    ):
        _run(label, args, capture=True)


def main(argv: list[str]) -> int:
    """Entry point. Returns 0 only if every gate that ran passed."""
    flags = {a for a in argv if a.startswith("--")}
    unknown = flags - {"--full", "--versions"}
    if unknown:
        print(f"unknown flag(s): {sorted(unknown)}")
        print(__doc__)
        return 2
    if not check_cwd():
        return 2

    report_versions()
    if "--versions" in flags:
        return 0

    results: list[tuple[str, bool]] = []

    print("\n" + "=" * 70)
    print("GATES")
    print("=" * 70)

    ok, _ = _run(
        "ruff check (config: backend/pyproject.toml)",
        ["-m", "ruff", "check", *LINT_PATHS],
        capture=False,
    )
    results.append(("ruff", ok))

    ok, _ = _run(
        "targeted tests",
        ["-m", "pytest", *TARGETED_TESTS, "-q"],
        capture=False,
    )
    results.append(("targeted tests", ok))

    if "--full" in flags:
        # No --basetemp here: it is now permanent in pytest.ini's addopts
        # (roadmap D6), which is the entire point of that change.
        ok, _ = _run("full suite", ["-m", "pytest", "-q"], capture=False)
        results.append(("full suite", ok))
    else:
        print("\n>>> full suite  SKIPPED — pass --full to include it (~4 min)")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, passed in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    failed = [name for name, passed in results if not passed]
    if failed:
        print(f"\nRESULT: FAIL ({', '.join(failed)})")
        return 1
    scope = "all gates" if "--full" in flags else "ruff + targeted tests"
    print(f"\nRESULT: PASS ({scope})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
