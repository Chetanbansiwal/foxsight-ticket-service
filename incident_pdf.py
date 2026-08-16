"""Server-side incident report PDF (clause 47.0) — the printable evidence record.

Why this exists server-side at all: the printable report has always been an HTML
page the OPERATOR'S BROWSER prints. That is fine when a person is looking at it,
and useless to anything automated — an escalation email cannot open a browser.
So the same record is rendered here, with no headless browser involved.

Two things this file is deliberately careful about:

* **An evidence section with no evidence.** The HTML report shipped an `<img>`
  whose source was empty on every ticket, so a report that claimed to carry the
  evidence frame went out blank and nothing said so. Here a missing frame is
  stated in words, with the reason, and never rendered as an empty box.

* **Text the core fonts cannot encode.** fpdf's built-in fonts are latin-1 only,
  and this deployment has camera names and descriptions that are not. Encoding
  is therefore forced through a lossy-but-safe filter rather than being allowed
  to raise mid-render — a report that fails to generate is worse than one with a
  substituted character, and the alternative (shipping a Unicode TTF) is a
  licensing and image-size decision, not a formatting one.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any, Dict, Optional

from fpdf import FPDF
from fpdf.enums import XPos, YPos

# A4 with 15mm margins.
CONTENT_W = 180.0
LABEL_W = 42.0

SEV_RGB = {
    "critical": (185, 28, 28),
    "high": (194, 65, 12),
    "medium": (161, 98, 7),
    "low": (71, 85, 105),
}


def _safe(text: Any) -> str:
    """Text the built-in fonts can actually render.

    `latin-1` is the core-font codepage; anything outside it raises inside fpdf
    at draw time, which would turn one Devanagari camera name into a 500 on the
    whole report. Replace rather than fail, and keep it in ONE place so no call
    site can forget.
    """
    if text is None:
        return ""
    return str(text).encode("latin-1", "replace").decode("latin-1")


def _fmt_ts(ts: Optional[float], tz_name: str) -> str:
    """Absolute time, always carrying its zone.

    An unlabelled timestamp on an evidence document is a liability: the box runs
    UTC while the people reading this work in local time, and a report that says
    only "00:55" invites the reader to assume whichever one suits them.
    """
    if not ts:
        return "—"
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(float(ts), ZoneInfo(tz_name or "UTC"))
        label = dt.strftime("%Z") or (tz_name or "UTC")
    except Exception:
        dt = datetime.utcfromtimestamp(float(ts))
        label = "UTC"
    return f"{dt.strftime('%d %b %Y, %H:%M:%S')} {label}"


class _Report(FPDF):
    def __init__(self, ticket_number: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.ticket_number = ticket_number
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(15, 15, 15)

    def footer(self):  # noqa: D102 - fpdf hook
        self.set_y(-15)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(120, 120, 120)
        self.cell(
            0, 5,
            _safe(f"{self.ticket_number}  ·  system-generated evidence record  ·  page {self.page_no()}"
                  f" of {{nb}}"),
            align="C",
        )
        self.set_text_color(0, 0, 0)


def _kv(pdf: _Report, label: str, value: str) -> None:
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(LABEL_W, 5.5, _safe(label))
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(0, 0, 0)
    pdf.multi_cell(CONTENT_W - LABEL_W, 5.5, _safe(value or "—"),
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _heading(pdf: _Report, text: str) -> None:
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(241, 245, 249)
    pdf.set_text_color(30, 41, 59)
    pdf.cell(CONTENT_W, 7, _safe(f"  {text}"), fill=True,
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)


def build_incident_pdf(
    report: Dict[str, Any],
    evidence_jpeg: Optional[bytes] = None,
    evidence_note: str = "",
    tz_name: str = "UTC",
) -> bytes:
    """Render the report payload (same dict the JSON endpoint returns) to PDF.

    `evidence_note` explains an absent frame; it is shown verbatim when
    `evidence_jpeg` is None so the reader learns WHY there is no picture rather
    than being left to wonder whether one was withheld.
    """
    t = report.get("ticket") or {}
    cam = report.get("camera") or {}
    esc = report.get("escalation") or {}
    num = str(t.get("ticket_number") or t.get("id") or "INCIDENT")

    pdf = _Report(num)
    pdf.alias_nb_pages()
    pdf.add_page()

    # ── Title bar ────────────────────────────────────────────────────────────
    sev = str(t.get("severity") or "").lower()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(CONTENT_W - 55, 10, _safe("INCIDENT REPORT"))
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*SEV_RGB.get(sev, (71, 85, 105)))
    pdf.cell(55, 10, _safe(f"{sev.upper() or 'UNSPECIFIED'}"), align="R",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(CONTENT_W, 5, _safe(num), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(1)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(3)

    # ── Incident ─────────────────────────────────────────────────────────────
    _heading(pdf, "INCIDENT")
    _kv(pdf, "Title", t.get("title"))
    if t.get("description"):
        _kv(pdf, "Description", t.get("description"))
    _kv(pdf, "Alarm type", t.get("alarm_type"))
    _kv(pdf, "Status", f"{t.get('status') or '—'}"
                       f"{'  (LATCHED — still asserted)' if t.get('is_latched') else ''}")
    _kv(pdf, "Occurred at", _fmt_ts(t.get("last_occurred_at"), tz_name))
    if t.get("created_at") and t.get("created_at") != t.get("last_occurred_at"):
        _kv(pdf, "First raised", _fmt_ts(t.get("created_at"), tz_name))
    if t.get("detection_count"):
        _kv(pdf, "Occurrences", str(t.get("detection_count")))

    # ── Source ───────────────────────────────────────────────────────────────
    _heading(pdf, "SOURCE")
    _kv(pdf, "Camera", cam.get("name") or (f"Camera {cam.get('id')}" if cam.get("id") else None))
    _kv(pdf, "Location", cam.get("location"))

    # ── Response ─────────────────────────────────────────────────────────────
    _heading(pdf, "RESPONSE")
    _kv(pdf, "Acknowledged", (f"{t.get('acknowledged_by')} at {_fmt_ts(t.get('acknowledged_at'), tz_name)}"
                              if t.get("acknowledged_at") else "Not acknowledged"))
    _kv(pdf, "Assigned to", t.get("assigned_to"))
    _kv(pdf, "Resolved at", _fmt_ts(t.get("resolved_at"), tz_name) if t.get("resolved_at") else "Not resolved")
    if esc.get("level"):
        _kv(pdf, "Escalation", f"Level {esc.get('level')}"
                               f"{' — escalated ' + _fmt_ts(esc.get('escalated_at'), tz_name) if esc.get('escalated_at') else ''}")
    if t.get("sla_breach"):
        pdf.set_text_color(185, 28, 28)
        _kv(pdf, "SLA", "BREACHED")
        pdf.set_text_color(0, 0, 0)

    # ── Evidence ─────────────────────────────────────────────────────────────
    _heading(pdf, "EVIDENCE")
    if evidence_jpeg:
        try:
            from PIL import Image
            with Image.open(io.BytesIO(evidence_jpeg)) as im:
                iw, ih = im.size
            draw_w = min(150.0, CONTENT_W)
            draw_h = draw_w * (ih / iw) if iw else draw_w * 0.5625
            # Keep the frame and its caption on one page — an evidence image
            # orphaned from the timestamp that identifies it is not evidence.
            if pdf.get_y() + draw_h + 14 > 279:
                pdf.add_page()
            x = 15 + (CONTENT_W - draw_w) / 2
            pdf.image(io.BytesIO(evidence_jpeg), x=x, y=pdf.get_y(), w=draw_w, h=draw_h)
            pdf.set_y(pdf.get_y() + draw_h + 2)
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(110, 110, 110)
            pdf.multi_cell(
                CONTENT_W, 4.5,
                _safe(f"Frame recorded at {_fmt_ts(t.get('last_occurred_at'), tz_name)} on "
                      f"{cam.get('name') or 'the source camera'}. Extracted from the stored "
                      f"recording; not a re-encode of a live view."),
                align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT,
            )
            pdf.set_text_color(0, 0, 0)
        except Exception as e:  # a broken frame must not lose the whole report
            pdf.set_font("Helvetica", "I", 9)
            pdf.multi_cell(CONTENT_W, 5,
                           _safe(f"Evidence frame could not be embedded ({type(e).__name__})."),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    else:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(120, 120, 120)
        pdf.multi_cell(
            CONTENT_W, 5,
            _safe(evidence_note or "No evidence frame is available for this incident."),
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )
        pdf.set_text_color(0, 0, 0)

    # ── Timeline ─────────────────────────────────────────────────────────────
    timeline = report.get("timeline") or []
    _heading(pdf, f"AUDIT TIMELINE ({len(timeline)} entries)")
    if not timeline:
        pdf.set_font("Helvetica", "I", 9)
        pdf.multi_cell(CONTENT_W, 5, _safe("No recorded activity."),
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    else:
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(248, 250, 252)
        for w, h in ((46, "When"), (24, "Kind"), (26, "Actor"), (84, "Detail")):
            pdf.cell(w, 5.5, _safe(h), border="B", fill=True)
        pdf.ln(5.5)
        pdf.set_font("Helvetica", "", 8)
        for e in timeline:
            if pdf.get_y() > 265:
                pdf.add_page()
            y0 = pdf.get_y()
            pdf.cell(46, 5, _safe(_fmt_ts(e.get("ts"), tz_name)))
            pdf.cell(24, 5, _safe(e.get("kind")))
            pdf.cell(26, 5, _safe(e.get("actor")))
            pdf.set_xy(15 + 46 + 24 + 26, y0)
            pdf.multi_cell(84, 5, _safe(e.get("detail")),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ── Provenance ───────────────────────────────────────────────────────────
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 7.5)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(
        CONTENT_W, 4,
        _safe(f"Generated {_fmt_ts(report.get('generated_at'), tz_name)} by "
              f"{report.get('generated_by') or 'the system'}. Times are shown in "
              f"{tz_name or 'UTC'}. This record is produced from the stored audit trail; "
              f"entries cannot be edited after the fact."),
        new_x=XPos.LMARGIN, new_y=YPos.NEXT,
    )
    pdf.set_text_color(0, 0, 0)

    out = pdf.output()
    return bytes(out)
