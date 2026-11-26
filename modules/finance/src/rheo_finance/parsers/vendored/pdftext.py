"""PDF to text via poppler's pdftotext, which keeps the column layout bills depend on."""
import re
import shutil
import subprocess
from pathlib import Path

MIN_TEXT_CHARS = 200


class NotTextPDF(ValueError):
    """The file has no usable text layer (a scan, or not a PDF)."""


def pdf_to_text(path: Path) -> str:
    if shutil.which("pdftotext") is None:
        raise RuntimeError("pdftotext not found; install poppler (brew install poppler / apt install poppler-utils)")
    proc = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise NotTextPDF(f"pdftotext could not read the file: {proc.stderr.strip()[:200]}")
    text = compact(proc.stdout)
    if len(text.strip()) < MIN_TEXT_CHARS:
        raise NotTextPDF("no text layer found; scanned PDFs are out of scope")
    return text


def compact(text: str) -> str:
    """Shrink runs of padding spaces and blank lines. Keeps columns apart, cuts tokens."""
    text = re.sub(r" {4,}", "   ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text
