#!/usr/bin/env python3
"""Build the per-source legal analysis as a PDF.

For every source the tracker fetches, one line on why ACCESS (scraping) is permissible and
one line on why REPRODUCTION (copyright) is permissible — reasoned independently, because a
source can be lawful to read but not to copy. Opens with the cross-cutting principles the
whole estate stands on. Crisp and to the point, for a partner to sign off.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, PageBreak,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "engine" / "audit" / "legal_analysis_2026-08-27.json"
OUT = HERE / "TMT-Radar-legal-basis.pdf"

NAVY = colors.HexColor("#00405C")
INK = colors.HexColor("#14181B")
BODY = colors.HexColor("#2E353A")
MUTE = colors.HexColor("#5A646B")
RULE = colors.HexColor("#C9D2D8")
SOFT = colors.HexColor("#E7ECEF")
BG = colors.HexColor("#F4F7F8")
OK = colors.HexColor("#1B6B4A")
OK_BG = colors.HexColor("#E6F1EB")
OCHRE = colors.HexColor("#7E6410")
OCHRE_BG = colors.HexColor("#F6F0DC")
ALARM = colors.HexColor("#8A2B1C")
ALARM_BG = colors.HexColor("#F7E9E6")


def S(name, **kw):
    from reportlab.lib.styles import ParagraphStyle
    base = dict(fontName="Times-Roman", fontSize=9, leading=12.2, textColor=BODY, alignment=TA_LEFT)
    base.update(kw)
    return ParagraphStyle(name, **base)


TITLE = S("t", fontName="Times-Bold", fontSize=20, leading=23, textColor=INK, spaceAfter=3)
SUB = S("s", fontSize=10.5, leading=14, textColor=MUTE, spaceAfter=10)
H2 = S("h2", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=NAVY, spaceBefore=15, spaceAfter=6)
P = S("p", spaceAfter=7)
SMALL = S("sm", fontSize=8, leading=11, textColor=MUTE)
PRIN = S("pr", fontSize=8.6, leading=11.6, spaceAfter=6)
GRP = S("g", fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=INK, spaceBefore=10, spaceAfter=3)
AUTH = S("a", fontName="Times-Bold", fontSize=8.8, leading=11, textColor=INK)
LABL = S("l", fontName="Helvetica-Bold", fontSize=6.6, leading=8.4, textColor=NAVY)
CELL = S("c", fontSize=8, leading=10.4, textColor=BODY)
BADGE = S("b", fontName="Helvetica-Bold", fontSize=6.4, leading=8, textColor=colors.white, alignment=1)


def esc(t: str) -> str:
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


POSTURE = {
    "include": ("INCLUDE", OK),
    "include_with_conditions": ("CONDITIONS", OCHRE),
    "include_metadata_only": ("METADATA ONLY", NAVY),
}


def family(sid: str, authority: str) -> str:
    a = authority.lower()
    for key, name in [
        ("e-gazette", "e-Gazette — the cornerstone"), ("gazette", "e-Gazette — the cornerstone"),
        ("trai", "TRAI"), ("meity", "MeitY"), ("pib", "PIB"),
        ("cert-in", "CERT-In"), ("cert", "CERT-In"), ("nccs", "NCCS"), ("tec", "TEC / MTCTE"),
        ("mib", "MIB"), ("cbfc", "CBFC"), ("press registrar", "PRGI"), ("prgi", "PRGI"),
        ("ascionline", "ASCI"), ("asci", "ASCI"), ("in-space", "IN-SPACe"), ("inspace", "IN-SPACe"),
        ("tdsat", "TDSAT"), ("competition", "CCI"), ("cci", "CCI"),
        ("consumer", "CCPA"), ("ccpa", "CCPA"), ("dpiit", "DPIIT"),
        ("supreme court", "Courts"), ("high court", "Courts"), ("nclat", "Courts"),
    ]:
        if key in a:
            return name
    return authority.split("(")[0].strip()[:24]


def posture_badge(posture: str) -> Table:
    label, colr = POSTURE.get(posture, ("REVIEW", MUTE))
    t = Table([[Paragraph(label, BADGE)]], colWidths=[26 * mm], rowHeights=[7 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colr),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return t


def source_block(a: dict) -> Table:
    access = a.get("access_one_line", "")
    copyr = a.get("copyright_one_line", "")
    cond = a.get("conditions", "")
    left = [
        [Paragraph(esc(a.get("authority", a.get("id", ""))), AUTH)],
        [Paragraph("ACCESS", LABL)], [Paragraph(esc(access), CELL)],
        [Paragraph("COPYRIGHT", LABL)], [Paragraph(esc(copyr), CELL)],
    ]
    if cond and cond.lower() not in ("none", "-", ""):
        left += [[Paragraph("CONDITIONS", LABL)], [Paragraph(esc(cond), CELL)]]
    lt = Table(left, colWidths=[150 * mm])
    lt.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                            ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
    outer = Table([[lt, posture_badge(a.get("posture", ""))]], colWidths=[152 * mm, 28 * mm])
    outer.setStyle(TableStyle([
        ("VALIGN", (0, 0), (0, 0), "TOP"), ("VALIGN", (1, 0), (1, 0), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, SOFT),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return outer


def header_footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(NAVY)
    canvas.setLineWidth(2)
    canvas.line(18 * mm, 285 * mm, 192 * mm, 285 * mm)
    canvas.setFont("Helvetica-Bold", 6.6)
    canvas.setFillColor(NAVY)
    canvas.drawString(18 * mm, 287 * mm, "TMT REGULATORY RADAR  ·  LEGAL BASIS PER SOURCE")
    canvas.drawRightString(192 * mm, 287 * mm, "PRIVILEGED & CONFIDENTIAL  ·  NOT LEGAL ADVICE")
    canvas.setFont("Helvetica", 6.6)
    canvas.setFillColor(MUTE)
    canvas.drawString(18 * mm, 11 * mm, "Analysis as at 27 August 2026. Terms change; re-verify before relying on this.")
    canvas.drawRightString(192 * mm, 11 * mm, f"Page {doc.page}")
    canvas.restoreState()


def condense(principle: str) -> tuple:
    """Split 'HEADER — body' into (header, body) for display."""
    m = re.match(r"^([A-Z][A-Z /,.'&()-]+?)(?:\s*[—:-]\s+)(.*)$", principle.strip(), re.S)
    if m:
        return m.group(1).strip().title(), m.group(2).strip()
    return "", principle.strip()


def build() -> None:
    data = json.loads(SRC.read_text())
    so = data["signoff"]
    approved = so["approved"]
    excluded = so.get("excluded", [])
    principles = so.get("principles", [])

    doc = BaseDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                          topMargin=22 * mm, bottomMargin=16 * mm,
                          title="TMT Radar — Legal Basis per Source", author="TMT Regulatory Radar")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f")
    doc.addPageTemplates([PageTemplate(id="all", frames=[frame], onPage=header_footer)])

    st: List = []
    st.append(Paragraph("Why each source is lawful to scrape and to reproduce", TITLE))
    st.append(Paragraph("For every source the tracker collects from, the access question (is the "
                        "scraping lawful?) and the copyright question (is the reproduction lawful?) "
                        "reasoned separately. The cross-cutting principles come first; the per-source "
                        "lines apply them.", SUB))

    # bottom line
    bl = Table([[Paragraph("<b>Bottom line.</b> " + esc(so.get("summary", "")[:900]), S("bl", fontSize=9, leading=12.5, textColor=INK))]],
               colWidths=[174 * mm])
    bl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), OK_BG), ("LINEBEFORE", (0, 0), (0, -1), 2.4, OK),
                            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                            ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
    st.append(bl)

    st.append(Paragraph("THE PRINCIPLES THE WHOLE ESTATE STANDS ON", H2))
    for i, pr in enumerate(principles, 1):
        head, body = condense(pr)
        txt = (f"<b>{i}. {esc(head)}.</b> " if head else f"<b>{i}.</b> ") + esc(body)
        st.append(Paragraph(txt, PRIN))

    st.append(PageBreak())
    st.append(Paragraph("SOURCE BY SOURCE", H2))
    st.append(Paragraph("Each source with its one-line access rationale, one-line copyright rationale, "
                        "and posture — <b>Include</b>, <b>Conditions</b>, or <b>Metadata only</b> "
                        "(the dashboard stores only bibliographic facts; no document is archived).", SMALL))
    st.append(Spacer(1, 4))

    # group by family, cornerstone first
    groups: dict = {}
    for a in approved:
        groups.setdefault(family(a.get("id", ""), a.get("authority", "")), []).append(a)
    order = ["e-Gazette — the cornerstone", "TRAI", "MeitY", "PIB", "CERT-In", "NCCS", "TEC / MTCTE",
             "MIB", "CBFC", "PRGI", "ASCI", "IN-SPACe", "TDSAT", "Courts", "CCI", "CCPA", "DPIIT"]
    ordered = [g for g in order if g in groups] + [g for g in groups if g not in order]

    for g in ordered:
        block = [Paragraph(esc(g), GRP)]
        for a in groups[g]:
            block.append(source_block(a))
        st.append(KeepTogether(block) if len(groups[g]) <= 2 else block[0])
        if len(groups[g]) > 2:
            for a in groups[g]:
                st.append(source_block(a))

    # excluded
    if excluded:
        st.append(Paragraph("EXCLUDED — NOT COLLECTED", H2))
        for x in excluded:
            card = Table([[Paragraph("<b>" + esc(x.get("authority", x.get("id", ""))) + "</b> — " + esc(x.get("why", "")), CELL)]],
                         colWidths=[174 * mm])
            card.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ALARM_BG), ("LINEBEFORE", (0, 0), (0, -1), 2.4, ALARM),
                                      ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                                      ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
            st.append(card)
            st.append(Spacer(1, 5))

    # caveats
    st.append(Paragraph("THE LIMITS OF THIS ANALYSIS", H2))
    st.append(Paragraph(
        "This is a compliance analysis of publicly stated terms and the applicable statute as at "
        "27 August 2026, prepared to inform the firm's own posture; it is not a legal opinion and "
        "has not been settled by counsel. Two questions are genuinely unsettled in Indian law and are "
        "flagged rather than smoothed over: (a) no court has ruled on whether scraping publicly "
        "available data is 'without permission' under s.43 of the IT Act; and (b) the enforceability "
        "of browsewrap website terms absent assent is unresolved. The posture rests on express or "
        "implied permission plus the absence of any recoverable loss, and on the s.52 permitted-act "
        "and fair-dealing grounds — the latter, as construed in ANI Media v OpenAI, being an interim "
        "prima facie ruling. Every instrument must still be verified against the gazette text before "
        "client advice.", SMALL))

    doc.build(st)
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    build()
