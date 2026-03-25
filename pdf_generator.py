"""
pdf_generator.py — Standalone PDF generation for Raw Diet chatbot.
Two functions:
  - build_plan_pdf(plan_text, user_name, plan_title) → bytes
  - build_chat_pdf(history, user_name) → bytes
"""
import io, re, traceback, logging
from datetime import datetime

logger = logging.getLogger(__name__)

CLINIC_LINE1   = "Red Apple Wellness Diet Center"
CLINIC_LINE2   = "RAW-DIET  |  SINCE 2008"
CLINIC_LINE3   = "Dr. Meghana Kumare — Dietician & Sports Nutritionist | 20+ Years"
CLINIC_CONTACT = "+91 7774944783  |  rawdiets12@gmail.com  |  raw-diet.com"
CLINIC_ADDRESS = "Fortune Crest, Opp. Khare Town Post Office, Dharampeth, Nagpur – 440010"

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
USER_BG   = "#E8F4FD"


def _esc(t):
    return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def _parse_md(text):
    """Parse markdown into (type, text) tuples."""
    text = text.replace("[PDF_REQUESTED]", "").strip()
    result = []
    for raw in text.split("\n"):
        s = raw.strip()
        if not s:
            result.append(("empty", ""))
        elif s.startswith("### "):
            result.append(("h3", s[4:].strip()))
        elif s.startswith("## "):
            result.append(("h2", s[3:].strip()))
        elif s.startswith("# "):
            result.append(("h1", s[2:].strip()))
        elif re.match(r"^\*\*[^*]+\*\*:?$", s):
            result.append(("h3", re.sub(r"\*\*","",s).strip().rstrip(":")))
        elif re.match(r"^\d+[\.\)]\s", s):
            c = re.sub(r"^\d+[\.\)]\s+","",s)
            c = re.sub(r"\*\*(.+?)\*\*",r"\1",c)
            result.append(("numbered", s[:2] + " " + c))
        elif s.startswith(("- ","* ","• ")):
            c = re.sub(r"\*\*(.+?)\*\*",r"\1", s[2:].strip())
            result.append(("bullet", c))
        else:
            c = re.sub(r"\*\*(.+?)\*\*",r"\1",s)
            c = re.sub(r"\*(.+?)\*",r"\1",c)
            result.append(("body", c))
    return result


def _C():
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
        "f2":       colors.HexColor("#F2F3F4"),
    }


def _ST(C):
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY, TA_RIGHT
    def S(n, **k): return ParagraphStyle(n, **k)
    return {
        "clinic_title": S("ct", fontName="Helvetica-Bold", fontSize=19,
                          textColor=C["red_dark"], alignment=TA_CENTER, leading=24),
        "clinic_sub":   S("cs", fontName="Helvetica", fontSize=8.5,
                          textColor=C["mid"], alignment=TA_CENTER, leading=13),
        "meta_label":   S("ml", fontName="Helvetica-Bold", fontSize=8.5,
                          textColor=C["green"], leading=13),
        "meta_value":   S("mv", fontName="Helvetica", fontSize=8.5,
                          textColor=C["dark"], leading=13),
        # Plan styles
        "h1_txt": S("h1t", fontName="Helvetica-Bold", fontSize=12,
                    textColor=C["white"], leading=16),
        "h2_txt": S("h2t", fontName="Helvetica-Bold", fontSize=10.5,
                    textColor=C["green"], leading=14),
        "h3_txt": S("h3t", fontName="Helvetica-Bold", fontSize=9.5,
                    textColor=C["dark"], leading=13, spaceBefore=5, spaceAfter=2),
        "body":   S("bo", fontName="Helvetica", fontSize=9,
                    textColor=C["dark"], leading=14, spaceAfter=3, alignment=TA_JUSTIFY),
        "bul_dot":S("bdo", fontName="Helvetica-Bold", fontSize=12,
                    textColor=C["red"], leading=14),
        "bul_txt":S("btx", fontName="Helvetica", fontSize=9,
                    textColor=C["dark"], leading=14),
        "num_txt":S("nt", fontName="Helvetica", fontSize=9,
                    textColor=C["dark"], leading=14, spaceAfter=2, leftIndent=14),
        # Chat styles
        "you_label": S("yl", fontName="Helvetica-Bold", fontSize=7,
                       textColor=C["green"], leading=9),
        "you_txt":   S("yt", fontName="Helvetica-Bold", fontSize=9,
                       textColor=C["dark"], leading=13),
        "bot_label": S("bl", fontName="Helvetica-Bold", fontSize=7,
                       textColor=C["red"], leading=9),
        "bot_h2":    S("bh2", fontName="Helvetica-Bold", fontSize=9.5,
                       textColor=C["green"], leading=13, spaceBefore=5, spaceAfter=2),
        "bot_h3":    S("bh3", fontName="Helvetica-Bold", fontSize=9,
                       textColor=C["dark"], leading=12, spaceBefore=3, spaceAfter=1),
        "bot_body":  S("bbo", fontName="Helvetica", fontSize=9,
                       textColor=C["dark"], leading=13, spaceAfter=2),
        "bot_bul_dot": S("bbd", fontName="Helvetica-Bold", fontSize=11,
                         textColor=C["red"], leading=13),
        "bot_bul_txt": S("bbt", fontName="Helvetica", fontSize=9,
                         textColor=C["dark"], leading=13),
        "bot_num":   S("bnum", fontName="Helvetica", fontSize=9,
                       textColor=C["dark"], leading=13, spaceAfter=1, leftIndent=12),
        "ts_txt":    S("ts", fontName="Helvetica-Oblique", fontSize=7,
                       textColor=C["mid"], alignment=TA_RIGHT, leading=9),
        # Footer
        "footer":     S("ft", fontName="Helvetica", fontSize=7.5,
                        textColor=C["mid"], alignment=TA_CENTER, leading=11),
        "disclaimer": S("dc", fontName="Helvetica-Oblique", fontSize=8,
                        textColor=C["red_dark"], alignment=TA_CENTER, leading=12),
    }


def _watermark(canv, doc, page_w, page_h, is_chat=False):
    from reportlab.lib import colors
    # Diagonal watermark
    canv.saveState()
    canv.setFont("Helvetica-Bold", 46)
    canv.setFillColor(colors.HexColor(RED), alpha=0.04)
    canv.translate(page_w/2, page_h/2)
    canv.rotate(42)
    canv.drawCentredString(0,  52, "Red Apple Wellness")
    canv.drawCentredString(0, -52, "raw-diet.com")
    canv.restoreState()
    # Repeat header strip on pages 2+
    if doc.page > 1:
        canv.saveState()
        canv.setFillColor(colors.HexColor(RED_BG))
        canv.rect(0, page_h-26, page_w, 26, fill=1, stroke=0)
        canv.setFont("Helvetica-Bold", 7.5)
        canv.setFillColor(colors.HexColor(RED))
        label = ("Chat History  |  Red Apple Wellness Diet Center  |  raw-diet.com"
                 if is_chat else
                 "Red Apple Wellness Diet Center  |  Dr. Meghana Kumare  |  +91 7774944783")
        canv.drawCentredString(page_w/2, page_h-16, label)
        canv.restoreState()
    # Page number
    canv.saveState()
    canv.setFont("Helvetica", 7.5)
    canv.setFillColor(colors.HexColor(MID))
    canv.drawCentredString(page_w/2, 14, f"Page {doc.page}")
    canv.restoreState()


def _banner(cw, C, ST, subtitle=""):
    from reportlab.platypus import Table, TableStyle, Paragraph
    rows = [
        [Paragraph(_esc(CLINIC_LINE1), ST["clinic_title"])],
        [Paragraph(_esc(CLINIC_LINE2), ST["clinic_sub"])],
        [Paragraph(_esc(CLINIC_LINE3), ST["clinic_sub"])],
    ]
    if subtitle:
        rows.append([Paragraph(f"<b>{_esc(subtitle)}</b>", ST["clinic_sub"])])
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
    rows = [[Paragraph(_esc(l), ST["meta_label"]),
             Paragraph(_esc(v), ST["meta_value"])]
            for l, v in rows_data]
    from reportlab.lib.units import cm
    c1 = 3.0*cm
    t  = Table(rows, colWidths=[c1, cw-c1])
    t.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0,0),(-1,-1), [C["f2"], C["white"]]),
        ("TOPPADDING",    (0,0),(-1,-1), 5),
        ("BOTTOMPADDING", (0,0),(-1,-1), 5),
        ("LEFTPADDING",   (0,0),(-1,-1), 8),
        ("RIGHTPADDING",  (0,0),(-1,-1), 8),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("BOX",           (0,0),(-1,-1), 0.5, C["lite"]),
        ("LINEBELOW",     (0,0),(-1,-1), 0.3, C["lite"]),
    ]))
    return t


def _content_block(story, parsed, cw, C, ST,
                   h2_style="h2_txt", h3_style="h3_txt",
                   body_style="body",
                   bul_dot_style="bul_dot", bul_txt_style="bul_txt",
                   num_style="num_txt"):
    """Render parsed markdown lines into story elements."""
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.units import cm

    empty_count = 0
    for ptype, ptext in parsed:
        if ptype == "empty":
            empty_count += 1
            if empty_count == 1:
                story.append(Spacer(1, 4))
            continue
        empty_count = 0

        if ptype == "h1":
            t = Table([[Paragraph(_esc(ptext), ST["h1_txt"])]], colWidths=[cw])
            t.setStyle(TableStyle([
                ("BACKGROUND",    (0,0),(-1,-1), C["red"]),
                ("TOPPADDING",    (0,0),(-1,-1), 8),
                ("BOTTOMPADDING", (0,0),(-1,-1), 8),
                ("LEFTPADDING",   (0,0),(-1,-1), 12),
                ("RIGHTPADDING",  (0,0),(-1,-1), 12),
            ]))
            story.append(Spacer(1, 6))
            story.append(t)
            story.append(Spacer(1, 4))

        elif ptype == "h2":
            t = Table([[Paragraph(_esc(ptext), ST[h2_style])]], colWidths=[cw])
            t.setStyle(TableStyle([
                ("BACKGROUND",  (0,0),(-1,-1), C["green_bg"]),
                ("TOPPADDING",  (0,0),(-1,-1), 5),
                ("BOTTOMPADDING",(0,0),(-1,-1), 5),
                ("LEFTPADDING", (0,0),(-1,-1), 10),
                ("RIGHTPADDING",(0,0),(-1,-1), 10),
                ("LINEBEFORE",  (0,0),(0,-1), 3.5, C["green"]),
            ]))
            story.append(Spacer(1, 4))
            story.append(t)
            story.append(Spacer(1, 3))

        elif ptype == "h3":
            story.append(Paragraph(_esc(ptext), ST[h3_style]))

        elif ptype == "bullet":
            dot_w = 16
            t = Table([[
                Paragraph("&#8226;", ST[bul_dot_style]),
                Paragraph(_esc(ptext), ST[bul_txt_style]),
            ]], colWidths=[dot_w, cw - dot_w])
            t.setStyle(TableStyle([
                ("VALIGN",        (0,0),(-1,-1), "TOP"),
                ("TOPPADDING",    (0,0),(-1,-1), 3),
                ("BOTTOMPADDING", (0,0),(-1,-1), 3),
                ("LEFTPADDING",   (0,0),(0,-1), 4),
                ("RIGHTPADDING",  (0,0),(0,-1), 0),
                ("LEFTPADDING",   (1,0),(1,-1), 4),
                ("RIGHTPADDING",  (1,0),(1,-1), 4),
                ("BACKGROUND",    (0,0),(-1,-1), C["bullet_bg"]),
                ("LINEBELOW",     (0,0),(-1,-1), 0.25, C["lite"]),
            ]))
            story.append(t)
            story.append(Spacer(1, 1))

        elif ptype == "numbered":
            story.append(Paragraph(_esc(ptext), ST[num_style]))

        else:
            story.append(Paragraph(_esc(ptext), ST[body_style]))


def _footer(story, cw, C, ST, today):
    from reportlab.platypus import Spacer, Table, TableStyle, Paragraph, HRFlowable
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=1.5, color=C["red"], spaceAfter=8))
    disc = Table([[Paragraph(
        "&#9888;&nbsp; This plan is for general guidance only. For a medically safe, "
        "personalised plan based on your health conditions &amp; medications, "
        "please consult <b>Dr. Meghana Kumare</b> directly.",
        ST["disclaimer"])]], colWidths=[cw])
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


# ── PUBLIC FUNCTIONS ───────────────────────────────────────────────────────────

def build_plan_pdf(plan_text: str, user_name: str = "User",
                   plan_title: str = "Diet Plan") -> bytes:
    """Generate a branded, well-formatted diet plan PDF."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Spacer, HRFlowable

    page_w, page_h = A4
    L = R = 2.0*cm; T = 2.5*cm; B = 2.5*cm
    cw = page_w - L - R
    today = datetime.now().strftime("%d %B %Y")
    buf = io.BytesIO()
    C  = _C()
    ST = _ST(C)

    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=L, rightMargin=R, topMargin=T, bottomMargin=B,
        title=f"{plan_title} — {user_name}", author=CLINIC_LINE1)

    def draw(canv, doc):
        _watermark(canv, doc, page_w, page_h, is_chat=False)

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

    _content_block(story, _parse_md(plan_text), cw, C, ST)
    _footer(story, cw, C, ST, today)
    doc.build(story, onFirstPage=draw, onLaterPages=draw)
    return buf.getvalue()


def build_chat_pdf(history: list, user_name: str = "User") -> bytes:
    """Generate a branded chat history PDF with full formatted responses."""
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
    C  = _C()
    ST = _ST(C)

    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=L, rightMargin=R, topMargin=T, bottomMargin=B,
        title=f"Chat History — {user_name}", author=CLINIC_LINE1)

    def draw(canv, doc):
        _watermark(canv, doc, page_w, page_h, is_chat=True)

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
    story.append(Spacer(1, 12))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C["lite"], spaceAfter=10))

    for msg in history:
        q  = str(msg.get("question","")).strip()
        a  = str(msg.get("answer",  "")).strip()
        ts = str(msg.get("created_at",""))
        if "T" in ts: ts = ts.replace("T"," ")[:16]

        if not q or (q.startswith("[") and q.endswith("]")):
            continue

        # ── USER BUBBLE ───────────────────────────────────────────────────
        q_safe  = _esc(q[:500] + ("…" if len(q) > 500 else ""))
        ts_safe = _esc(ts)

        user_tbl = Table([
            [Paragraph("YOU", ST["you_label"]),
             Paragraph(ts_safe, ST["ts_txt"])],
            [Paragraph(q_safe, ST["you_txt"]),
             Paragraph("", ST["you_txt"])],
        ], colWidths=[cw*0.82, cw*0.18])
        user_tbl.setStyle(TableStyle([
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
        story.append(user_tbl)
        story.append(Spacer(1, 3))

        # ── BOT BUBBLE — parse and render markdown properly ───────────────
        # Header row
        bot_header = Table([
            [Paragraph("DR. MEGHANA'S ASSISTANT", ST["bot_label"]),
             Paragraph("", ST["bot_label"])],
        ], colWidths=[cw*0.82, cw*0.18])
        bot_header.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,-1), C["white"]),
            ("TOPPADDING",    (0,0),(-1,-1), 6),
            ("BOTTOMPADDING", (0,0),(-1,-1), 3),
            ("LEFTPADDING",   (0,0),(-1,-1), 10),
            ("RIGHTPADDING",  (0,0),(-1,-1), 8),
            ("LINEBEFORE",    (0,0),(0,-1), 3, C["red"]),
            ("LINEABOVE",     (0,0),(-1,0), 0.4, C["lite"]),
        ]))
        story.append(bot_header)

        # Bot content — fully parsed markdown inside a framed box
        # We render into a sub-story, then wrap in a bordered table
        bot_inner = []
        inner_w = cw - 26  # account for left border + padding

        parsed_a = _parse_md(a)
        _content_block(
            bot_inner, parsed_a, inner_w, C, ST,
            h2_style="bot_h2", h3_style="bot_h3",
            body_style="bot_body",
            bul_dot_style="bot_bul_dot", bul_txt_style="bot_bul_txt",
            num_style="bot_num",
        )

        if not bot_inner:
            bot_inner.append(Paragraph(_esc(a[:600]), ST["bot_body"]))

        # Wrap bot content in a table cell for the left border + background
        from reportlab.platypus import KeepTogether
        bot_content_tbl = Table(
            [[bot_inner]],
            colWidths=[cw]
        )
        bot_content_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,-1), C["white"]),
            ("TOPPADDING",    (0,0),(-1,-1), 4),
            ("BOTTOMPADDING", (0,0),(-1,-1), 8),
            ("LEFTPADDING",   (0,0),(-1,-1), 10),
            ("RIGHTPADDING",  (0,0),(-1,-1), 8),
            ("LINEBEFORE",    (0,0),(0,-1), 3, C["red"]),
            ("LINEBELOW",     (0,0),(-1,-1), 0.4, C["lite"]),
            ("LINEAFTER",     (0,0),(-1,-1), 0.4, C["lite"]),
        ]))
        story.append(bot_content_tbl)
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
