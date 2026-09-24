"""
Layout for the tailored CV: one document dict in, a PDF and a plain-text
rendering out. No LLM, no network, no I/O beyond writing the file.

Why this is a separate module from cv_rewriter_agent
----------------------------------------------------
The old pipeline asked the model to emit a CV as laid-out plain text and then
reverse-engineered the layout back out of it with regex heuristics -- first
line is the name, second is contact, ALL-CAPS lines are headers, "- " lines
are bullets. That does the work twice and loses information both directions,
and it caps the layout at whatever plain text can express. Three things the
blueprint asks for cannot survive that round trip at all:

  * dates and location right-aligned on the same line as the job title
    (plain text has no way to say "right-align the second half of this line",
    so every entry spent a whole extra line on its dates -- which is most of
    why tailored CVs were running to two pages),
  * bold emphasis on the technique or technology that matters for the job
    (the old prompt banned markdown outright because the renderer would have
    printed the asterisks verbatim),
  * clickable project links.

So the model now returns structured data and this module owns the layout.
Everything factual in here -- names, employers, dates, locations, project
URLs, degrees -- arrives already reconciled against the user's stored profile
by cv_rewriter_agent, which is what makes it safe to render without checking
it again.

Both entry points accept a plain string as well as a document dict, and fall
back to the previous line-based layout when given one. That keeps a degraded
model response (unparseable JSON) renderable instead of throwing away a
rewrite that has already been paid for.
"""
import os
import re
from io import BytesIO
from xml.sax.saxutils import escape as _xml_escape, quoteattr as _xml_quoteattr

# ---- the reference format ----------------------------------------------------
#
# Every number below was measured with pdfplumber off the user's reference CV
# ("Aly_AI_&_Automation Engineer.pdf", a Word export) rather than guessed, so a
# tailored CV comes out in the same format as the one they maintain by hand:
#
#   * A4, 0.3in (21.6pt) side margins; section rules run 20.2 -> 575.3.
#   * Calibri throughout: name 26pt regular, headline 14pt bold, everything
#     else ~10pt (9.96) on a 12.2pt line pitch; dates/locations 9pt
#     bold-italic, right-aligned on the entry's own line.
#   * Section headers are black bold caps over a 0.72pt navy rule.
#   * LinkedIn / GitHub print as the words "Linkedin" / "Github", blue and
#     underlined, linking to the stored URL.
#
# Calibri is not redistributable, so it is not bundled: it is picked up from
# the system (Windows ships it), then Carlito -- the metric-compatible open
# clone (Debian/Ubuntu: fonts-crosextra-carlito) -- and only then Helvetica.
CV_ACCENT_COLOR = "#1F3864"   # section rules: (0.122, 0.22, 0.392)
CV_TEXT_COLOR = "#000000"
CV_MUTED_COLOR = "#000000"    # the reference prints dates in black, not grey
LINKS_COLOR = "#0070C0"       # (0.0, 0.439, 0.753), Word's "Blue, Accent 1"

BODY_SIZE = 9.96
LINE_PITCH = 12.2
META_SIZE = 9.0
NAME_SIZE = 26.04
HEADLINE_SIZE = 14.04

# Densities tried, in order, before any content is cut. 1.0 IS the reference
# format; the smaller ones only exist so an unusually long CV can still fit
# one page before a bullet has to go.
FIT_DENSITIES = (1.0, 0.97, 0.94, 0.91)

# The reference doesn't stretch its sections to fill the page, so neither does
# this: leftover space stays at the bottom, exactly where Word leaves it.
MAX_SECTION_GAP = 0.0

PAGE_MARGIN_X = 21.6
PAGE_MARGIN_TOP = 13.1
PAGE_MARGIN_BOTTOM = 21.6

# Rules overhang the text column by 1.4pt a side (20.2 vs 21.6).
RULE_OVERHANG = 1.4

# Bullets: the glyph sits 4.6pt in from the margin and its text 16pt, so a
# wrapped line aligns under the text rather than under the dot.
BULLET_TEXT_INDENT = 16.0
BULLET_GLYPH_INDENT = 4.6

# reportlab's base14 Helvetica only supports WinAnsiEncoding. LLMs routinely
# write "typographically correct" Unicode punctuation instead of plain ASCII
# (e.g. U+2010 HYPHEN for compound words like "machine-learning"), and a few
# of those codepoints aren't in WinAnsi. Confirmed by direct reproduction --
# building a one-line test PDF with each character and checking which font
# reportlab actually used per-glyph via pdfplumber -- that U+2010/2011/2012
# (hyphen variants) and U+2212 (minus sign) silently fall back to a
# ZapfDingbats/Symbol glyph at that character's byte position instead of
# erroring, which renders as a solid black square: exactly the artifact seen
# in real tailored-CV output. En/em dash, curly quotes, ellipsis, and NBSP
# were tested too and render fine in Helvetica, but are included below anyway
# for consistency/defense -- normalizing them costs nothing and protects
# against the same class of bug if the font ever changes.
_PDF_UNSAFE_PUNCTUATION = {
    "‐": "-", "‑": "-", "‒": "-",  # hyphen, non-breaking hyphen, figure dash
    "−": "-",                                 # minus sign
    "–": "-", "—": "-",                  # en dash, em dash
    "‘": "'", "’": "'",                   # curly single quotes
    "“": '"', "”": '"',                   # curly double quotes
    "…": "...",                                # ellipsis
    " ": " ",                                  # non-breaking space
}
_PDF_UNSAFE_PUNCTUATION_RE = re.compile(
    "|".join(re.escape(c) for c in _PDF_UNSAFE_PUNCTUATION))


def _sanitize_for_pdf_font(text: str) -> str:
    # Only the Helvetica fallback needs this: Calibri/Carlito are real TTFs
    # with the whole punctuation set, and the reference itself prints curly
    # apostrophes and en dashes.
    if _fonts()["ttf"]:
        return text or ""
    return _PDF_UNSAFE_PUNCTUATION_RE.sub(
        lambda m: _PDF_UNSAFE_PUNCTUATION[m.group(0)], text or "")


# ---- fonts -------------------------------------------------------------------

_FONT_CANDIDATES = (
    ("Calibri", ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf")),
    ("Carlito", ("Carlito-Regular.ttf", "Carlito-Bold.ttf",
                 "Carlito-Italic.ttf", "Carlito-BoldItalic.ttf")),
)


def _font_dirs() -> list[str]:
    dirs = [os.environ.get("CV_FONT_DIR", ""),
            os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
            os.path.expanduser("~/.fonts"), os.path.expanduser("~/.local/share/fonts"),
            "/Library/Fonts", os.path.expanduser("~/Library/Fonts")]
    for root in ("/usr/share/fonts", "/usr/local/share/fonts"):
        if os.path.isdir(root):
            dirs.extend(dirpath for dirpath, _, _ in os.walk(root))
    return [d for d in dirs if d and os.path.isdir(d)]


def _find_font_files(names) -> list[str] | None:
    """All four faces of one family, or None if any is missing -- a family with
    no bold face would render every <b> span as regular text."""
    dirs = _font_dirs()
    found = []
    for name in names:
        for directory in dirs:
            # Case-insensitive: Windows ships "calibri.ttf", other copies "Calibri.ttf".
            match = next((os.path.join(directory, f) for f in os.listdir(directory)
                          if f.lower() == name.lower()), None)
            if match:
                found.append(match)
                break
        else:
            return None
    return found


_FONT_CACHE: dict | None = None


def _fonts() -> dict:
    """Registered font names: regular / bold / italic / bold_italic, plus
    whether they are real TTFs ("ttf").

    Registered once per process, and as a family, so <b> and <i> inside a
    paragraph resolve to the real bold/italic faces.
    """
    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for family, files in _FONT_CANDIDATES:
        paths = _find_font_files(files)
        if not paths:
            continue
        names = [f"CV{family}", f"CV{family}-Bold", f"CV{family}-Italic",
                 f"CV{family}-BoldItalic"]
        try:
            for name, path in zip(names, paths):
                pdfmetrics.registerFont(TTFont(name, path))
        except Exception:  # noqa: BLE001 -- a broken font file falls through to the next family
            continue
        for bold, italic, name in ((0, 0, names[0]), (1, 0, names[1]),
                                   (0, 1, names[2]), (1, 1, names[3])):
            addMapping(names[0], bold, italic, name)
        _FONT_CACHE = {"regular": names[0], "bold": names[1], "italic": names[2],
                       "bold_italic": names[3], "ttf": True}
        return _FONT_CACHE

    _FONT_CACHE = {"regular": "Helvetica", "bold": "Helvetica-Bold",
                   "italic": "Helvetica-Oblique", "bold_italic": "Helvetica-BoldOblique",
                   "ttf": False}
    return _FONT_CACHE


# ---- dates -------------------------------------------------------------------

_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_date(value) -> str:
    """'2024-06' -> 'Jun/2024'; '2024' -> '2024'; 'present' -> 'Present'.

    Anything else is printed as written. Dates reach here straight from the
    profile, where they are whatever the user typed or the extractor read, and
    a date this can't parse is far more likely to be an unusual real format
    than a mistake worth hiding.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower() in ("present", "current", "now", "ongoing"):
        return "Present"
    match = re.match(r"^(\d{4})[-/](\d{1,2})$", text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return f"{_MONTH_NAMES[month - 1]}/{year}"
    return text


def format_date_range(start, end) -> str:
    left, right = format_date(start), format_date(end)
    if left and right:
        return f"{left} - {right}"
    return left or right


# ---- inline markup -----------------------------------------------------------
#
# Bullets carry **bold** spans marking the one or two terms that matter for the
# target job. Escaping happens FIRST and the tags are inserted after, so a
# literal "<" or "&" in a bullet can never become markup -- and so the
# asterisks can never reach the page, which is what the old renderer did.

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def _inline(text: str) -> str:
    escaped = _xml_escape(str(text or ""))
    marked = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    # Any unpaired ** left over would print as asterisks; drop them.
    return marked.replace("**", "")


def strip_markup(text: str) -> str:
    """The same string without the bold markers, for the plain-text rendering.

    The scorer quotes evidence spans back verbatim in its explanations, so
    asterisks left in the text would surface in the UI as "Built **RAG**
    pipelines".
    """
    return _BOLD_RE.sub(r"\1", str(text or "")).replace("**", "")


def _link(url: str, label: str, color: str = LINKS_COLOR) -> str:
    """A clickable span. `url` is trusted only as far as being made safe for an
    XML attribute -- reportlab will happily write a malformed href otherwise."""
    href = _browsable(url)
    if not href:
        return _xml_escape(label)
    return (f'<link href={_xml_quoteattr(href)}>'
            f'<font color="{color}"><u>{_xml_escape(label)}</u></font></link>')


def _browsable(url: str) -> str | None:
    """Mirrors the Profile page's own rule: a stored link is whatever the user
    typed, and the scheme is added only when something needs to follow it."""
    text = str(url or "").strip()
    if not text:
        return None
    if re.match(r"^https?://", text, re.I):
        return text
    if re.match(r"^[\w-]+(\.[\w-]+)+([/?#].*)?$", text):
        return f"https://{text}"
    return None


# ---- plain-text rendering ----------------------------------------------------

def render_cv_text(document) -> str:
    """The document as plain text: ALL-CAPS section headers, '- ' bullets.

    This is what gets re-scored and what the debug bench shows in its text
    pane. The section headers deliberately match the ones
    cv_profile._SECTION_PATTERNS knows, because rescoring reads evidence
    structurally out of this text rather than paying for a second parse.
    """
    if isinstance(document, str):
        return document
    if not isinstance(document, dict):
        return ""

    doc = document
    lines: list[str] = []

    headline = str(doc.get("headline") or "").strip()
    name = str(doc.get("name") or "").strip()
    # Name first: cv_profile's structural reader and every ATS treat the top
    # line as the candidate, and the headline is a subtitle even when it is
    # printed larger.
    if name:
        lines.append(name)
    if headline:
        lines.append(headline)

    contact = _contact_parts(doc.get("contact"))
    if contact:
        lines.append(" | ".join(contact))

    def section(title, body_lines):
        if not body_lines:
            return
        lines.append("")
        lines.append(title)
        lines.extend(body_lines)

    summary = strip_markup(doc.get("summary")).strip()
    section("SUMMARY", [summary] if summary else [])

    body = []
    for entry in doc.get("experience") or []:
        head = " | ".join(p for p in (entry.get("title"), entry.get("organization")) if p)
        meta = " | ".join(p for p in (format_date_range(entry.get("start"), entry.get("end")),
                                      entry.get("location")) if p)
        body.append(" | ".join(p for p in (head, meta) if p))
        body.extend(f"- {strip_markup(b)}" for b in entry.get("bullets") or [])
    section("EXPERIENCE", body)

    body = []
    for entry in doc.get("projects") or []:
        head = str(entry.get("name") or "")
        parts = [head]
        if entry.get("url"):
            parts.append(str(entry["url"]))
        dates = format_date_range(entry.get("start"), entry.get("end"))
        if dates:
            parts.append(dates)
        body.append(" | ".join(p for p in parts if p))
        body.extend(f"- {strip_markup(b)}" for b in entry.get("bullets") or [])
    if doc.get("note"):
        body.append(strip_markup(doc["note"]))
    section("PROJECTS", body)

    body = []
    for entry in doc.get("education") or []:
        degree = ", ".join(p for p in (entry.get("degree"), entry.get("field")) if p)
        head = " | ".join(p for p in (degree, entry.get("institution")) if p)
        meta = " | ".join(p for p in (format_date_range(entry.get("start"), entry.get("end")),
                                      entry.get("location")) if p)
        body.append(" | ".join(p for p in (head, meta) if p))
    section("EDUCATION", body)

    body = [f"{group.get('category')}: {', '.join(group.get('items') or [])}"
            for group in doc.get("skills") or [] if group.get("items")]
    section("TECHNICAL SKILLS", body)

    return "\n".join(lines).strip()


def _contact_parts(contact) -> list[str]:
    contact = contact if isinstance(contact, dict) else {}
    order = ("email", "phone", "location", "linkedin", "github")
    return [str(contact.get(k)).strip() for k in order if str(contact.get(k) or "").strip()]


# ---- PDF ---------------------------------------------------------------------

def save_cv_as_pdf(document, output_path: str) -> None:
    """Render the document to `output_path` in the blueprint's layout.

    Given a plain string instead of a document, falls back to the previous
    line-based layout (see _legacy_story) so a degraded rewrite still produces
    a readable PDF.
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if isinstance(document, str) or not isinstance(document, dict):
        _build_pdf(_legacy_story(str(document or ""), _styles()), output_path)
        return

    # One page is the target. Density first, content last: the document is
    # rendered at each density in turn and only starts losing bullets once the
    # tightest one still overflows. Trimming is then deterministic and ordered
    # least-costly-first (see _trim_step) rather than letting the page break
    # wherever it lands -- a CV whose last project is split across a page
    # boundary reads worse than one without that project.
    working = _copy_document(document)

    # 1. Cut only as much as the tightest density needs. Content is the last
    #    thing to go, so this asks the floor -- not the preferred size --
    #    whether the document fits at all.
    for _ in range(9):
        if _measure_pages(_document_story(working, FIT_DENSITIES[-1])) <= 1:
            break
        if not _trim_step(working):
            break

    # 2. Then set it as large as it will go. Re-run from the top rather than
    #    keeping the floor: whatever step 1 removed may have freed enough room
    #    for a comfortable size, and small type on a page that could have held
    #    bigger type is a worse CV than either.
    scale = FIT_DENSITIES[-1]
    for density in FIT_DENSITIES:
        if _measure_pages(_document_story(working, density)) <= 1:
            scale = density
            break

    # 3. Spread the remaining slack between the sections instead of leaving it
    #    pooled at the bottom.
    gap = _section_gap_that_fills_the_page(working, scale)
    _build_pdf(_document_story(working, scale, gap), output_path)


def _section_gap_that_fills_the_page(doc: dict, scale: float) -> float:
    """The largest per-section gap that still fits on one page.

    Binary search rather than arithmetic: the leftover space is not simply
    divisible, because widening a gap can push a paragraph into a different
    wrap and change the height by more than the gap itself. Measuring is a
    ~10ms build, so seven of them is cheaper than being clever and wrong.
    """
    if _measure_pages(_document_story(doc, scale, MAX_SECTION_GAP)) <= 1:
        return MAX_SECTION_GAP
    low, high = 0.0, MAX_SECTION_GAP
    for _ in range(6):
        middle = (low + high) / 2
        if _measure_pages(_document_story(doc, scale, middle)) <= 1:
            low = middle
        else:
            high = middle
    return low


# The frame SimpleDocTemplate builds pads its content by 6pt a side, so the
# usable width is 12pt narrower than doc.width. Getting this wrong is not
# subtle: a table exactly doc.width wide is WIDER than the frame's content
# area, and Table's default hAlign of CENTER then shifts it 6pt left of every
# paragraph on the page -- which is exactly why entry rows used to sit a hair
# left of the section headers above them.
_FRAME_PADDING = 12


def _doc_width():
    from reportlab.lib.pagesizes import A4
    return A4[0] - 2 * PAGE_MARGIN_X


def _new_doc(target):
    """A document whose FIRST TEXT lands exactly on PAGE_MARGIN_X/TOP.

    The margins passed here are not where the text goes: the frame adds its
    own 6pt of padding inside them. Subtracting half of _FRAME_PADDING is what
    makes the measured geometry above come out right on the page.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate
    inset = _FRAME_PADDING / 2
    return SimpleDocTemplate(
        target, pagesize=A4,
        leftMargin=PAGE_MARGIN_X - inset, rightMargin=PAGE_MARGIN_X - inset,
        topMargin=PAGE_MARGIN_TOP - inset, bottomMargin=PAGE_MARGIN_BOTTOM - inset,
        title="Curriculum Vitae", author="",
    )


def _build_pdf(story, output_path: str) -> None:
    doc = _new_doc(output_path)
    doc.build(story or [_empty_paragraph()])


def _measure_pages(story) -> int:
    """Page count of a trial build. Platypus consumes the flowables it lays
    out, so this builds from a deep copy into memory and throws the bytes
    away -- reusing the story afterwards would render a half-empty CV."""
    from copy import deepcopy
    doc = _new_doc(BytesIO())
    try:
        doc.build(deepcopy(story) or [_empty_paragraph()])
    except Exception:  # noqa: BLE001 -- measurement must never break rendering
        return 1
    return getattr(doc, "page", 1)


def _empty_paragraph():
    from reportlab.platypus import Paragraph
    return Paragraph("(empty CV)", _styles()["body"])


def _styles(scale: float = 1.0, section_gap: float = 0.0):
    """The stylesheet at a given density.

    scale=1.0 is the reference format. Smaller values tighten type and spacing
    together so a long CV can be squeezed before any content is cut -- which
    is the order a person does it in (see FIT_DENSITIES).
    """
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT

    fonts = _fonts()
    text = HexColor(CV_TEXT_COLOR)
    base = getSampleStyleSheet()["Normal"]

    def s(value):
        return round(value * scale, 2)

    # NOTE: ParagraphStyle inherits `leading` (line height) from its parent
    # unless set explicitly, so every style here sets its own -- a 26pt name
    # on the parent's 12pt leading would overlap the line under it.
    return {
        # The reference puts the name first and large, the headline under it.
        "name": ParagraphStyle(
            "CVName", parent=base, fontName=fonts["regular"], fontSize=s(NAME_SIZE),
            leading=s(32.4), textColor=text, alignment=TA_CENTER),
        "headline": ParagraphStyle(
            "CVHeadline", parent=base, fontName=fonts["bold"], fontSize=s(HEADLINE_SIZE),
            leading=s(22.2), textColor=text, alignment=TA_CENTER),
        "contact": ParagraphStyle(
            "CVContact", parent=base, fontName=fonts["regular"], fontSize=s(BODY_SIZE),
            leading=s(LINE_PITCH), textColor=text, alignment=TA_CENTER,
            spaceAfter=s(12.0)),
        "header": ParagraphStyle(
            "CVHeader", parent=base, fontName=fonts["bold"], fontSize=s(BODY_SIZE),
            leading=s(LINE_PITCH), textColor=text, spaceBefore=s(6.0) + section_gap,
            spaceAfter=0),
        "body": ParagraphStyle(
            "CVBody", parent=base, fontName=fonts["regular"], fontSize=s(BODY_SIZE),
            leading=s(LINE_PITCH), textColor=text, embeddedHyphenation=1),
        "skills": ParagraphStyle(
            "CVSkills", parent=base, fontName=fonts["regular"], fontSize=s(BODY_SIZE),
            leading=s(LINE_PITCH), textColor=text),
        "entry": ParagraphStyle(
            "CVEntry", parent=base, fontName=fonts["regular"], fontSize=s(BODY_SIZE),
            leading=s(LINE_PITCH), textColor=text),
        "meta": ParagraphStyle(
            # Leading shortened by the size difference so, bottom-aligned in the
            # entry row, the 9pt dates land on the 10pt title's baseline.
            "CVMeta", parent=base, fontName=fonts["bold_italic"], fontSize=s(META_SIZE),
            leading=s(LINE_PITCH - (BODY_SIZE - META_SIZE)),
            textColor=HexColor(CV_MUTED_COLOR), alignment=TA_RIGHT),
        # Hanging indent via bulletText: the glyph at BULLET_GLYPH_INDENT, the
        # text -- and every wrapped line -- at BULLET_TEXT_INDENT.
        "bullet": ParagraphStyle(
            "CVBullet", parent=base, fontName=fonts["regular"], fontSize=s(BODY_SIZE),
            leading=s(LINE_PITCH), textColor=text, leftIndent=BULLET_TEXT_INDENT,
            bulletIndent=BULLET_GLYPH_INDENT, bulletFontName=fonts["regular"],
            bulletFontSize=s(BODY_SIZE), spaceAfter=s(3.1), embeddedHyphenation=1),
        "note": ParagraphStyle(
            "CVNote", parent=base, fontName=fonts["italic"], fontSize=s(META_SIZE),
            leading=s(11), textColor=text, spaceBefore=s(2)),
    }


def _rule(space_after: float = 8.4):
    """The navy section rule, overhanging the text column like the reference's.

    Sits 12.1pt below the header's top; the next line's top follows 10.9pt
    under it (8.8pt for the summary paragraph, which the reference sets
    tighter -- hence the parameter)."""
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import HRFlowable
    return HRFlowable(width=_doc_width() + 2 * RULE_OVERHANG, thickness=0.72,
                      color=HexColor(CV_ACCENT_COLOR), hAlign="CENTER",
                      spaceBefore=1.7, spaceAfter=space_after)


def _date_range_for_pdf(start, end) -> str:
    """The reference separates the two dates with an en dash. The plain-text
    rendering keeps its ASCII hyphen, which is what rescoring reads."""
    text = format_date_range(start, end)
    return text.replace(" - ", " – ") if _fonts()["ttf"] else text


def _entry_row(left_html: str, right_html: str, styles, width, scale: float = 1.0):
    """The blueprint's defining row: title and employer on the left, dates and
    location right-aligned on the same baseline. A two-cell table rather than
    a tab stop, because platypus wraps a long left side onto a second line and
    a tab stop would drag the right side down with it."""
    from reportlab.platypus import Paragraph, Table, TableStyle

    table = Table(
        [[Paragraph(left_html, styles["entry"]), Paragraph(right_html, styles["meta"])]],
        colWidths=[width * 0.7, width * 0.3],
        hAlign="LEFT",   # never centre-nudge the row off the page's left edge
    )
    table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        # BOTTOM, so the 9pt dates share the 10pt title's baseline as in the
        # reference, rather than riding a point above it.
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
    ]))
    # As flowable space rather than cell padding: platypus collapses adjacent
    # space to the larger value, which is how the reference's gaps behave
    # (6.2pt after a bullet, but only the rule's own gap right under a header).
    table.spaceBefore = round(6.2 * scale, 2)
    table.spaceAfter = round(2.3 * scale, 2)
    return table


def _document_story(doc: dict, scale: float = 1.0, section_gap: float = 0.0):
    from reportlab.platypus import Paragraph, Spacer

    styles = _styles(scale, section_gap)
    width = _doc_width()
    story = []

    def clean(value):
        return _sanitize_for_pdf_font(str(value or "")).strip()

    def section(title, rule_space_after=8.4):
        story.append(Paragraph(_xml_escape(title), styles["header"]))
        story.append(_rule(rule_space_after))

    def meta(*parts):
        return _xml_escape(" | ".join(p for p in parts if p))

    # ---- header block: name, then headline ----
    name, headline = clean(doc.get("name")), clean(doc.get("headline"))
    if name:
        story.append(Paragraph(_inline(name), styles["name"]))
    if headline:
        story.append(Paragraph(_inline(headline), styles["headline" if name else "name"]))

    contact = doc.get("contact") if isinstance(doc.get("contact"), dict) else {}
    rendered = [_xml_escape(clean(contact.get(key)))
                for key in ("email", "phone", "location") if clean(contact.get(key))]
    # Profiles print as a word, not a URL -- the reference's "Linkedin | Github".
    for key, label in (("linkedin", "Linkedin"), ("github", "Github")):
        value = clean(contact.get(key))
        if value:
            link = _link(value, label, LINKS_COLOR) if _browsable(value) else _xml_escape(value)
            rendered.append(f'<font size="{round(META_SIZE * scale, 2)}">{link}</font>')
    if rendered:
        story.append(Paragraph("&nbsp;&nbsp;|&nbsp;&nbsp;".join(rendered), styles["contact"]))

    # ---- summary ----
    summary = clean(doc.get("summary"))
    if summary:
        section("SUMMARY", rule_space_after=6.3)
        story.append(Paragraph(_inline(summary), styles["body"]))

    def bullets(items):
        for item in items or []:
            text = clean(item)
            if text:
                story.append(Paragraph(_inline(text), styles["bullet"], bulletText="\u2022"))

    # ---- experience: **Title** | Organization ......... dates | location ----
    experience = [e for e in doc.get("experience") or [] if isinstance(e, dict)]
    if experience:
        section("EXPERIENCE")
        for entry in experience:
            left = " | ".join(p for p in (f"<b>{_inline(clean(entry.get('title')))}</b>"
                                          if entry.get("title") else "",
                                          _inline(clean(entry.get("organization")))) if p)
            right = meta(_date_range_for_pdf(entry.get("start"), entry.get("end")),
                         clean(entry.get("location")))
            story.append(_entry_row(left, right, styles, width, scale))
            bullets(entry.get("bullets"))

    # ---- projects: the whole name in bold, dates only ----
    projects = [p for p in doc.get("projects") or [] if isinstance(p, dict)]
    if projects:
        section("PROJECTS")
        for entry in projects:
            left = f"<b>{_inline(clean(entry.get('name')))}</b>" if entry.get("name") else ""
            if _browsable(entry.get("url")):
                left += f" | {_link(clean(entry.get('url')), 'Link')}"
            right = meta(_date_range_for_pdf(entry.get("start"), entry.get("end")))
            story.append(_entry_row(left, right, styles, width, scale))
            bullets(entry.get("bullets"))
        note = clean(doc.get("note"))
        if note:
            story.append(Paragraph(_inline(note), styles["note"]))

    # ---- education: **Degree in Field** | Institution ..... dates | location ----
    education = [e for e in doc.get("education") or [] if isinstance(e, dict)]
    if education:
        section("EDUCATION")
        for entry in education:
            degree = " in ".join(p for p in (clean(entry.get("degree")),
                                             clean(entry.get("field"))) if p)
            left = " | ".join(p for p in (f"<b>{_inline(degree)}</b>" if degree else "",
                                          _inline(clean(entry.get("institution")))) if p)
            right = meta(_date_range_for_pdf(entry.get("start"), entry.get("end")),
                         clean(entry.get("location")))
            story.append(_entry_row(left, right, styles, width, scale))

    # ---- skills: **Category:** items ----
    groups = [g for g in doc.get("skills") or []
              if isinstance(g, dict) and g.get("items")]
    if groups:
        section("TECHNICAL SKILLS")
        for group in groups:
            category = clean(group.get("category"))
            items = ", ".join(clean(i) for i in group["items"] if clean(i))
            label = f"<b>{_inline(category)}:</b> " if category else ""
            story.append(Paragraph(f"{label}{_inline(items)}", styles["skills"]))

    if not story:
        story.append(Spacer(1, 1))
    return story


# ---- one-page budget ---------------------------------------------------------

def _copy_document(doc: dict) -> dict:
    from copy import deepcopy
    return deepcopy(doc)


def _trim_step(doc: dict) -> bool:
    """Remove exactly one thing, cheapest first. True if something was removed.

    The order encodes what a CV can afford to lose: the optional footnote, then
    detail on the projects the model itself ranked least relevant, then whole
    trailing projects, and only then a line of work history -- never below two
    bullets for a job, and never a job or a degree, which would leave an
    unexplained gap.
    """
    if doc.get("note"):
        doc["note"] = ""
        return True

    projects = [p for p in doc.get("projects") or [] if isinstance(p, dict)]
    for floor in (3, 2, 1):
        for entry in reversed(projects):
            if len(entry.get("bullets") or []) > floor:
                entry["bullets"] = entry["bullets"][:floor]
                return True

    if len(projects) > 2:
        doc["projects"] = projects[:-1]
        return True

    experience = [e for e in doc.get("experience") or [] if isinstance(e, dict)]
    for floor in (3, 2):
        for entry in reversed(experience):
            if len(entry.get("bullets") or []) > floor:
                entry["bullets"] = entry["bullets"][:floor]
                return True

    return False


# ---- legacy plain-text layout ------------------------------------------------

_HEADER_KEYWORDS = {
    "summary", "profile", "objective", "professional summary",
    "experience", "work experience", "employment", "employment history",
    "education", "skills", "technical skills", "core skills",
    "projects", "certifications", "achievements", "awards",
    "publications", "languages", "interests", "contact",
}


def _looks_like_header(line: str) -> bool:
    bare = line.strip().rstrip(":").strip()
    if not bare:
        return False
    if bare.lower() in _HEADER_KEYWORDS:
        return True
    # short, all-caps line with no sentence punctuation reads as a header
    if len(bare) <= 40 and not bare.endswith((".", ",", ";")) and any(c.isalpha() for c in bare):
        letters = [c for c in bare if c.isalpha()]
        if letters and all(c.isupper() for c in letters):
            return True
    return False


def _looks_like_bullet(line: str) -> bool:
    return bool(re.match(r"^[-*•–]\s+", line.strip()))


def _legacy_story(cv_text: str, styles):
    """The pre-blueprint layout, kept for plain-text input: name, contact line,
    ALL-CAPS headers with a rule, '- ' bullets."""
    from reportlab.platypus import Paragraph, Spacer

    story = []
    found_name = False
    checked_contact_line = False

    for raw_line in _sanitize_for_pdf_font(cv_text).split("\n"):
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 6))
            continue

        if not found_name:
            story.append(Paragraph(_inline(line), styles["name"]))
            found_name = True
            continue

        if not checked_contact_line:
            checked_contact_line = True
            if not _looks_like_header(line) and not _looks_like_bullet(line):
                story.append(Paragraph(_inline(line), styles["contact"]))
                continue
            # No contact line present -- fall through and process it normally.

        if _looks_like_bullet(line):
            text = re.sub(r"^[-*•–]\s+", "", line)
            story.append(Paragraph(_inline(text), styles["bullet"], bulletText="\u2022"))
        elif _looks_like_header(line):
            story.append(Paragraph(_inline(line), styles["header"]))
            story.append(_rule())
        else:
            story.append(Paragraph(_inline(line), styles["body"]))

    return story or [_empty_paragraph()]
