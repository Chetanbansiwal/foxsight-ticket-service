"""alarm_type derivation — the cases that came off the real box.

Counts in the comments are what was measured on the deployment box on
2026-08-19, when 3396 of 3550 tickets had no alarm_type at all.

    python3 test_alarm_type_map.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from alarm_type_map import derive_alarm_type  # noqa: E402


def vm(event_type=None, object_class=None, **extra):
    """An alert_data payload shaped like the analytics producer's."""
    meta = {}
    if event_type is not None:
        meta["events"] = [{"event_type": event_type, "track_id": "t1"}]
    if object_class is not None:
        meta["object_class"] = object_class
    meta.update(extra)
    return {"alert_type": "some operator rule name", "vendor_metadata": meta}


CASES = [
    # (label, alert_data, expected)
    ("loitering (1765 on box)", vm("dwell_time_exceeded", "person"), "loitering"),
    ("intrusion (1057 on box)", vm("roi_entry", "person"), "intrusion"),
    ("crowd (539 on box)", vm("crowd_detected", "person"), "crowd_gathering"),
    ("object left (19 on box)", vm("roi_entry", "abandoned_object"), "object_left"),
    ("object removed (16 on box)", vm("roi_entry", "removed_object"), "object_removed"),
    ("line crossing", vm("line_crossed", "person"), "line_crossed"),
    ("tamper", vm("tamper"), "camera_tamper"),

    # Nothing to go on -> None, never a guess. A mislabelled alarm corrupts both
    # the report and any escalation policy matching on event type.
    ("no payload", None, None),
    ("empty payload", {}, None),
    ("no vendor_metadata", {"alert_type": "x"}, None),
    ("no events list", {"vendor_metadata": {"object_class": "person"}}, None),
    ("empty events list", {"vendor_metadata": {"events": []}}, None),
    ("unknown event type", vm("something_new", "person"), None),
    ("malformed event entry", {"vendor_metadata": {"events": ["not-a-dict"]}}, None),
    ("alert_data not a dict", "a string", None),

    # roi_entry with an unfamiliar class still means someone entered a region.
    ("roi_entry unknown class", vm("roi_entry", "vehicle"), "intrusion"),
    ("roi_entry no class", vm("roi_entry"), "intrusion"),

    # Case and whitespace come from a vendor payload, not from us.
    ("upper case event", vm("ROI_ENTRY", "PERSON"), "intrusion"),
    ("padded strings", vm("  dwell_time_exceeded  ", " person "), "loitering"),
]


def main():
    failures = []
    for label, payload, expected in CASES:
        try:
            got = derive_alarm_type(payload)
        except Exception as e:  # a classifier must never take the request down
            failures.append(f"{label}: raised {type(e).__name__}: {e}")
            continue
        if got != expected:
            failures.append(f"{label}: expected {expected!r}, got {got!r}")

    # The rule name is operator free-text ("crowd_61c_test", "object_left_zone_61g"
    # were real values). It must never influence the result.
    misleading = vm("roi_entry", "person")
    misleading["alert_type"] = "Loitering rule"
    if derive_alarm_type(misleading) != "intrusion":
        failures.append("the operator's rule name influenced the derived type")

    for f in failures:
        print(f"FAIL: {f}")
    if failures:
        print(f"\n{len(failures)} of {len(CASES) + 1} failed")
        return 1
    print(f"OK: {len(CASES) + 1} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
