"""Controls for the server-side incident report PDF (clause 47.0).

The HTML report this replaces shipped an `<img>` whose source was empty on every
ticket: a document that claimed to carry the evidence frame went out blank, and
nothing anywhere said so. The failure was invisible precisely because "a report
was produced" and "the report shows the evidence" were never separate checks.
So they are separate here — a PDF that renders is not the same as a PDF that
contains the picture, and the absence case must SAY why rather than leave a gap.

Run:
  python3 test_incident_pdf.py
"""

import io
import re
import sys
import zlib

from incident_pdf import build_incident_pdf, _safe, _fmt_ts

FAILURES = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        FAILURES.append(name)


def check_true(name, cond, why=""):
    check(name + (f" ({why})" if why else ""), bool(cond), True)


T0 = 1_786_884_000  # fixed epoch; no wall-clock dependence


def sample_report(**over):
    r = {
        "generated_at": T0 + 60,
        "generated_by": "admin",
        "ticket": {
            "id": "abc-123", "ticket_number": "TKT-1786884000-abc123",
            "title": "Vandalism detected", "description": "Object struck the enclosure",
            "severity": "high", "status": "open", "alarm_type": "vandalism",
            "is_latched": True, "last_occurred_at": T0, "created_at": T0 - 3600,
            "detection_count": 3, "acknowledged_at": None, "acknowledged_by": None,
            "assigned_to": None, "resolved_at": None, "sla_breach": False,
        },
        "camera": {"id": 6, "name": "P3_224", "location": "North gate"},
        "escalation": {"level": 2, "escalated_at": T0 + 300},
        "timeline": [
            {"ts": T0, "kind": "status", "actor": "system", "detail": "→ open"},
            {"ts": T0 + 1, "kind": "escalation", "actor": "system",
             "detail": "Escalation L1 fired → 2 recipient(s)"},
            {"ts": T0 + 2, "kind": "notification", "actor": "admin", "detail": "email → sent"},
        ],
    }
    r["ticket"].update(over.pop("ticket", {}))
    r.update(over)
    return r


def jpeg_bytes(w=320, h=180):
    """A real JPEG — the renderer asks Pillow for its dimensions, so a fake
    byte string would pass a length check while failing the thing that matters."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 80, 120)).save(buf, format="JPEG")
    return buf.getvalue()


def pdf_text(pdf: bytes) -> str:
    """Concatenate the decompressed content streams so assertions look at what
    the document actually SAYS, not at the dict we passed in."""
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        blob = m.group(1)
        try:
            out.append(zlib.decompress(blob).decode("latin-1", "replace"))
        except Exception:
            out.append(blob.decode("latin-1", "replace"))
    return "\n".join(out)


def main():
    print("renders at all")
    img = jpeg_bytes()
    pdf = build_incident_pdf(sample_report(), img, "", "UTC")
    check_true("produces a PDF", pdf.startswith(b"%PDF-"), "magic bytes")
    check_true("is not trivially empty", len(pdf) > 2000, f"{len(pdf)} bytes")
    check_true("terminates properly", pdf.rstrip().endswith(b"%%EOF"))

    print("\ncarries the incident facts")
    text = pdf_text(pdf)
    for label, needle in (("ticket number", "TKT-1786884000-abc123"),
                          ("title", "Vandalism detected"),
                          ("camera", "P3_224"),
                          ("location", "North gate"),
                          ("severity", "HIGH"),
                          ("evidence heading", "EVIDENCE"),
                          ("timeline heading", "AUDIT TIMELINE")):
        check_true(f"contains the {label}", needle in text)

    print("\nTHE regression: the image is actually embedded")
    # A JPEG is stored as a DCTDecode image XObject. Its absence is exactly the
    # bug the HTML report had — everything else present, no picture.
    check_true("embeds a JPEG image stream", b"/DCTDecode" in pdf)
    check_true("declares an image XObject", b"/Subtype /Image" in pdf or b"/Subtype/Image" in pdf)
    # And the with-image document must be materially bigger than the without.
    NOTE = "No recorded footage covers this moment"
    bare = build_incident_pdf(sample_report(), None, NOTE + ".", "UTC")
    check_true("image adds real bytes", len(pdf) > len(bare) + 1000,
               f"{len(pdf)} vs {len(bare)}")

    print("\nabsence is stated, never left blank")
    bare_text = pdf_text(bare)
    check_true("no image stream when there is no frame", b"/DCTDecode" not in bare)
    check_true("the caller's reason is printed verbatim", NOTE in bare_text)
    # An empty note must still produce a sentence — silence here is the original
    # bug, where the section simply showed nothing.
    silent = pdf_text(build_incident_pdf(sample_report(), None, "", "UTC"))
    check_true("an empty note falls back to a stated default",
               "No evidence frame is available" in silent)
    # The caption must NOT claim a frame exists when none was embedded.
    check_true("no frame caption without a frame", "Frame recorded at" not in bare_text)
    check_true("caption present when there IS a frame", "Frame recorded at" in text)

    print("\ndegrades instead of failing")
    # Corrupt image bytes: Pillow raises, and the report must still be produced.
    broken = build_incident_pdf(sample_report(), b"not-a-jpeg-at-all", "", "UTC")
    check_true("survives a corrupt frame", broken.startswith(b"%PDF-"))
    check_true("says the frame failed", "could not be embedded" in pdf_text(broken))

    # Text outside latin-1 must not raise — core fonts cannot encode it.
    uni = build_incident_pdf(
        sample_report(ticket={"title": "प्रवेश — Zone 3 · café", "description": "тест"}),
        img, "", "UTC")
    check_true("survives non-latin-1 text", uni.startswith(b"%PDF-"))

    print("\nhelpers")
    check("_safe replaces unencodable chars", _safe("café प्रवेश").encode("latin-1") is not None, True)
    check("_safe handles None", _safe(None), "")
    check("_fmt_ts labels its zone", _fmt_ts(T0, "UTC").endswith("UTC"), True)
    check("_fmt_ts on a missing time", _fmt_ts(None, "UTC"), "—")
    check("_fmt_ts on zero is not a date", _fmt_ts(0, "UTC"), "—")
    # Different zones must render different clock times for the same instant.
    check_true("zone actually applied",
               _fmt_ts(T0, "UTC") != _fmt_ts(T0, "Asia/Kolkata"))

    print("\nempty timeline")
    quiet = build_incident_pdf(sample_report(timeline=[]), img, "", "UTC")
    check_true("still renders", quiet.startswith(b"%PDF-"))
    check_true("says there was no activity", "No recorded activity" in pdf_text(quiet))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} — {', '.join(FAILURES)}")
        return 1
    print("All incident-PDF checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
