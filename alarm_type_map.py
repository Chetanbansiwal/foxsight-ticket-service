"""Derive a ticket's `alarm_type` from the analytics payload.

Why this exists: `alarm_type` is the column the ticket list filters on and the
escalation matrix matches against (`match_event_types`), but the analytics
producer never set it. On the deployment box that left **3396 of 3550** tickets
untyped — every Loitering, Person Entry and Crowd alarm — so a filter or report
by alert type would have silently missed 96% of the alarms.

The type was recoverable, but only from the right field. Three candidates were
measured against real data:

* `alarm_type` — the correct home, empty on everything analytics produced.
* `alert_data.alert_type` — looks like a type, is actually the OPERATOR'S RULE
  NAME. Real values on the box: "Loitering rule", "intrusion rule", but also
  "crowd_61c_test", "object_left_zone_61g", "crowd_cam8_overlay". Renaming a
  rule would change the "type", and two operators naming the same rule
  differently would produce two types. Unusable as a category.
* `alert_data.vendor_metadata.events[].event_type` + `object_class` — machine
  generated, stable, independent of naming. This is what we use.

`event_type` alone is not enough: `roi_entry` covers BOTH intrusion and the
unattended-object rules (1057 + 35 tickets on the box). What separates them is
`object_class` — person vs abandoned_object/removed_object — which also splits
"left" from "removed", something the ticket title never did (it says
"Object left/removed in zone" for both).

Every value derived here is one that ALREADY exists in the vocabulary on the
box, so this classifies into the established set rather than inventing names.

One exception, and it is not a derivation: a rule can NAME the type of its
tickets (`alert_config.alarm_type`, carried as `vendor_metadata.alarm_type`).
That is a typed field the rule's author chose and analytics-service validated —
not the rule's display name the notes above rule out — and it is the only way
for rules whose meaning is their own: a track_sequence event says only that a
sequence completed, whether the rule watches for vehicles passing or a person
without a helmet. Guessing a type from that event (it was briefly
`vehicle_pass`) would mislabel every other use.
"""
import re
from typing import Any, Dict, Optional

# Same rule analytics-service applies when the type is saved; re-checked here so
# a malformed value from any producer falls back to derivation instead of
# landing in a VARCHAR(50) column the escalation matrix matches on.
_NAMED_TYPE = re.compile(r"^[a-z][a-z0-9_]{1,49}$")

# object_class -> alarm_type, for event types where the class is what
# distinguishes one alarm from another.
_ROI_ENTRY_BY_CLASS = {
    "abandoned_object": "object_left",
    "removed_object": "object_removed",
    "person": "intrusion",
}

# event_type -> alarm_type, where the event type alone is decisive.
_BY_EVENT_TYPE = {
    "dwell_time_exceeded": "loitering",
    "crowd_detected": "crowd_gathering",
    "line_crossed": "line_crossed",
    "line_crossing": "line_crossed",
    "region_exit": "region_exit",
    "tamper": "camera_tamper",
    "scene_change": "scene_change",
}


def _first_event_type(alert_data: Dict[str, Any]) -> Optional[str]:
    """The event_type of the first vendor event, if the payload carries one."""
    vm = alert_data.get("vendor_metadata")
    if not isinstance(vm, dict):
        return None
    events = vm.get("events")
    if not isinstance(events, list) or not events:
        return None
    first = events[0]
    if not isinstance(first, dict):
        return None
    et = first.get("event_type")
    return et.strip().lower() if isinstance(et, str) and et.strip() else None


def _object_class(alert_data: Dict[str, Any]) -> Optional[str]:
    vm = alert_data.get("vendor_metadata")
    if not isinstance(vm, dict):
        return None
    oc = vm.get("object_class")
    return oc.strip().lower() if isinstance(oc, str) and oc.strip() else None


def derive_alarm_type(alert_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """Best-effort alarm_type for an analytics payload, or None.

    None rather than a guess: an unknown alarm showing as "" is honest and
    filterable-around, whereas one mislabelled `intrusion` corrupts both the
    report and any escalation policy matching on event type. Callers keep
    whatever the producer sent — this only fills a gap.
    """
    if not isinstance(alert_data, dict):
        return None

    vm = alert_data.get("vendor_metadata")
    named = vm.get("alarm_type") if isinstance(vm, dict) else None
    if isinstance(named, str) and _NAMED_TYPE.match(named):
        return named

    event_type = _first_event_type(alert_data)
    if not event_type:
        return None

    if event_type == "roi_entry":
        # Intrusion vs unattended object live behind the same event; the object
        # class is the only thing that tells them apart.
        return _ROI_ENTRY_BY_CLASS.get(_object_class(alert_data) or "", "intrusion")

    return _BY_EVENT_TYPE.get(event_type)
