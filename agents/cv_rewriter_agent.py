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
            "into a PDF directly:\n"
            "- First line: the candidate's name.\n"
            "- Section headers in ALL CAPS on their own line (e.g. SUMMARY, EXPERIENCE, "
            "EDUCATION, SKILLS) -- use whichever sections the original CV actually has.\n"
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


def save_cv_as_pdf(cv_text: str, output_path: str):
    """
    Renders CV text into an actual formatted PDF -- name as a title, ALL-CAPS
    section headers as headers, '- ' lines as bullets, everything else as
    body paragraphs. Relies on rewrite_cv()'s prompt asking the LLM for this
    exact plain-text structure; falls back gracefully (just body paragraphs)
    if the text doesn't follow it.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    base = getSampleStyleSheet()
    title_style = ParagraphStyle("CVTitle", parent=base["Heading1"], fontSize=17, spaceAfter=10)
    header_style = ParagraphStyle("CVHeader", parent=base["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=4)
    body_style = ParagraphStyle("CVBody", parent=base["Normal"], fontSize=10.5, leading=14, spaceAfter=3)
    bullet_style = ParagraphStyle("CVBullet", parent=body_style, leftIndent=14)

    doc = SimpleDocTemplate(
        output_path, pagesize=LETTER,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
    )

    story = []
    title_written = False
    for raw_line in cv_text.split("\n"):
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 6))
            continue

        if not title_written:
            story.append(Paragraph(_xml_escape(line), title_style))
            title_written = True
        elif _looks_like_bullet(line):
            text = re.sub(r"^[-*•–]\s+", "", line)
            story.append(Paragraph(f"&bull;&nbsp;&nbsp;{_xml_escape(text)}", bullet_style))
        elif _looks_like_header(line):
            story.append(Paragraph(_xml_escape(line), header_style))
        else:
            story.append(Paragraph(_xml_escape(line), body_style))

    if not story:
        story = [Paragraph("(empty CV)", body_style)]

    doc.build(story)
