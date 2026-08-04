"""
CV Rewriter Agent.

Integrity rule: this must reframe real experience for a job description, never
invent skills or history the candidate doesn't have. The system prompt enforces
this explicitly, and missing skills are surfaced separately rather than woven in.
"""
import os
import re
from xml.sax.saxutils import escape as _xml_escape

from langchain_core.messages import SystemMessage, HumanMessage
from agents.llm import get_llm


def rewrite_cv(cv_text: str, job_description: str, missing_skills: list[str]) -> str:
    llm = get_llm(temperature=0.3)
    messages = [
        SystemMessage(content=(
            "Rewrite this CV to be ATS-optimized for the given job description. "
            "Naturally incorporate relevant keywords the candidate genuinely has experience "
            "with, restructure bullet points around measurable impact, and do NOT fabricate "
            "skills or experience the candidate doesn't have. If a required skill is missing, "
            "leave it out of the CV rather than inventing it — missing skills are reported "
            "separately.\n\n"
            "Format the output as plain text laid out like a real CV, so it can be rendered "
            "into a PDF directly, matching the structure of the original CV:\n"
            "- First line: the candidate's name, exactly as it appears in the original CV.\n"
            "- Second line: contact info (email, phone, location, LinkedIn -- whatever the "
            "original CV includes), on a single line, exactly as it appears in the original. "
            "Do not skip this line even if it's long.\n"
            "- A blank line, then section headers in ALL CAPS on their own line (e.g. SUMMARY, "
            "EXPERIENCE, EDUCATION, SKILLS) -- use whichever sections the original CV actually "
            "has, in the same order.\n"
            "- Bullet points for individual achievements/responsibilities, each starting "
            "with '- '.\n"
            "- No markdown symbols (no #, no **, no backticks) -- headers and bullets should "
            "read correctly as plain text on their own."
        )),
        HumanMessage(content=(
            f"Current CV:\n{cv_text}\n\n"
            f"Job Description:\n{job_description}\n\n"
            f"Known missing skills (do NOT fabricate these into the CV):\n{missing_skills}"
        )),
    ]
    resp = llm.invoke(messages)
    return resp.content


def save_cv_as_docx(cv_text: str, output_path: str):
    from docx import Document
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    doc = Document()
    for line in cv_text.split("\n"):
        doc.add_paragraph(line)
    doc.save(output_path)


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
    # short, all-caps-or-titlecase line with no sentence punctuation reads as a header
    if len(bare) <= 40 and not bare.endswith((".", ",", ";")) and any(c.isalpha() for c in bare):
        letters = [c for c in bare if c.isalpha()]
        if letters and all(c.isupper() for c in letters):
            return True
    return False


def _looks_like_bullet(line: str) -> bool:
    return bool(re.match(r"^[-*•–]\s+", line.strip()))


# Accent color sampled directly from the uploaded CV (cv/current_cv.pdf) --
# its section-divider rules and header text use this exact navy, extracted
# via pdfplumber (non_stroking_color (0.122, 0.227, 0.373) -> this hex).
# Reportlab can't match the original's Calibri/Times mix without embedding
# font files (Calibri isn't freely redistributable), so this uses Helvetica
# throughout -- a clean, portable sans-serif -- rather than the original's
# specific fonts. The goal is matching the dominant visual signature (accent
# color, centered name/contact header, divider rules under section headers)
# rather than a pixel-identical reproduction.
CV_ACCENT_COLOR = "#1F3A5F"


def save_cv_as_pdf(cv_text: str, output_path: str):
    """
    Renders CV text into a formatted PDF styled after the uploaded CV: a
    centered name + contact line, section headers in the accent color with a
    full-width divider rule beneath them (matching the original's horizontal
    rules), and '- ' lines as bullets. Relies on rewrite_cv()'s prompt asking
    the LLM for name / contact / ALL-CAPS headers in that order; falls back
    gracefully (skips the contact-line styling) if the text doesn't include one.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    accent = HexColor(CV_ACCENT_COLOR)
    muted = HexColor("#555555")

    # NOTE: ParagraphStyle inherits `leading` (line height) from its parent
    # unless set explicitly -- base["Normal"] has fontSize=10/leading=12, so
    # any style bumping fontSize well past that (e.g. the 20pt name) MUST set
    # its own leading too, or the next paragraph starts before the glyphs'
    # actual height clears and visibly overlaps it. Every style below sets
    # leading explicitly rather than relying on what it inherits.
    base = getSampleStyleSheet()
    name_style = ParagraphStyle(
        "CVName", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=20, leading=24, textColor=accent, alignment=TA_CENTER, spaceAfter=4,
    )
    contact_style = ParagraphStyle(
        "CVContact", parent=base["Normal"], fontName="Helvetica",
        fontSize=9.5, leading=12, textColor=muted, alignment=TA_CENTER, spaceAfter=14,
    )
    header_style = ParagraphStyle(
        "CVHeader", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=12, leading=15, textColor=accent, spaceBefore=14, spaceAfter=2,
    )
    body_style = ParagraphStyle(
        "CVBody", parent=base["Normal"], fontName="Helvetica",
        fontSize=10, leading=13.5, spaceAfter=3,
    )
    bullet_style = ParagraphStyle("CVBullet", parent=body_style, leftIndent=14)

    doc = SimpleDocTemplate(
        output_path, pagesize=LETTER,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.65 * inch, bottomMargin=0.65 * inch,
    )

    story = []
    found_name = False
    checked_contact_line = False

    for raw_line in cv_text.split("\n"):
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 6))
            continue

        if not found_name:
            story.append(Paragraph(_xml_escape(line), name_style))
            found_name = True
            continue

        if not checked_contact_line:
            checked_contact_line = True
            if not _looks_like_header(line) and not _looks_like_bullet(line):
                story.append(Paragraph(_xml_escape(line), contact_style))
                continue
            # No contact line present (LLM skipped it) -- fall through and
            # process this line normally below instead of dropping it.

        if _looks_like_bullet(line):
            text = re.sub(r"^[-*•–]\s+", "", line)
            story.append(Paragraph(f"&bull;&nbsp;&nbsp;{_xml_escape(text)}", bullet_style))
        elif _looks_like_header(line):
            story.append(Paragraph(_xml_escape(line), header_style))
            story.append(HRFlowable(width="100%", thickness=0.75, color=accent, spaceBefore=1, spaceAfter=6))
        else:
            story.append(Paragraph(_xml_escape(line), body_style))

    if not story:
        story = [Paragraph("(empty CV)", body_style)]

    doc.build(story)
