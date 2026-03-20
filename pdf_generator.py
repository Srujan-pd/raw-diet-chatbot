"""
pdf_generator.py — Standalone PDF generation for Raw Diet chatbot.
Two functions:
  - build_plan_pdf(plan_text, user_name, plan_title) → bytes
  - build_chat_pdf(history, user_name) → bytes
"""
import io, re, traceback, logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Colors & constants ─────────────────────────────────────────────────────────
RED       = "#B03A2E"
RED_DARK  = "#7B241C"
RED_BG    = "#FDECEA"
RED_LITE  = "#F1948A"
GREEN     = "#1A5276"
GREEN_BG  = "#EBF5FB"
DARK      = "#1C2833"
MID       = "#566573"
LITE      = "#D5D8DC"
BULLET_BG = "#F4F6F7"
USER_BG   = "#EBF5FB"
WHITE     = "#FFFFFF"

CLINIC_LINE1 = "Red Apple Wellness Diet Center"
CLINIC_LINE2 = "RAW-DIET  |  SINCE 2008"
CLINIC_LINE3 = "Dr. Meghana Kumare — Dietician & Sports Nutritionist | 20+ Years"
CLINIC_CONTACT = "+91 7774944783  |  rawdiets12@gmail.com  |  raw-diet.com"
CLINIC_ADDRESS = "Fortune Crest, Opp. Khare Town Post Office, Dharampeth, Nagpur – 440010"


def _esc(t):
    return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def _clean(text):
    """Strip markdown for display."""
    text = text.replace("[PDF_REQUESTED]", "")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*",     r"\1", text)
    text = re.sub(r"#{1,6}\s+",     "",    text)
    return text.strip()


def _parse_md(text):
    """Parse markdown into (type, text) list."""
    text = text.replace("[PDF_REQUESTED]","").strip()
    result = []
    for raw in text.split("\n"):
        s = raw.strip()
        if not s:
            result.append(("empty",""))
        elif s.startswith("### "):
            result.append(("h3", s[4:].strip()))
        elif s.startswith("## "):
            result.append(("h2", s[3:].strip()))
        elif s.startswith("# "):
            result.append(("h1", s[2:].strip()))
        elif re.match(r"^\*\*[^*]+\*\*$", s):
            result.append(("h3", re.sub(r"\*\*","",s).strip()))
        elif re.match(r"^\d+[\.\)]\s", s):
            c = re.sub(r"\*\*(.+?)\*\*",r"\1", re.sub(r"^\d+[\.\)]\s+","",s))
            result.append(("numbered", s[:2] + c))
        elif s.startswith(("- ","* ","• ")):
            c = re.sub(r"\*\*(.+?)\*\*",r"\1", s[2:].strip())
            result.append(("bullet", c))
        else:
            c = re.sub(r"\*\*(.+?)\*\*",r"\1",s)
            c = re.sub(r"\*(.+?)\*",r"\1",c)
            result.append(("body", c))
    return result


def _colors():
    from reportlab.lib import colors
    return {
        "red":      colors.HexColor(RED),
        "red_dark": colors.HexColor(RED_DARK),
        "red_bg":   colors.HexColor(RED_BG),
        "red_lite": colors.HexColor(RED_LITE),
        "green":    colors.HexColor(GREEN),
        "green_bg": colors.HexColor(GREEN_BG),
        "dark":     colors.HexColor(DARK),
        "mid":      colors.HexColor(MID),
        "lite":     colors.HexColor(LITE),
        "bullet_bg":colors.HexColor(BULLET_BG),
        "user_bg":  colors.HexColor(USER_BG),
        "white":    colors.white,
    }


def _styles(C):
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
    def S(n, **k):
        return ParagraphStyle(n, **k)
    return {
        "clinic_title": S("ct", fontName="Helvetica-Bold", fontSize=19,
                          textColor=C["red_dark"], alignment=TA_CENTER, leading=24),
        "clinic_sub":   S("cs", fontName="Helvetica", fontSize=8.5,
                          textColor=C["mid"], alignment=TA_CENTER, leading=13),
        "meta_label":   S("ml", fontName="Helvetica-Bold", fontSize=8.5,
                          textColor=C["green"], leading=13),
        "meta_value":   S("mv", fontName="Helvetica", fontSize=8.5,
                          textColor=C["dark"], leading=13),
        "h1":  S("h1", fontName="Helvetica-Bold", fontSize=12,
                  textColor=C["white"], leading=16),
        "h2":  S("h2", fontName="Helvetica-Bold", fontSize=10.5,
                  textColor=C["green"], leading=14),
        "h3":  S("h3", fontName="Helvetica-Bold", fontSize=9.5,
                  textColor=C["dark"], leading=13, spaceBefore=5, spaceAfter=2),
        "body": S("body", fontName="Helvetica", fontSize=9,
                   textColor=C["dark"], leading=14, spaceAfter=3,
                   alignment=TA_JUSTIFY),
        "bullet_dot": S("bd", fontName="Helvetica-Bold", fontSize=12,
                         textColor=C["red"], leading=14),
        "bullet_val": S("bv", fontName="Helvetica", fontSize=9,
                         textColor=C["dark"], leading=14),
        "numbered":   S("num", fontName="Helvetica", fontSize=9,
                         textColor=C["dark"], leading=14, spaceAfter=2, leftIndent=14),
        "footer":     S("ft", fontName="Helvetica", fontSize=7.5,
                         textColor=C["mid"], alignment=TA_CENTER, leading=11),
        "disclaimer": S("dc", fontName="Helvetica-Oblique", fontSize=8,
                         textColor=C["red_dark"], alignment=TA_CENTER, leading=12),
        "q_label":    S("ql", fontName="Helvetica-Bold", fontSize=7.5,
                         textColor=C["green"], leading=10),
        "q_text":     S("qt", fontName="Helvetica-Bold", fontSize=9,
                         textColor=C["dark"], leading=14),
        "a_label":    S("al", fontName="Helvetica-Bold", fontSize=7.5,
                         textColor=C["red"], leading=10),
        "a_text":     S("at", fontName="Helvetica", fontSize=9,
                         textColor=C["dark"], leading=14),
        "ts_text":    S("ts", fontName="Helvetica-Oblique", fontSize=7,
                         textColor=C["mid"], alignment=TA_LEFT, leading=10),
    }


def _watermark_and_header(canv, doc, page_w, page_h, C, is_chat=False):
    from reportlab.lib import colors
    # Watermark
    canv.saveState()
    canv.setFont("Helvetica-Bold", 46)
    canv.setFillColor(colors.HexColor(RED), alpha=0.04)
    canv.translate(page_w/2, page_h/2)
    canv.rotate(42)
    canv.drawCentredString(0,  52, "Red Apple Wellness")
    canv.drawCentredString(0, -52, "raw-diet.com")
    canv.restoreState()
    # Page 2+ header strip
    if doc.page > 1:
        canv.saveState()
        canv.setFillColor(colors.HexColor(RED_BG))
        canv.rect(0, page_h-26, page_w, 26, fill=1, stroke=0)
        canv.setFont("Helvetica-Bold", 7.5)
        canv.setFillColor(colors.HexColor(RED))
        label = "Chat History  |  Red Apple Wellness Diet Center" if is_chat else \
                "Red Apple Wellness Diet Center  |  Dr. Meghana Kumare  |  +91 7774944783"
        canv.drawCentredString(page_w/2, page_h-16, label)
        canv.restoreState()
    # Page number
    canv.saveState()
    canv.setFont("Helvetica", 7.5)
    canv.setFillColor(colors.HexColor(MID))
    canv.drawCentredString(page_w/2, 14, f"Page {doc.page}")
    canv.restoreState()


def _banner(cw, C, ST, subtitle=""):
    from reportlab.platypus import Table, TableStyle, Paragraph, Spacer
    rows = [
        [Paragraph(_esc(CLINIC_LINE1), ST["clinic_title"])],
        [Paragraph(_esc(CLINIC_LINE2), ST["clinic_sub"])],
        [Paragraph(_esc(CLINIC_LINE3), ST["clinic_sub"])],
    ]
    if subtitle:
        rows.append([Paragraph(_esc(subtitle), ST["clinic_sub"])])
    t = Table(rows, colWidths=[cw])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), C["red_bg"]),
        ("TOPPADDING",    (0,0),(-1,-1), 7),
        ("BOTTOMPADDING", (0,-1),(-1,-1), 10),
        ("LEFTPADDING",   (0,0),(-1,-1), 14),
        ("RIGHTPADDING",  (0,0),(-1,-1), 14),
        ("LINEBELOW",     (0,-1),(-1,-1), 2.5, C["red"]),
    ]))
    return t


def _meta_table(rows_data, cw, C, ST):
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    rows = []
    for label, value in rows_data:
        rows.append([
            Paragraph(_esc(label), ST["meta_label"]),
            Paragraph(_esc(value), ST["meta_value"]),
        ])
    t = Table(rows, colWidths=[3.2*72/2.54, cw - 3.2*72/2.54])
    t.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0,0),(-1,-1), [colors.HexColor("#F2F3F4"), C["white"]]),
        ("TOPPADDING",    (0,0),(-1,-1), 5),
        ("BOTTOMPADDING", (0,0),(-1,-1), 5),
        ("LEFTPADDING",   (0,0),(-1,-1), 8),
        ("RIGHTPADDING",  (0,0),(-1,-1), 8),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("BOX",           (0,0),(-1,-1), 0.5, C["lite"]),
        ("LINEBELOW",     (0,0),(-1,-1), 0.3, C["lite"]),
    ]))
    return t


def _footer_block(story, cw, C, ST, today):
    from reportlab.platypus import Spacer, Table, TableStyle, Paragraph, HRFlowable
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=1.5, color=C["red"], spaceAfter=8))
    disc = Table([[Paragraph(
        "&#9888;&nbsp; This plan is for general guidance only. For a medically safe, "
        "personalised plan based on your health conditions &amp; medications, "
        "please consult <b>Dr. Meghana Kumare</b> directly.",
        ST["disclaimer"])]],
        colWidths=[cw])
    disc.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), C["red_bg"]),
        ("TOPPADDING",    (0,0),(-1,-1), 9),
        ("BOTTOMPADDING", (0,0),(-1,-1), 9),
        ("LEFTPADDING",   (0,0),(-1,-1), 12),
        ("RIGHTPADDING",  (0,0),(-1,-1), 12),
        ("BOX",           (0,0),(-1,-1), 0.5, C["red_lite"]),
    ]))
    story.append(disc)
    story.append(Spacer(1, 5))
    story.append(Paragraph(_esc(CLINIC_ADDRESS), ST["footer"]))
    story.append(Paragraph(_esc(CLINIC_CONTACT), ST["footer"]))
    story.append(Paragraph(
        f"Generated on {today} by Raw Diet AI &mdash; Not a substitute for professional medical advice.",
        ST["footer"]))


def build_plan_pdf(plan_text: str, user_name: str = "User",
                   plan_title: str = "Diet Plan") -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                     Table, TableStyle, HRFlowable)
    page_w, page_h = A4
    L = R = 2.0*cm; T = 2.5*cm; B = 2.5*cm
    cw = page_w - L - R
    today = datetime.now().strftime("%d %B %Y")
    buf = io.BytesIO()
    C  = _colors()
    ST = _styles(C)

    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=L, rightMargin=R, topMargin=T, bottomMargin=B,
        title=f"{plan_title} — {user_name}",
        author=CLINIC_LINE1)

    def draw(canv, doc):
        _watermark_and_header(canv, doc, page_w, page_h, C, is_chat=False)

    story = []
    story.append(_banner(cw, C, ST))
    story.append(Spacer(1, 10))
    story.append(_meta_table([
        ("Prepared for", user_name),
        ("Plan",         plan_title),
        ("Date",         today),
        ("Center",       "Red Apple Wellness Diet Center, Nagpur"),
        ("Contact",      CLINIC_CONTACT),
    ], cw, C, ST))
    story.append(Spacer(1, 12))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C["lite"], spaceAfter=10))

    # Content
    for ptype, ptext in _parse_md(plan_text):
        if ptype == "empty":
            story.append(Spacer(1, 4)); continue

        if ptype == "h1":
            t = Table([[Paragraph(_esc(ptext), ST["h1"])]], colWidths=[cw])
            t.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(-1,-1),C["red"]),
                ("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8),
                ("LEFTPADDING",(0,0),(-1,-1),12),("RIGHTPADDING",(0,0),(-1,-1),12),
            ]))
            story.append(Spacer(1,6)); story.append(t); story.append(Spacer(1,4))

        elif ptype == "h2":
            t = Table([[Paragraph(_esc(ptext), ST["h2"])]], colWidths=[cw])
            t.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(-1,-1),C["green_bg"]),
                ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
                ("LEFTPADDING",(0,0),(-1,-1),10),("RIGHTPADDING",(0,0),(-1,-1),10),
                ("LINEBEFORE",(0,0),(0,-1),3.5,C["green"]),
            ]))
            story.append(Spacer(1,4)); story.append(t); story.append(Spacer(1,2))

        elif ptype == "h3":
            story.append(Paragraph(_esc(ptext), ST["h3"]))

        elif ptype == "bullet":
            t = Table([[
                Paragraph("&#8226;", ST["bullet_dot"]),
                Paragraph(_esc(ptext), ST["bullet_val"]),
            ]], colWidths=[20, cw-20])
            t.setStyle(TableStyle([
                ("VALIGN",(0,0),(-1,-1),"TOP"),
                ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
                ("LEFTPADDING",(0,0),(0,-1),4),("LEFTPADDING",(1,0),(1,-1),4),
                ("RIGHTPADDING",(0,0),(-1,-1),6),
                ("BACKGROUND",(0,0),(-1,-1),C["bullet_bg"]),
                ("LINEBELOW",(0,0),(-1,-1),0.25,C["lite"]),
            ]))
            story.append(t); story.append(Spacer(1,1))

        elif ptype == "numbered":
            story.append(Paragraph(_esc(ptext), ST["numbered"]))
        else:
            story.append(Paragraph(_esc(ptext), ST["body"]))

    _footer_block(story, cw, C, ST, today)
    doc.build(story, onFirstPage=draw, onLaterPages=draw)
    return buf.getvalue()


def build_chat_pdf(history: list, user_name: str = "User") -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                     Table, TableStyle, HRFlowable)
    from reportlab.lib import colors

    page_w, page_h = A4
    L = R = 2.0*cm; T = 2.5*cm; B = 2.5*cm
    cw = page_w - L - R
    today = datetime.now().strftime("%d %B %Y %H:%M")
    buf = io.BytesIO()
    C  = _colors()
    ST = _styles(C)

    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=L, rightMargin=R, topMargin=T, bottomMargin=B,
        title=f"Chat History — {user_name}",
        author=CLINIC_LINE1)

    def draw(canv, doc):
        _watermark_and_header(canv, doc, page_w, page_h, C, is_chat=True)

    story = []
    story.append(_banner(cw, C, ST, subtitle="Chat History"))
    story.append(Spacer(1, 10))
    story.append(_meta_table([
        ("User",      user_name),
        ("Messages",  str(len(history))),
        ("Generated", today),
        ("Center",    "Red Apple Wellness Diet Center, Nagpur"),
        ("Contact",   CLINIC_CONTACT),
    ], cw, C, ST))
    story.append(Spacer(1, 14))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C["lite"], spaceAfter=10))

    # Chat messages
    for i, msg in enumerate(history, 1):
        q  = str(msg.get("question", "")).strip()
        a  = str(msg.get("answer",   "")).strip()
        ts = str(msg.get("created_at", ""))
        if "T" in ts: ts = ts.replace("T"," ")[:16]

        if not q or (q.startswith("[") and q.endswith("]")):
            continue

        # ── USER BUBBLE ────────────────────────────────────────────────────
        q_safe = _esc(q[:400] + ("…" if len(q) > 400 else ""))
        ts_safe = _esc(ts)

        user_inner = Table([
            [Paragraph("YOU", ST["q_label"]),
             Paragraph(ts_safe, ST["ts_text"])],
            [Paragraph(q_safe, ST["q_text"]),
             Paragraph("", ST["q_text"])],
        ], colWidths=[cw*0.85, cw*0.15])
        user_inner.setStyle(TableStyle([
            ("SPAN",          (0,1),(1,1)),
            ("BACKGROUND",    (0,0),(-1,-1), C["user_bg"]),
            ("TOPPADDING",    (0,0),(-1,-1), 5),
            ("BOTTOMPADDING", (0,0),(-1,-1), 5),
            ("LEFTPADDING",   (0,0),(-1,-1), 10),
            ("RIGHTPADDING",  (0,0),(-1,-1), 8),
            ("LINEBEFORE",    (0,0),(0,-1), 3, C["green"]),
            ("ALIGN",         (1,0),(1,0), "RIGHT"),
            ("VALIGN",        (0,0),(-1,-1), "TOP"),
        ]))
        story.append(user_inner)
        story.append(Spacer(1, 3))

        # ── BOT BUBBLE ─────────────────────────────────────────────────────
        # Clean the answer text
        a_clean = _clean(a)
        # Truncate very long answers for display
        if len(a_clean) > 1500:
            a_clean = a_clean[:1500] + "\n…[continued — see full chat]"
        a_safe = _esc(a_clean)

        bot_inner = Table([
            [Paragraph("DR. MEGHANA'S ASSISTANT", ST["a_label"]),
             Paragraph("", ST["a_label"])],
            [Paragraph(a_safe, ST["a_text"]),
             Paragraph("", ST["a_text"])],
        ], colWidths=[cw*0.85, cw*0.15])
        bot_inner.setStyle(TableStyle([
            ("SPAN",          (0,1),(1,1)),
            ("BACKGROUND",    (0,0),(-1,-1), colors.white),
            ("TOPPADDING",    (0,0),(-1,-1), 5),
            ("BOTTOMPADDING", (0,0),(-1,-1), 5),
            ("LEFTPADDING",   (0,0),(-1,-1), 10),
            ("RIGHTPADDING",  (0,0),(-1,-1), 8),
            ("LINEBEFORE",    (0,0),(0,-1), 3, C["red"]),
            ("BOX",           (0,0),(-1,-1), 0.4, C["lite"]),
            ("VALIGN",        (0,0),(-1,-1), "TOP"),
        ]))
        story.append(bot_inner)
        story.append(Spacer(1, 10))

    # Footer
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1.5, color=C["red"], spaceAfter=6))
    story.append(Paragraph(_esc(CLINIC_ADDRESS), ST["footer"]))
    story.append(Paragraph(_esc(CLINIC_CONTACT), ST["footer"]))
    story.append(Paragraph(
        f"Chat history generated on {today} — Red Apple Wellness Diet Center",
        ST["footer"]))

    doc.build(story, onFirstPage=draw, onLaterPages=draw)
    return buf.getvalue()
