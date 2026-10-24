import io
import json
import shutil
import zipfile

import pytest
from sqlalchemy import func, select

from app import cv_import
from app.cv_import import CVImportError, detect_kind, extract_text, parse_fields
from app.models import UserContextVersion
from tests.conftest import AUTH

CV_LINES = [
    "Ilyass Example - Computer Engineering Student, ENSIAS Rabat",
    "Projects: Go rate limiter for the student club API; Kubernetes operator for lab clusters",
    "Experience: Backend intern, Acme Payments, Summer 2025 - built a reconciliation service in Python",
    "Skills: Go, Python, PostgreSQL, Docker, Kubernetes",
    "Looking for a 6-month end-of-studies internship from February 2027",
]

LLM_FIELDS = {
    "background": "Education\nENSIAS Rabat, computer engineering\nProjects\nGo rate limiter – student club API – Go",
    "target_role": "Backend internship",
    "target_stack": "Go, Python, Kubernetes",
    "availability": "6 months from February 2027",
    "location": "Rabat",
    "notes": "First name for sign-off: Ilyass",
}


def make_pdf(lines: list[str], size: int = 12) -> bytes:
    """A minimal one-page PDF with a real text layer (Helvetica)."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content = f"BT /F1 {size} Tf 50 740 Td {int(size * 1.6)} TL " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p.replace('&', '&amp;')}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


# --- extraction -------------------------------------------------------------------


def test_detect_kind():
    assert detect_kind(make_pdf(["x"])) == "pdf"
    assert detect_kind(make_docx(["x"]), "cv.docx") == "docx"
    assert detect_kind(b"\x89PNG\r\n\x1a\n....", "cv.png") == "image"
    assert detect_kind(b"\xff\xd8\xff\xe0....", "scan.jpg") == "image"
    assert detect_kind("Mon CV".encode(), "cv.txt") == "text"
    with pytest.raises(CVImportError) as e:
        detect_kind(b"\xd0\xcf\x11\xe0 old word", "cv.doc")
    assert e.value.status == 415 and ".docx" in e.value.message
    with pytest.raises(CVImportError):
        detect_kind(b"\x00\x01binary", "cv.bin")


def test_pdf_text_layer_is_used_without_ocr(monkeypatch):
    def no_ocr(*a, **k):
        raise AssertionError("OCR should not run for a PDF with a text layer")

    monkeypatch.setattr(cv_import, "ocr_image", no_ocr)
    ex = extract_text(make_pdf(CV_LINES), "cv.pdf", "eng")
    assert ex.method == "pdf-text" and ex.ocr_pages == 0
    assert "Go rate limiter" in ex.text and "ENSIAS" in ex.text


def test_scanned_pdf_falls_back_to_ocr(monkeypatch):
    rendered = []
    monkeypatch.setattr(cv_import, "render_pdf_pages", lambda data, pages: rendered.append(pages) or [b"png1"])
    monkeypatch.setattr(cv_import, "ocr_image", lambda img, langs: "\n".join(CV_LINES) + f"\n(langs={langs})")
    ex = extract_text(make_pdf([]), "scan.pdf", "eng+fra")
    assert ex.method == "pdf-ocr" and ex.ocr_pages == 1 and rendered == [1]
    assert "Kubernetes operator" in ex.text and "langs=eng+fra" in ex.text


def test_image_is_ocrd(monkeypatch):
    monkeypatch.setattr(cv_import, "ocr_image", lambda img, langs: "  ".join(CV_LINES))
    ex = extract_text(b"\x89PNG\r\n\x1a\nfake", "photo.png", "eng")
    assert ex.method == "image-ocr" and ex.ocr_pages == 1 and "ENSIAS" in ex.text


def test_docx_and_text():
    ex = extract_text(make_docx(CV_LINES + ["R&D club"]), "cv.docx", "eng")
    assert ex.method == "docx"
    assert ex.text.splitlines()[0] == CV_LINES[0] and "R&D club" in ex.text
    ex = extract_text("\r\n".join(CV_LINES).encode(), "cv.txt", "eng")
    assert ex.method == "text" and "\r" not in ex.text


def test_extraction_errors(monkeypatch):
    with pytest.raises(CVImportError) as e:
        extract_text(b"", "cv.pdf", "eng")
    assert e.value.status == 400
    with pytest.raises(CVImportError) as e:
        extract_text(b"%PDF" + b"0" * (cv_import.MAX_UPLOAD_BYTES + 1), "big.pdf", "eng")
    assert e.value.status == 413
    with pytest.raises(CVImportError) as e:
        extract_text(b"hi", "cv.txt", "eng")
    assert e.value.status == 422 and e.value.code == "no_text"


def test_missing_tesseract_is_explained(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("tesseract")

    monkeypatch.setattr(cv_import.subprocess, "run", missing)
    with pytest.raises(CVImportError) as e:
        cv_import.ocr_image(b"img", "eng")
    assert e.value.code == "ocr_unavailable" and "tesseract" in e.value.message


@pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftoppm")), reason="needs tesseract + poppler")
def test_real_ocr_roundtrip():
    """Rasterize a text PDF with pdftoppm, then OCR the image: the same path a scanned CV takes."""
    lines = ["Computer Engineering Student", "Kubernetes operator project", "Backend internship"]
    [png] = cv_import.render_pdf_pages(make_pdf(lines, size=20), 1)
    text = cv_import.ocr_image(png, "eng+fra")
    for word in ["Engineering", "Kubernetes", "internship"]:
        assert word in text


# --- structuring --------------------------------------------------------------------


def test_parse_fields_tolerates_fences_and_lists():
    raw = "```json\n" + json.dumps({**LLM_FIELDS, "background": ["Line 1", "Line 2"], "extra": "ignored"}) + "\n```"
    fields = parse_fields(raw)
    assert fields["background"] == "Line 1\nLine 2"
    assert set(fields) == {"background", "target_role", "target_stack", "availability", "location", "notes"}
    assert parse_fields('Sure: {"background": "x", "location": null}') == {
        "background": "x", "target_role": "", "target_stack": "", "availability": "", "location": "", "notes": "",
    }
    for bad in ["no json here", "{not: valid}", "[1, 2]"]:
        with pytest.raises(CVImportError) as e:
            parse_fields(bad)
        assert e.value.status == 502


# --- endpoints -------------------------------------------------------------------------


def test_api_import_fills_fields_without_saving(client, llm, db):
    llm.queue(json.dumps(LLM_FIELDS))
    before = db.scalar(select(func.count(UserContextVersion.id)))
    r = client.post("/api/context/import", files={"file": ("cv.pdf", make_pdf(CV_LINES), "application/pdf")}, headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["fields"] == LLM_FIELDS
    assert data["method"] == "pdf-text" and "ENSIAS" in data["text"]
    # Nothing saved: the user reviews first.
    assert db.scalar(select(func.count(UserContextVersion.id))) == before

    system, user = llm.calls[0]
    assert "Never invent" in system["content"] and "contact details" in system["content"]
    assert user["content"].startswith("<cv>") and "Go rate limiter" in user["content"]


def test_api_import_errors_are_clear(client, llm):
    r = client.post("/api/context/import", files={"file": ("cv.doc", b"\xd0\xcf\x11\xe0 old", "application/msword")}, headers=AUTH)
    assert r.status_code == 415 and ".docx" in r.json()["detail"]["message"]
    llm.queue("I can't do that")
    r = client.post("/api/context/import", files={"file": ("cv.txt", "\n".join(CV_LINES).encode(), "text/plain")}, headers=AUTH)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "llm_bad_json"
    assert client.post("/api/context/import", files={"file": ("cv.txt", b"x", "text/plain")}).status_code == 401


def test_dashboard_import(client, llm, dash):
    llm.queue(json.dumps(LLM_FIELDS))
    r = dash.post("/context/import", files={"file": ("cv.docx", make_docx(CV_LINES), "application/octet-stream")})
    assert r.status_code == 200 and r.json()["fields"]["location"] == "Rabat"
    page = dash.get("/context")
    assert "Fill from your CV" in page.text


def test_dashboard_import_requires_login(client):
    r = client.post("/context/import", files={"file": ("cv.txt", b"x", "text/plain")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
