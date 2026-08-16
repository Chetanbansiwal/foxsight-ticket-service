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
_WANTED = ("VALID_CHANNELS", "UNWIRED_CHANNELS", "IN_APP_CHANNELS",
           "LEGACY_CHANNEL_ALIASES", "_DEFAULT_POLICIES", "_clean_channels")
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
IN_APP_CHANNELS = _ns.get("IN_APP_CHANNELS")
DEFAULT_POLICIES = _ns.get("_DEFAULT_POLICIES")

FAILURES = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        FAILURES.append(name)


def check_true(name, cond):
    check(name, bool(cond), True)


def fire_level_source() -> str:
    fn = next((n for n in _TREE.body
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_fire_level"), None)
    assert fn is not None, "_fire_level has been renamed or removed"
    return ast.get_source_segment(_SRC, fn) or ""


def main():
    assert clean is not None, "_clean_channels has been renamed or removed"

    print("channel vocabulary")
    check("webhook is not a channel", "webhook" in VALID_CHANNELS, False)
    check("the five offered channels", sorted(VALID_CHANNELS),
          ["email", "popup", "sms", "toast", "whatsapp"])
    check("in-app is two distinct presentations", sorted(IN_APP_CHANNELS),
          ["popup", "toast"])
    # `push` is no longer a channel in its own right — it is what old policies
    # hold, and it must keep meaning what it did: a popup.
    check("push is not offered", "push" in VALID_CHANNELS, False)
    check("sms/whatsapp declared unwired", sorted(UNWIRED_CHANNELS), ["sms", "whatsapp"])

    print("\n_clean_channels")
    check("drops webhook", clean(["toast", "webhook"]), ["toast"])
    check("drops anything unknown", clean(["toast", "carrier-pigeon"]), ["toast"])
    check("keeps order as given", clean(["email", "toast"]), ["email", "toast"])
    check("de-duplicates", clean(["toast", "toast", "email"]), ["toast", "email"])
    check("normalizes case and spacing", clean([" Email ", "TOAST"]), ["email", "toast"])
    # Legacy migration: a stored `push` must keep behaving as it did.
    check("legacy push becomes popup", clean(["push"]), ["popup"])
    check("legacy push de-dupes against popup", clean(["push", "popup"]), ["popup"])
    check("toast and popup can coexist", clean(["toast", "popup"]), ["toast", "popup"])
    # A recipient whose only channel was webhook would otherwise end up with an
    # EMPTY list — a row that notifies nobody while still looking configured.
    check("webhook-only falls back to in-app", clean(["webhook"]), ["popup"])
    check("empty falls back to in-app", clean([]), ["popup"])
    check("None falls back to in-app", clean(None), ["popup"])
    check("sms is accepted (stored, not yet delivered)", clean(["sms"]), ["sms"])
    check("whatsapp is accepted", clean(["whatsapp"]), ["whatsapp"])

    print("\npush routing in _fire_level")
    src = fire_level_source()
    # The addressed copy must go to the push-only set...
    check("popup list is addressed",
          bool(re.search(r'"popup_user_ids":\s*sorted\(popup_targets\)', src)), True)
    check("toast list is addressed",
          bool(re.search(r'"toast_user_ids":\s*sorted\(toast_targets\)', src)), True)
    check("popup is gated on the channel",
          bool(re.search(r'if\s+"popup"\s+in\s+channels', src)), True)
    check("toast is gated on the channel",
          bool(re.search(r'elif\s+"toast"\s+in\s+channels', src)), True)
    check("no longer publishes to every recipient",
          bool(re.search(r'"user_ids":\s*sorted\(notified\)', src)), False)
    # The ambient copy is zone-filtered downstream by camera_id; without it the
    # payload reads as system-wide and reaches operators who cannot see the camera.
    check("camera_id is published for zone filtering",
          bool(re.search(r'"camera_id":\s*ticket\.camera_id', src)), True)
    # ...while the room-wide awareness copy still counts everyone.
    check("recipient_count counts all recipients",
          bool(re.search(r'"recipient_count":\s*len\(notified\)', src)), True)
    check("the publish still runs for an email-only level",
          bool(re.search(r'if\s+notified:\s*\n\s*try:', src)), True)
    # Rows are written for every channel; only push is 'sent' at write time.
    check("only in-app is marked sent on write",
          bool(re.search(r'"sent"\s+if\s+in_app\s+else\s+"pending"', src)), True)
    check("in-app membership decides that",
          bool(re.search(r'in_app\s*=\s*ch\s+in\s+IN_APP_CHANNELS', src)), True)
    check("recipients are cleaned before use",
          bool(re.search(r"channels\s*=\s*_clean_channels\(r\.channels\)", src)), True)

    print("\ndefault policies route what no rule claims")
    # Before this, an alarm matching no policy was presented by a rule hardcoded
    # in the browser — low/medium a toast, high/critical seizing the screen —
    # which nothing in the product disclosed and no operator could change.
    names = [p["name"] for p in DEFAULT_POLICIES]
    check("two defaults", len(DEFAULT_POLICIES), 2)
    check("names are distinct", len(set(names)), 2)
    # The catch-all MUST match everything, or the gap it exists to close remains.
    catch_all = [p for p in DEFAULT_POLICIES if p["match_severity"] is None]
    check("exactly one unconditional catch-all", len(catch_all), 1)
    check("the catch-all is the lowest priority", catch_all[0]["priority"],
          min(p["priority"] for p in DEFAULT_POLICIES))
    # Severity ordering: the high/critical policy must outrank the catch-all,
    # otherwise the catch-all wins first and nothing ever gets a popup.
    high = [p for p in DEFAULT_POLICIES if p["match_severity"] == "high"]
    check("a high/critical default exists", len(high), 1)
    check_true("high outranks the catch-all",
               high[0]["priority"] > catch_all[0]["priority"])
    check("high/critical interrupts", high[0]["channels"], ["popup"])
    check("everything else is a toast", catch_all[0]["channels"], ["toast"])
    for p in DEFAULT_POLICIES:
        check_true(f"{p['name']!r} uses a real channel",
                   all(c in VALID_CHANNELS for c in p["channels"]))

    print("\nzone_any addressing")
    src_all = _SRC
    check_true("the engine resolves zone_any",
               bool(re.search(r'if\s+r\.recipient_type\s*==\s*"zone_any"', src_all)))
    check_true("test-fire resolves it too — a dry run that shows nobody is a lie",
               src_all.count('recipient_type == "zone_any"') >= 2)
    check_true("zone access filtering is shared, not duplicated",
               bool(re.search(r"async def _filter_by_zone_access", src_all)))
    check_true("_recipients_in_zone uses it",
               bool(re.search(r"async def _recipients_in_zone[\s\S]{0,900}_filter_by_zone_access",
                              src_all)))
    check_true("_recipients_for_role uses it too",
               bool(re.search(r"async def _recipients_for_role[\s\S]{0,900}_filter_by_zone_access",
                              src_all)))
    # The default policy must address the zone audience, not a single role.
    seed = re.search(r"async def _ensure_default_policies[\s\S]*?\n\n\n", src_all)
    check_true("the seed addresses zone_any",
               bool(seed and 'recipient_type="zone_any"' in seed.group(0)))
    check_true("the seed is idempotent by name",
               bool(seed and "EscalationPolicy.name.in_(names)" in seed.group(0)))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} — {', '.join(FAILURES)}")
        return 1
    print("All escalation-channel checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
