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

# Accent color sampled directly from the uploaded CV (cv/current_cv.pdf) --
# its section-divider rules and header text use this exact navy, extracted
# via pdfplumber (non_stroking_color (0.122, 0.227, 0.373) -> this hex).
# Reportlab can't match the original's Calibri/Times mix without embedding
# font files (Calibri isn't freely redistributable), so this uses Helvetica
# throughout -- a clean, portable sans-serif -- rather than the original's
# specific fonts.
CV_ACCENT_COLOR = "#555555"
CV_MUTED_COLOR = "#555555"
LINKS_COLOR = "#0462C0"  # Google's blue for links, for consistency with the web
# Densities tried, in order, before any content is cut -- LARGEST first, so a
# CV that would otherwise end an inch above the bottom margin is set bigger
# rather than left floating in white space. The floor is the last one: 9.5pt
# body text scaled to ~8.7pt, which still prints cleanly and is what most
# one-page CVs are set in anyway.
FIT_DENSITIES = (1.12, 1.08, 1.04, 1.0, 0.95, 0.91)

# Whatever vertical slack is left after the density is chosen gets spread
# between the sections rather than pooling at the bottom of the page. Capped,
# because a sparse CV stretched to the margins reads as padding.
MAX_SECTION_GAP = 16.0

# Page geometry, measured off the user's own CV (cv/current_cv.pdf) with
# pdfplumber rather than guessed: every line of text on it -- section headers,
# body, entry titles, skills rows -- starts at x=31.0, its divider rules run
# 29.5 to 582.5, and its content runs from y=17.5 to y=777.7. Those are narrow
# margins by word-processor standards and they are most of why that CV fits on
# one page; matching them is what lets a tailored CV hold the same amount.
PAGE_MARGIN_X = 31.0
PAGE_MARGIN_TOP = 18.0
PAGE_MARGIN_BOTTOM = 18.0

# Bullets, same source: the glyph sits 4.5pt in from the margin and its text
# 15.9pt, so a wrapped line aligns under the text rather than under the dot.
BULLET_TEXT_INDENT = 16.0
BULLET_GLYPH_INDENT = 4.5

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
    return _PDF_UNSAFE_PUNCTUATION_RE.sub(
        lambda m: _PDF_UNSAFE_PUNCTUATION[m.group(0)], text or "")


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
    from reportlab.lib.pagesizes import LETTER
    return LETTER[0] - 2 * PAGE_MARGIN_X


def _new_doc(target):
    """A document whose FIRST TEXT lands exactly on PAGE_MARGIN_X/TOP.

    The margins passed here are not where the text goes: the frame adds its
    own 6pt of padding inside them. Subtracting half of _FRAME_PADDING is what
    makes the measured geometry above come out right on the page.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.platypus import SimpleDocTemplate
    inset = _FRAME_PADDING / 2
    return SimpleDocTemplate(
        target, pagesize=LETTER,
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

    `scale` tightens type and spacing together so the page can be squeezed
    before any content is cut -- which is the order a person does it in, and
    the difference between a CV that keeps its third project and one that
    doesn't. Only three densities are ever used (see FIT_DENSITIES); the
    tightest is still comfortably readable at print size.
    """
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT

    accent = HexColor(CV_ACCENT_COLOR)
    muted = HexColor(CV_MUTED_COLOR)
    base = getSampleStyleSheet()["Normal"]

    def s(value):
        return round(value * scale, 2)

    # NOTE: ParagraphStyle inherits `leading` (line height) from its parent
    # unless set explicitly -- base has fontSize=10/leading=12, so any style
    # bumping fontSize well past that (e.g. the 19pt headline) MUST set its
    # own leading too, or the next paragraph starts before the glyphs' actual
    # height clears and visibly overlaps it.
    return {
        "headline": ParagraphStyle(
            "CVHeadline", parent=base, fontName="Helvetica-Bold", fontSize=s(19),
            leading=s(22), textColor=accent, alignment=TA_CENTER, spaceAfter=1),
        "name": ParagraphStyle(
            "CVName", parent=base, fontName="Helvetica-Bold", fontSize=s(13),
            leading=s(16), alignment=TA_CENTER, spaceAfter=s(3)),
        "contact": ParagraphStyle(
            "CVContact", parent=base, fontName="Helvetica", fontSize=s(8.8),
            leading=s(11.5), textColor=muted, alignment=TA_CENTER, spaceAfter=s(9)),
        "header": ParagraphStyle(
            "CVHeader", parent=base, fontName="Helvetica-Bold", fontSize=s(10.5),
            leading=s(13), textColor=accent, spaceBefore=s(8) + section_gap,
            spaceAfter=1),
        "body": ParagraphStyle(
            "CVBody", parent=base, fontName="Helvetica", fontSize=s(9.5),
            leading=s(12.2), spaceAfter=s(2)),
        "entry": ParagraphStyle(
            "CVEntry", parent=base, fontName="Helvetica", fontSize=s(9.5),
            leading=s(12.2)),
        "meta": ParagraphStyle(
            "CVMeta", parent=base, fontName="Helvetica-BoldOblique", fontSize=s(8.8),
            leading=s(12.2), textColor=muted, alignment=TA_RIGHT),
        # Hanging indent, at the same offsets the user's own CV uses: wrapped
        # lines align under the text, not under the dot.
        "bullet": ParagraphStyle(
            "CVBullet", parent=base, fontName="Helvetica", fontSize=s(9.5),
            leading=s(12.2), leftIndent=BULLET_TEXT_INDENT,
            firstLineIndent=BULLET_GLYPH_INDENT - BULLET_TEXT_INDENT,
            spaceAfter=s(1.5)),
        # "note": ParagraphStyle(
        #     "CVNote", parent=base, fontName="Helvetica-Oblique", fontSize=s(8.5),
        #     leading=s(11), textColor=muted, spaceBefore=s(2)),
    }


def _entry_row(left_html: str, right_html: str, styles, width):
    """The blueprint's defining row: title and employer on the left, dates and
    location right-aligned on the same baseline. A two-cell table rather than
    a tab stop, because platypus wraps a long left side onto a second line and
    a tab stop would drag the right side down with it."""
    from reportlab.platypus import Paragraph, Table, TableStyle

    table = Table(
        [[Paragraph(left_html, styles["entry"]), Paragraph(right_html, styles["meta"])]],
        colWidths=[width * 0.65, width * 0.35],
        hAlign="LEFT",   # never centre-nudge the row off the page's left edge
    )
    table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return table


def _document_story(doc: dict, scale: float = 1.0, section_gap: float = 0.0):
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import Paragraph, Spacer, HRFlowable

    styles = _styles(scale, section_gap)
    width = _doc_width()
    accent = HexColor(CV_ACCENT_COLOR)
    story = []

    def clean(value):
        return _sanitize_for_pdf_font(str(value or "")).strip()

    def section(title):
        story.append(Paragraph(_xml_escape(title), styles["header"]))
        story.append(HRFlowable(width="100%", thickness=0.75, color=accent,
                                spaceBefore=1, spaceAfter=4))

    # ---- header block ----
    headline, name = clean(doc.get("headline")), clean(doc.get("name"))
    if headline and name:
        story.append(Paragraph(_inline(headline), styles["headline"]))
        story.append(Paragraph(_inline(name), styles["name"]))
    elif name:
        story.append(Paragraph(_inline(name), styles["headline"]))
    elif headline:
        story.append(Paragraph(_inline(headline), styles["headline"]))

    contact = doc.get("contact") if isinstance(doc.get("contact"), dict) else {}
    rendered = []
    for key in ("email", "phone", "location", "linkedin", "github"):
        value = clean(contact.get(key))
        if not value:
            continue
        rendered.append(_link(value, value, LINKS_COLOR)
                        if key in ("linkedin", "github") and _browsable(value)
                        else _xml_escape(value))
    if rendered:
        story.append(Paragraph("&nbsp;&nbsp;|&nbsp;&nbsp;".join(rendered), styles["contact"]))

    # ---- summary ----
    summary = clean(doc.get("summary"))
    if summary:
        section("SUMMARY")
        story.append(Paragraph(_inline(summary), styles["body"]))

    def bullets(items):
        for item in items or []:
            text = clean(item)
            if text:
                story.append(Paragraph(f"\u2022&nbsp;&nbsp;{_inline(text)}", styles["bullet"]))

    # ---- experience ----
    experience = [e for e in doc.get("experience") or [] if isinstance(e, dict)]
    if experience:
        section("EXPERIENCE")
        for entry in experience:
            left = " | ".join(p for p in (f"<b>{_inline(clean(entry.get('title')))}</b>"
                                          if entry.get("title") else "",
                                          _inline(clean(entry.get("organization")))) if p)
            right = " | ".join(p for p in (
                _xml_escape(format_date_range(entry.get("start"), entry.get("end"))),
                _xml_escape(clean(entry.get("location")))) if p)
            story.append(_entry_row(left, right, styles, width))
            bullets(entry.get("bullets"))

    # ---- projects ----
    projects = [p for p in doc.get("projects") or [] if isinstance(p, dict)]
    if projects:
        section("PROJECTS")
        for entry in projects:
            parts = [f"<b>{_inline(clean(entry.get('name')))}</b>"] if entry.get("name") else []
            if _browsable(entry.get("url")):
                parts.append(f" | ({_link(clean(entry.get('url')), 'Link')})")
            left = " ".join(parts)
            right = _xml_escape(format_date_range(entry.get("start"), entry.get("end")))
            story.append(_entry_row(left, right, styles, width))
            bullets(entry.get("bullets"))
        note = clean(doc.get("note"))
        if note:
            story.append(Paragraph(_inline(note), styles["note"]))

    # ---- education ----
    education = [e for e in doc.get("education") or [] if isinstance(e, dict)]
    if education:
        section("EDUCATION")
        for entry in education:
            degree = ", ".join(p for p in (clean(entry.get("degree")),
                                           clean(entry.get("field"))) if p)
            left = " | ".join(p for p in (f"<b>{_inline(degree)}</b>" if degree else "",
                                          _inline(clean(entry.get("institution")))) if p)
            right = " | ".join(p for p in (
                _xml_escape(format_date_range(entry.get("start"), entry.get("end"))),
                _xml_escape(clean(entry.get("location")))) if p)
            story.append(_entry_row(left, right, styles, width))

    # ---- skills ----
    groups = [g for g in doc.get("skills") or []
              if isinstance(g, dict) and g.get("items")]
    if groups:
        section("TECHNICAL SKILLS")
        for group in groups:
            category = clean(group.get("category"))
            items = ", ".join(clean(i) for i in group["items"] if clean(i))
            label = f"<b>{_inline(category)}:</b> " if category else ""
            story.append(Paragraph(f"{label}{_inline(items)}", styles["body"]))

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
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import Paragraph, Spacer, HRFlowable

    accent = HexColor(CV_ACCENT_COLOR)
    story = []
    found_name = False
    checked_contact_line = False

    for raw_line in _sanitize_for_pdf_font(cv_text).split("\n"):
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 6))
            continue

        if not found_name:
            story.append(Paragraph(_inline(line), styles["headline"]))
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
            story.append(Paragraph(f"\u2022&nbsp;&nbsp;{_inline(text)}", styles["bullet"]))
        elif _looks_like_header(line):
            story.append(Paragraph(_inline(line), styles["header"]))
            story.append(HRFlowable(width="100%", thickness=0.75, color=accent,
                                    spaceBefore=1, spaceAfter=6))
        else:
            story.append(Paragraph(_inline(line), styles["body"]))

    return story or [_empty_paragraph()]
