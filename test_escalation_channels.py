"""Controls for which channels an escalation level may use, and who gets pushed.

Two defects lived here, both silent by construction:

1. `webhook` was offered as a per-recipient channel. Delivery resolves a
   destination from the user's verified notification channels, and
   user-management only ever accepts email/sms/whatsapp/push as a channel type
   — so a webhook row could never resolve an address and failed on every
   attempt, forever. Nothing surfaced it: per-row failures are written to
   `notification_logs.error_message`, not to any log a person reads.

2. The in-app push was published to EVERY resolved recipient regardless of the
   channels ticked, because the publish read the "all recipients" set. So the
   channel picker governed the audit rows and nothing else: un-ticking In-app
   still put the toast on the operator's screen. The inverse mistake is just as
   bad and is guarded below — routing push by channel must not silence the
   room-wide "an escalation is running" copy, which is about awareness rather
   than delivery and still counts everyone the level named.

Run:
  python3 test_escalation_channels.py
"""

import ast
import re
import sys
from pathlib import Path

_SRC = Path(__file__).with_name("main.py").read_text()
_TREE = ast.parse(_SRC)

# main.py imports FastAPI/SQLAlchemy/redis — absent outside the service image.
# Lift the pure pieces so this exercises the SHIPPED source, not a copy.
_WANTED = ("VALID_CHANNELS", "UNWIRED_CHANNELS", "_clean_channels")
_body = [n for n in _TREE.body
         if (isinstance(n, ast.FunctionDef) and n.name in _WANTED)
         or (isinstance(n, ast.Assign)
             and any(getattr(t, "id", None) in _WANTED for t in n.targets))]
_mod = ast.Module(body=_body, type_ignores=[])
ast.fix_missing_locations(_mod)
_ns: dict = {}
exec(compile(_mod, "main.py", "exec"), _ns)

clean = _ns.get("_clean_channels")
VALID_CHANNELS = _ns.get("VALID_CHANNELS")
UNWIRED_CHANNELS = _ns.get("UNWIRED_CHANNELS")

FAILURES = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        FAILURES.append(name)


def fire_level_source() -> str:
    fn = next((n for n in _TREE.body
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_fire_level"), None)
    assert fn is not None, "_fire_level has been renamed or removed"
    return ast.get_source_segment(_SRC, fn) or ""


def main():
    assert clean is not None, "_clean_channels has been renamed or removed"

    print("channel vocabulary")
    check("webhook is not a channel", "webhook" in VALID_CHANNELS, False)
    check("the four offered channels", sorted(VALID_CHANNELS),
          ["email", "push", "sms", "whatsapp"])
    check("sms/whatsapp declared unwired", sorted(UNWIRED_CHANNELS), ["sms", "whatsapp"])

    print("\n_clean_channels")
    check("drops webhook", clean(["push", "webhook"]), ["push"])
    check("drops anything unknown", clean(["push", "carrier-pigeon"]), ["push"])
    check("keeps order as given", clean(["email", "push"]), ["email", "push"])
    check("de-duplicates", clean(["push", "push", "email"]), ["push", "email"])
    check("normalizes case and spacing", clean([" Email ", "PUSH"]), ["email", "push"])
    # A recipient whose only channel was webhook would otherwise end up with an
    # EMPTY list — a row that notifies nobody while still looking configured.
    check("webhook-only falls back to in-app", clean(["webhook"]), ["push"])
    check("empty falls back to in-app", clean([]), ["push"])
    check("None falls back to in-app", clean(None), ["push"])
    check("sms is accepted (stored, not yet delivered)", clean(["sms"]), ["sms"])
    check("whatsapp is accepted", clean(["whatsapp"]), ["whatsapp"])

    print("\npush routing in _fire_level")
    src = fire_level_source()
    # The addressed copy must go to the push-only set...
    check("publish addresses push_targets",
          bool(re.search(r'"user_ids":\s*sorted\(push_targets\)', src)), True)
    check("push_targets is gated on the channel",
          bool(re.search(r'if\s+"push"\s+in\s+channels', src)), True)
    check("no longer publishes to every recipient",
          bool(re.search(r'"user_ids":\s*sorted\(notified\)', src)), False)
    # ...while the room-wide awareness copy still counts everyone.
    check("recipient_count counts all recipients",
          bool(re.search(r'"recipient_count":\s*len\(notified\)', src)), True)
    check("the publish still runs for an email-only level",
          bool(re.search(r'if\s+notified:\s*\n\s*try:', src)), True)
    # Rows are written for every channel; only push is 'sent' at write time.
    check("only push is marked sent on write",
          bool(re.search(r'"sent"\s+if\s+ch\s*==\s*"push"\s+else\s+"pending"', src)), True)
    check("recipients are cleaned before use",
          bool(re.search(r"channels\s*=\s*_clean_channels\(r\.channels\)", src)), True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} — {', '.join(FAILURES)}")
        return 1
    print("All escalation-channel checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
