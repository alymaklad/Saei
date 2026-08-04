"""
CV Rewriter Agent.

Integrity rule: this must reframe real experience for a job description, never
invent skills or history the candidate doesn't have. The system prompt enforces
this explicitly, and missing skills are surfaced separately rather than woven in.
"""
import os
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
            "separately."
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
