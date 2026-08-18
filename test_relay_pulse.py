"""Controls for the programmed relay response (WS4, clause 50.8).

A relay is physical: leaving one latched is a siren that will not stop or a
door that stays unlocked, so the rules about WHEN a release is scheduled matter
more than the happy path.

Run:
  python3 test_relay_pulse.py
"""

import ast
import sys
from pathlib import Path

_SRC = Path(__file__).with_name("main.py").read_text()
_TREE = ast.parse(_SRC)

FAILURES = []
CHECKS = 0


def check(label, got, want):
    global CHECKS
    CHECKS += 1
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILURES.append(label)


def _fn(name):
    for n in ast.walk(_TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(_SRC, n) or ""
    return ""


def main():
    print("relay pulse — a release must actually be scheduled\n")

    run = _fn("_run_response_action")
    rel = _fn("_release_relay_after")

    check("the release helper exists", bool(rel), True)
    check("the release drives the output INACTIVE", '"state": "inactive"' in rel, True)
    check("the release waits before firing", "asyncio.sleep(seconds)" in rel, True)
    check("a failed release is logged, not swallowed silently",
          "may remain latched" in rel, True)

    check("a pulse is scheduled without blocking the escalation loop",
          "asyncio.create_task(_release_relay_after(" in run, True)
    check("the pulse is capped", "min(pulse, MAX_PULSE_SECONDS)" in run, True)
    check("the cap is small enough to bound a restart window",
          0 < int(_SRC.split("MAX_PULSE_SECONDS = ")[1].split("\n")[0]) <= 60, True)

    # The three conditions that must ALL hold before scheduling a release.
    check("no release when no pulse was asked for", "pulse > 0" in run, True)
    check("no release when the action was a deactivate",
          'state in (True, "active", "on", "activate", "1", 1)' in run, True)
    check("no release when the drive itself failed", "r.status_code < 400" in run, True)

    # A malformed pulse must not raise inside the response path.
    check("a non-numeric pulse degrades to no pulse",
          "except (TypeError, ValueError)" in run, True)

    # The action still targets the alarm's own camera (nearby-camera is future
    # work, deliberately not half-built).
    check("the relay fires on the ticket's camera",
          "cameras/{ticket.camera_id}/relay-outputs" in run, True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} — {', '.join(FAILURES)}")
        return 1
    print(f"All {CHECKS} relay-pulse checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
