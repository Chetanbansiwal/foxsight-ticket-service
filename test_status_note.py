"""Controls for the note attached to a ticket status change.

PATCH /api/tickets/{id}/status read only `comment`, but the web client has
always sent `reason` — resolveTicket(id, reason) and closeTicket(id, reason)
both take one. So an operator's stated reason for resolving or closing was
accepted by the API and silently discarded. It was invisible because the modal
did not yet expose a reason field; the moment one is added, the text would
vanish with no error.

Run:
  python3 test_status_note.py
"""

import ast
import sys
from pathlib import Path

# main.py imports FastAPI/SQLAlchemy/etc, which are not present outside the
# service image. Lift the one pure helper out of the source instead, so this
# control runs anywhere and still tests the shipped code rather than a copy.
_SRC = Path(__file__).with_name("main.py").read_text()
_TREE = ast.parse(_SRC)
_FN = next(
    (n for n in _TREE.body
     if isinstance(n, ast.FunctionDef) and n.name == "_status_note"),
    None,
)
assert _FN is not None, "_status_note has been renamed or removed from main.py"

_ns: dict = {}
exec(compile(ast.Module(body=[_FN], type_ignores=[]), "main.py", "exec"), _ns)
_status_note = _ns["_status_note"]


CASES = []


def case(name):
    def deco(f):
        CASES.append((name, f))
        return f
    return deco


@case("the reason the client actually sends is recorded")
def _():
    assert _status_note({"status": "closed", "reason": "false alarm, wind"}) == \
        "false alarm, wind"


@case("comment still works, so any older caller keeps functioning")
def _():
    assert _status_note({"status": "resolved", "comment": "guard dispatched"}) == \
        "guard dispatched"


@case("reason wins when both are present")
def _():
    # They should not both arrive, but if they do the live caller's field is
    # the one an operator actually typed into.
    got = _status_note({"reason": "from the client", "comment": "from elsewhere"})
    assert got == "from the client", got


@case("no note at all leaves no comment")
def _():
    assert _status_note({"status": "closed"}) is None


@case("blank and whitespace-only notes do not become empty comments")
def _():
    for blank in ("", "   ", "\n\t "):
        assert _status_note({"reason": blank}) is None, repr(blank)


@case("surrounding whitespace is trimmed")
def _():
    assert _status_note({"reason": "  patrol confirmed  "}) == "patrol confirmed"


def main():
    failed = 0
    for name, fn in CASES:
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}\n       {e}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
