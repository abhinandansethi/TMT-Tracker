#!/usr/bin/env python3
"""Build the scraping compliance report as a PDF.

Every finding is tied to the exact page it came from and quoted verbatim, so the report
can be checked against the source rather than believed.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, PageBreak,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)

OUT = Path(__file__).resolve().parent / "TMT-Radar-scraping-compliance.pdf"

NAVY = colors.HexColor("#00405C")
INK = colors.HexColor("#14181B")
BODY = colors.HexColor("#2E353A")
MUTE = colors.HexColor("#5A646B")
RULE = colors.HexColor("#C9D2D8")
ALARM = colors.HexColor("#8A2B1C")
ALARM_BG = colors.HexColor("#F9EDEA")
OCHRE = colors.HexColor("#7E6410")
QUOTE_BG = colors.HexColor("#F4F7F8")
OK = colors.HexColor("#1B6B4A")

ss = getSampleStyleSheet()


def S(name, **kw) -> ParagraphStyle:
    base = dict(fontName="Times-Roman", fontSize=9.5, leading=13.2, textColor=BODY,
                alignment=TA_LEFT, spaceAfter=0)
    base.update(kw)
    return ParagraphStyle(name, **base)


TITLE = S("t", fontName="Times-Bold", fontSize=19, leading=22, textColor=INK, spaceAfter=3)
SUB = S("s", fontSize=10.5, leading=14, textColor=MUTE, spaceAfter=10)
H2 = S("h2", fontName="Helvetica-Bold", fontSize=8, leading=11, textColor=NAVY,
       spaceBefore=13, spaceAfter=5)
P = S("p", spaceAfter=6)
SMALL = S("sm", fontSize=8, leading=11, textColor=MUTE)
QUOTE = S("q", fontName="Courier", fontSize=7.6, leading=10.4, textColor=INK,
          leftIndent=6, rightIndent=6, spaceBefore=3, spaceAfter=3)
CITE = S("c", fontName="Helvetica", fontSize=7, leading=9.5, textColor=NAVY, spaceAfter=2)
TD = S("td", fontSize=8, leading=10.6)
TDS = S("tds", fontName="Courier", fontSize=7.2, leading=9.6, textColor=INK)
TH = S("th", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=colors.white)


def esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def quote_block(text: str, cite: str) -> Table:
    """A verbatim quote with the exact URL it came from underneath. Line breaks in the
    source are preserved: a robots.txt is two lines and must read as two lines."""
    marked = esc(text).replace("\n", "<br/>")
    inner = [[Paragraph(marked, QUOTE)], [Paragraph(esc(cite), CITE)]]
    t = Table(inner, colWidths=[164 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), QUOTE_BG),
        ("LINEBEFORE", (0, 0), (0, 0), 1.6, NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (0, 0), 5), ("BOTTOMPADDING", (0, 0), (0, 0), 5),
        ("TOPPADDING", (0, 1), (0, 1), 2), ("BOTTOMPADDING", (0, 1), (0, 1), 0),
    ]))
    return t


def stop_block(title: str, quote: str, cite: str, effect: str) -> KeepTogether:
    head = Paragraph(f'<b>{esc(title)}</b>', S("sh", fontName="Times-Bold", fontSize=11,
                                               leading=14, textColor=ALARM, spaceAfter=4))
    body = [[head], [quote_block(quote, cite)],
            [Paragraph(esc(effect).replace("\n", "<br/>"), P)]]
    t = Table(body, colWidths=[170 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ALARM_BG),
        ("LINEBEFORE", (0, 0), (0, -1), 2.2, ALARM),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (0, 0), 8), ("BOTTOMPADDING", (0, -1), (0, -1), 8),
    ]))
    return KeepTogether([t, Spacer(1, 8)])


def header_footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(NAVY)
    canvas.setLineWidth(2)
    canvas.line(20 * mm, 283 * mm, 190 * mm, 283 * mm)
    canvas.setFont("Helvetica-Bold", 6.6)
    canvas.setFillColor(NAVY)
    canvas.drawString(20 * mm, 285.5 * mm, "TMT REGULATORY RADAR  ·  SCRAPING COMPLIANCE REVIEW")
    canvas.drawRightString(190 * mm, 285.5 * mm, "INTERNAL  ·  NOT LEGAL ADVICE")
    canvas.setFont("Helvetica", 6.6)
    canvas.setFillColor(MUTE)
    canvas.drawString(20 * mm, 12 * mm, "Observed 25 August 2026. Terms change; re-verify before relying on this.")
    canvas.drawRightString(190 * mm, 12 * mm, f"Page {doc.page}")
    canvas.restoreState()


def build() -> None:
    doc = BaseDocTemplate(str(OUT), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                          topMargin=22 * mm, bottomMargin=18 * mm,
                          title="TMT Radar - Scraping Compliance Review",
                          author="TMT Regulatory Radar")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f")
    doc.addPageTemplates([PageTemplate(id="all", frames=[frame], onPage=header_footer)])

    st: List = []
    st.append(Paragraph("Is our scraping lawful?", TITLE))
    st.append(Paragraph("Every host the tracker fetches, checked against its own robots.txt, "
                        "terms of use and copyright policy. Each finding below is quoted "
                        "verbatim with the exact page it came from.", SUB))

    # ---- summary strip
    strip = [[Paragraph("<b>17</b> hosts checked", TD), Paragraph("<b>2</b> stopped", TD),
              Paragraph("<b>5</b> paused for decision", TD), Paragraph("<b>12</b> still fetched", TD)]]
    t = Table(strip, colWidths=[42 * mm] * 4)
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, RULE),
                           ("INNERGRID", (0, 0), (-1, -1), 0.6, RULE),
                           ("BACKGROUND", (0, 0), (-1, -1), QUOTE_BG),
                           ("TOPPADDING", (0, 0), (-1, -1), 5),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                           ("LEFTPADDING", (0, 0), (-1, -1), 7)]))
    st.append(t)
    st.append(Spacer(1, 12))

    st.append(Paragraph("STOPPED: THE COLLECTION ITSELF IS OBJECTED TO", H2))
    st.append(stop_block(
        "1.  uidai.gov.in  —  excludes every crawler",
        "User-Agent: *\nDisallow: /",
        "That is the complete file.  https://uidai.gov.in/robots.txt  (HTTP 200, verified 25 Aug 2026; "
        "identical with no User-Agent sent, so it is not agent-specific)",
        "The strongest exclusion the protocol can express, with no exception and no Allow rule. Both paths "
        "we fetched fall inside it. UIDAI's copyright policy separately limits reuse to research or private "
        "study \"but not for sale or for use in conjunction with commercial purposes\" "
        "(https://uidai.gov.in/en/website-policies) - the only clause found that reaches a commercial firm "
        "directly. Collection stopped."))

    st.append(stop_block(
        "2.  eservices.dot.gov.in  —  names web scraping as unauthorised",
        "\"Unauthorised activities: The term Unauthorised activities includes any activity\n"
        "which is punishable under section 43 (for eg web scraping, altering source code,\n"
        "hacking, introducing viruses etc.) and Section 45 of the Information Technology\n"
        "Act, 2000 ...\"\n\n"
        "\"Telecom eServices Portal shall not in any manner be used for any unauthorized\n"
        "activity.\"\n\n"
        "\"The Services provided on the Telecom eServices Portal shall be accessed only\n"
        "through the interfaces expressly authorised by DoT ...\"",
        "https://eservices.dot.gov.in/terms-use  (HTTP 200, verified verbatim 25 Aug 2026)",
        "It names the exact activity on the exact host, unqualified by volume, harm or purpose, so our "
        "proportionality facts do not take us outside its words. The permissive copyright policy on the same "
        "site does not cure it: that clause licenses reuse, this one restricts the method of collection. "
        "This is the costly one - DoT circulars and the Act-and-Rules shelf were core telecom coverage. The "
        "same agreement contemplates \"activity authorised in writing by DoT\", so a letter seeking written "
        "authorisation is the route back."))

    # ---- paused
    st.append(Paragraph("PAUSED FOR A PARTNER DECISION", H2))
    st.append(Paragraph(
        "MeitY (4 sources) and PIB publish crawl policies that <b>permit</b> our paths, while their edge "
        "returns HTTP 403 to any user agent that identifies itself as a bot. Only an exact consumer-browser "
        "string is served. Reaching them therefore means presenting a browser string, which is the practice "
        "this review otherwise advises against. That is a question about the firm's posture, not a technical "
        "one, so both are paused rather than resolved silently.", P))
    st.append(quote_block(
        "User-agent: *\nAllow: /",
        "https://www.meity.gov.in/robots.txt  (HTTP 200) - expressly permits all crawling, while "
        "https://www.meity.gov.in/cms/wp-json/... returns 403 to an identified agent and 200 to a browser string"))
    st.append(Spacer(1, 4))
    st.append(Paragraph(
        "Coverage cost is smaller than it looks: gazetted MeitY instruments remain reachable through the "
        "e-Gazette lane, and MeitY's own gazette listing has not moved since 16 December 2025.", SMALL))

    st.append(PageBreak())

    # ---- reproduction table
    st.append(Paragraph("REPRODUCTION TERMS, QUOTED", H2))
    st.append(Paragraph(
        "None of the hosts below restricts automated access; every keyword scan for crawl, spider, robot, "
        "scrape, harvest, data mining and bulk download returned nothing. What they restrict is "
        "<b>reproduction</b>, which bites the optional PDF archive and not the dashboard, since a title, date "
        "and URL are bibliographic facts. Each is curable by one email.", P))
    st.append(Spacer(1, 3))

    rows = [[Paragraph("Host / source page", TH), Paragraph("What it says, verbatim", TH),
             Paragraph("Effect", TH)]]
    data = [
        ("trai.gov.in", "trai.gov.in/website-policy",
         "\"Material featured on this Portal may be reproduced free of charge after taking proper permission "
         "by sending a mail to us.\"",
         "Prior permission. Departs from the usual GoI wording, which omits the permission step."),
        ("www.cert-in.org.in", "cert-in.org.in/s2cMainServlet?pageid=COPYRIGHTPOCY",
         "\"Material featured on this Portal may be reproduced free of charge after taking proper permission "
         "by sending a mail to us.\"",
         "Prior permission."),
        ("www.mtcte.tec.gov.in", "mtcte.tec.gov.in/website_policy",
         "\"Material featured on this Portal may be reproduced free of charge after taking proper permission "
         "by sending a mail to TEC.\"",
         "Prior permission."),
        ("mib.gov.in", "mib.gov.in/en/privacy-policy",
         "\"Contents of this website may not be reproduced partially or fully, without due permission from "
         "Ministry of Information and Broadcasting.\"",
         "Prior permission."),
        ("cbfcindia.gov.in", "cbfcindia.gov.in/cbfcAdmin/copyright-policy.php",
         "\"Contents of this website may not be reproduced partially or fully, without due permission from "
         "Central Board of Film Certification.\"",
         "Prior permission."),
        ("www.tec.gov.in", "tec.gov.in/terms-conditions and /copyright-policy",
         "Permissive on one page: \"Material featured on this website may be reproduced free of charge.\" "
         "Restrictive on another. The two conflict.",
         "Ambiguous. Treat as restrictive until TEC clarifies."),
        ("pib.gov.in", "pib.gov.in/content/101_2_Terms-and-Conditions.aspx",
         "\"Material featured on this website may be reproduced free of charge and there is no need for any "
         "prior approval for using the content.\"",
         "Free reuse. Third-party material excluded."),
        ("www.meity.gov.in", "meity.gov.in/website-policy/copyright-policy",
         "\"Material featured on this site may be reproduced free of charge in any format or media without "
         "requiring specific permission.\"",
         "Free reuse, accurate and attributed."),
        ("www.inspace.gov.in", "inspace.gov.in/inspace?id=inspace_footer_det_page&sec=tou",
         "\"Material featured on this site belongs to the IN-SPACe/DoS and the same may be reproduced free "
         "of charge ...\"",
         "Free reuse with acknowledgement."),
        ("www.ascionline.in", "ascionline.in (footer)",
         "\"2026 (c) ASCI. All Rights Reserved\" - the only IP statement on the site.",
         "Private body. No licence, and no limb of s.52(1)(q) applies. Metadata only."),
        ("tdsat.gov.in", "tdsat.gov.in/Delhi/policies.php",
         "No copyright policy exists; the intended page is commented out of the source and its URL 404s.",
         "Silence. Not a grant, but not a prohibition."),
        ("egazette.gov.in", "egazette.gov.in (About Us)",
         "No copyright notice anywhere on the site.",
         "Silence. Gazette matter is within s.52(1)(q)(i) in any event."),
    ]
    for host, page, quote, effect in data:
        rows.append([
            Paragraph(f"<b>{esc(host)}</b><br/><font size=6.4 color='#00405C'>{esc(page)}</font>", TD),
            Paragraph(esc(quote), TDS),
            Paragraph(esc(effect), TD),
        ])
    tbl = Table(rows, colWidths=[41 * mm, 79 * mm, 50 * mm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, RULE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, QUOTE_BG]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    st.append(tbl)

    # ---- what changed
    st.append(Paragraph("WHAT IS NOW ENFORCED IN THE CODE", H2))
    for line in [
        "<b>robots.txt is a hard pre-flight gate.</b> Every request is checked against the origin's own "
        "robots.txt before it is made, evaluated under RFC 9309. A disallowed path is refused by the engine "
        "whatever the registry says.",
        "<b>The spoofed Chrome user agent is gone.</b> The tracker identifies itself as "
        "TMT-Regulatory-Radar/2.0 with a contact mailbox. Set TMT_RADAR_CONTACT to a monitored address "
        "before the next run.",
        "<b>Suspensions are shown, not hidden.</b> The stopped and paused sources appear on the coverage "
        "page with their reasons, so the tracker never overstates what it watches.",
        "<b>Rate and provenance.</b> About one request per second per host; the last run made 40 requests "
        "across 12 hosts, none to an undeclared host, all logged.",
    ]:
        st.append(Paragraph("&bull;&nbsp;&nbsp;" + line, S("b", leftIndent=8, spaceAfter=4)))

    # ---- next steps: heading and list stay together, never orphaned at a page foot
    steps: List = [Paragraph("NEXT STEPS", H2)]
    for i, line in enumerate([
        "Decide on MeitY and PIB: accept a browser user agent on the footing that their robots.txt expressly "
        "permits crawling and record that reasoning, or leave them paused.",
        "Write to DoT seeking the written authorisation its own user agreement contemplates. This restores "
        "the most valuable suspended source.",
        "Send the one-line permission emails to TRAI, CERT-In, TEC and MTCTE if the PDF archive is wanted "
        "for those hosts; keep archiving off for them until then.",
        "Keep the archive internal: no circulation to clients, no external distribution.",
        "Never load these pages in an iframe. TRAI, MTCTE, MeitY, CERT-In, UIDAI, IN-SPACe and PRGI all "
        "expressly prohibit framing. Link out.",
    ], 1):
        steps.append(Paragraph(f"<b>{i}.</b>&nbsp;&nbsp;{line}", S("n", leftIndent=8, spaceAfter=4)))
    st.append(KeepTogether(steps))

    # ---- limits
    st.append(Paragraph("LIMITS OF THIS REVIEW", H2))
    st.append(Paragraph(
        "This is a compliance review of publicly stated terms observed on 25 August 2026, not a legal "
        "opinion, and it has not been settled by counsel. Terms change without notice. One gap: MIB's "
        "separate Terms and Conditions tab could not be retrieved (its AJAX endpoints returned empty shells "
        "and an HTTP 500), so MIB's clean position on automated access is provisional. Documents were not "
        "classified individually against s.52(1)(q); in particular the TDSAT path fetched is an "
        "administrative notices listing rather than judgments, so limb (iv) does not automatically apply. "
        "Two questions are genuinely unsettled and should not be reported upward as clean: whether fetching "
        "a page served openly at HTTP 200 is \"without permission\" for s.43 of the IT Act, and whether "
        "browsewrap terms bind a party who never assented.", SMALL))

    doc.build(st)
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    build()
