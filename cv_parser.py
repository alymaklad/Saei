"""Extract plain text from a CV, whether it's a PDF or a Word doc."""
import os


def parse_cv(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext in (".docx", ".dotx"):
        return _parse_docx(path)
    raise ValueError(f"Unsupported CV format: {ext} (use .pdf or .docx)")


def _parse_pdf(path: str) -> str:
    import pdfplumber
    text_parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
    return "\n".join(text_parts)


def _parse_docx(path: str) -> str:
    from docx import Document
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs)
