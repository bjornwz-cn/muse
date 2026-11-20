"""0.1.3：PDF OCR 加固——默认地址自动探测、OCR 不可用时静默降级不抛错。

容器内 127.0.0.1 指向容器自身，ocr-service 在宿主 docker 网桥上，
所以 127.0.0.1:8866 在容器里永远不通；探测不到就用宿主网关地址
172.17.0.1:8866。GET /ocr 返回 405（只接受 POST）也算可达。
"""
import urllib.error
import urllib.request

import pytest

from curator_core import textkit
from curator_core.textkit import extract_text, ocr_file, resolve_ocr_url

# 最小无文字层 PDF（无任何文本对象）：pdftotext 读不到文字 → 走 OCR 分支
MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
    b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj\n"
    b"xref\n0 4\n0000000000 65535 f\n0000000009 00000 n\n"
    b"0000000058 00000 n\n0000000115 00000 n\n"
    b"trailer << /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF\n"
)


@pytest.fixture(autouse=True)
def _reset_cache():
    textkit._OCR_URL_CACHE["url"] = None
    yield
    textkit._OCR_URL_CACHE["url"] = None


def test_resolve_ocr_url_env_override(monkeypatch):
    monkeypatch.setenv("OCR_URL", "http://10.0.0.9:8866")
    assert resolve_ocr_url() == "http://10.0.0.9:8866"


def test_resolve_ocr_url_probe_gateway(monkeypatch):
    """127.0.0.1 拒绝连接、网关 GET /ocr 返回 405 → 选网关地址。"""
    monkeypatch.delenv("OCR_URL", raising=False)

    def fake_urlopen(req, timeout=None):
        if "127.0.0.1" in req.full_url:
            raise urllib.error.URLError("Connection refused")
        raise urllib.error.HTTPError(req.full_url, 405, "Method Not Allowed", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert resolve_ocr_url() == "http://172.17.0.1:8866"


def test_resolve_ocr_url_both_down_defaults_gateway(monkeypatch):
    """两边都不通时默认网关地址（容器内正确地址），不抛错。"""
    monkeypatch.delenv("OCR_URL", raising=False)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert resolve_ocr_url() == "http://172.17.0.1:8866"


def test_ocr_file_unreachable_returns_empty(tmp_path):
    """OCR 服务不通时返回空文本，不抛错。"""
    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert ocr_file(str(png), ocr_url="http://127.0.0.1:9", timeout=2) == ""


def test_extract_text_scanned_pdf_no_ocr(tmp_path):
    """无文字层 PDF + OCR 不通 → 文本空、来源 scanned（不抛错、不误标 pdf）。"""
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(MINIMAL_PDF)
    text, src = extract_text(str(pdf), ocr_url="http://127.0.0.1:9", ocr_timeout=2)
    assert text == "" and src == "scanned"


def test_extract_text_plain_text(tmp_path):
    """纯文本文件不受 OCR 影响。"""
    f = tmp_path / "note.txt"
    f.write_text("hello world", encoding="utf-8")
    text, src = extract_text(str(f))
    assert text == "hello world" and src == "text"
