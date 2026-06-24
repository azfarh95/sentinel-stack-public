"""PDF MCP server — high-quality PDF → Markdown for LLMs via PyMuPDF4LLM, plus
Tesseract OCR for scanned documents.

Mirrors the maps-mcp FastMCP pattern (streamable_http_app + /health) so the
MetaMCP aggregator and the Infer Bridge consume it exactly like the other
*-mcp services. Accepts a local file under /data (read-only mount) or an
http(s) URL.
"""

import logging
import os
from contextlib import asynccontextmanager

import httpx
import fitz  # PyMuPDF
import pymupdf4llm
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DATA_DIR = "/data"
# Cap markdown/text handed back to the LLM so a 400-page PDF can't blow the
# context window. The cap is surfaced (truncated=True) — never silent.
MAX_CHARS = 120_000


@asynccontextmanager
async def _lifespan(server: FastMCP):
    logger.info("PDF MCP server starting (PyMuPDF %s)", fitz.VersionBind)
    yield
    logger.info("PDF MCP server shutting down")


mcp = FastMCP(
    "PDF",
    lifespan=_lifespan,
    instructions=(
        "Convert PDF (and EPUB/XPS/MOBI/CBZ) documents to clean, LLM-ready Markdown "
        "using PyMuPDF4LLM — preserves headings, lists, tables and multi-column "
        "reading order. Workflow: call pdf_info first to see the page count and which "
        "pages are scanned (no embedded text); use pdf_to_markdown for digital pages "
        "and pdf_ocr for scanned/image-only pages. The `path` is either a local file "
        "under /data (filename or absolute path) or an http(s) URL."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*",
                       "host.docker.internal:*", "pdf-mcp:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                         "http://host.docker.internal:*", "http://pdf-mcp:*"],
    ),
)


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _open(path: str) -> fitz.Document:
    """Open a document from an http(s) URL or a local path (absolute, or relative
    to /data). Raises ValueError with a clear message on failure."""
    p = (path or "").strip()
    if not p:
        raise ValueError("path is required")
    if p.lower().startswith(("http://", "https://")):
        with httpx.Client(follow_redirects=True, timeout=60) as c:
            r = c.get(p)
            r.raise_for_status()
        return fitz.open(stream=r.content, filetype="pdf")
    cand = p if os.path.isabs(p) else os.path.join(DATA_DIR, p)
    if not os.path.exists(cand):
        alt = os.path.join(DATA_DIR, os.path.basename(p))
        if os.path.exists(alt):
            cand = alt
        else:
            raise ValueError(f"file not found: {p} (looked under {DATA_DIR})")
    return fitz.open(cand)


def _parse_pages(spec: str, n: int):
    """'1-3,5' (1-based, human) -> [0,1,2,4] (0-based). Empty -> None (all)."""
    spec = (spec or "").strip()
    if not spec:
        return None
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            for i in range(int(a), int(b) + 1):
                out.append(i - 1)
        else:
            out.append(int(part) - 1)
    out = sorted({i for i in out if 0 <= i < n})
    return out or None


def _cap(text: str):
    if len(text) <= MAX_CHARS:
        return text, False
    return text[:MAX_CHARS], True


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def pdf_info(path: str) -> dict:
    """
    Inspect a document before extraction. Returns the page count, metadata, and a
    per-page text-coverage map so you can tell digital pages (use pdf_to_markdown)
    apart from scanned / image-only pages (use pdf_ocr).

    path : local file under /data (filename or absolute path) or an http(s) URL.
    """
    try:
        doc = _open(path)
    except Exception as e:
        return {"error": str(e)}
    try:
        pages, scanned = [], []
        for i in range(doc.page_count):
            chars = len(doc[i].get_text().strip())
            has = chars >= 20
            pages.append({"page": i + 1, "has_text": has, "chars": chars})
            if not has:
                scanned.append(i + 1)
        return {
            "source": path,
            "page_count": doc.page_count,
            "metadata": {k: v for k, v in (doc.metadata or {}).items() if v},
            "scanned_pages": scanned,
            "pages": pages,
            "hint": ("all pages have embedded text — use pdf_to_markdown"
                     if not scanned else
                     f"{len(scanned)} page(s) look scanned — use pdf_ocr for those"),
        }
    finally:
        doc.close()


@mcp.tool()
async def pdf_to_markdown(path: str, pages: str = "", page_chunks: bool = False) -> dict:
    """
    Convert a PDF (or EPUB/XPS/MOBI/CBZ) to clean, LLM-ready Markdown via
    PyMuPDF4LLM. Preserves headings, lists, tables and multi-column reading order.
    Best for digital (text-based) documents; for scanned pages use pdf_ocr.

    path        : local file under /data (filename or absolute path) or an http(s) URL.
    pages       : optional 1-based selection like "1-3,5" (empty = all pages).
    page_chunks : if true, return one entry per page ([{page, markdown}]) instead of
                  a single joined string — handy for page-cited RAG.

    Long output is capped (~120k chars, surfaced as truncated=true); narrow with
    `pages` for full detail on a section.
    """
    try:
        doc = _open(path)
    except Exception as e:
        return {"error": str(e)}
    try:
        sel = _parse_pages(pages, doc.page_count)
        md = pymupdf4llm.to_markdown(
            doc, pages=sel, page_chunks=page_chunks, show_progress=False,
        )
        if page_chunks:
            chunks, total = [], 0
            for idx, ch in enumerate(md):
                txt = ch.get("text", "") if isinstance(ch, dict) else str(ch)
                txt, _ = _cap(txt)
                total += len(txt)
                pno = (sel[idx] if sel else idx) + 1
                chunks.append({"page": pno, "markdown": txt})
            return {"source": path, "pages": len(chunks), "chunks": chunks, "chars": total}
        text, trunc = _cap(md)
        return {
            "source": path,
            "pages": len(sel) if sel else doc.page_count,
            "markdown": text,
            "truncated": trunc,
            "chars": len(text),
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        doc.close()


# ── OCR engines ──────────────────────────────────────────────────────────────
# Two tiers:
#   tesseract (default) — light, multilingual (incl. rus/Cyrillic via packs),
#                         best on clean high-DPI scans.
#   rapidocr  (upgrade) — PaddleOCR PP-OCR models on ONNXRuntime; stronger on
#                         poor scans / rotation / dense layout. Latin + Chinese
#                         (its bundled models); ignores `language`.

_RAPID = None  # lazily-built RapidOCR (heavy: loads ONNX models on first use)


def _rapid_engine():
    global _RAPID
    if _RAPID is None:
        from rapidocr_onnxruntime import RapidOCR
        _RAPID = RapidOCR()
    return _RAPID


def _ocr_page_rapid(page, dpi: int) -> str:
    """Render a PDF page to PNG and OCR it with RapidOCR. Returns joined text."""
    png = page.get_pixmap(dpi=dpi).tobytes("png")
    result, _ = _rapid_engine()(png)
    if not result:
        return ""
    # result rows are [box, text, score] — keep the text in reading order.
    return "\n".join(row[1] for row in result).strip()


@mcp.tool()
async def pdf_ocr(path: str, pages: str = "", language: str = "eng",
                  dpi: int = 200, engine: str = "tesseract") -> dict:
    """
    OCR scanned / image-only PDF pages. Use this for pages that pdf_info reports
    as scanned (no embedded text). Returns extracted plain text per page.

    path     : local file under /data or an http(s) URL.
    pages    : optional 1-based selection like "1-3,5" (empty = all pages).
    language : Tesseract language(s), e.g. "eng", "eng+rus", "eng+deu". Default
               "eng". (Ignored when engine="rapidocr".) Installed packs: eng, rus,
               deu, fra, spa, por, ara, chi_sim, jpn.
    dpi      : render resolution for OCR (higher = more accurate, slower). Default 200.
    engine   : "tesseract" (default — light, multilingual incl. Cyrillic) or
               "rapidocr" (upgrade — PP-OCR/ONNX, better on poor scans, rotation
               and dense layout; Latin + Chinese).
    """
    try:
        doc = _open(path)
    except Exception as e:
        return {"error": str(e)}
    use_rapid = engine.strip().lower() in ("rapidocr", "rapid", "paddle", "pro")
    try:
        sel = _parse_pages(pages, doc.page_count)
        idxs = sel if sel is not None else list(range(doc.page_count))
        results, total = [], 0
        for i in idxs:
            page = doc[i]
            try:
                if use_rapid:
                    txt = _ocr_page_rapid(page, dpi)
                else:
                    tp = page.get_textpage_ocr(flags=0, language=language, dpi=dpi, full=True)
                    txt = page.get_text(textpage=tp).strip()
            except Exception as e:
                txt = ""
                logger.warning("OCR failed on page %d (%s): %s", i + 1, engine, e)
            total += len(txt)
            results.append({"page": i + 1, "text": txt})
        return {
            "source": path,
            "engine": "rapidocr" if use_rapid else "tesseract",
            "language": None if use_rapid else language,
            "dpi": dpi,
            "pages": len(results),
            "results": results,
            "chars": total,
            "truncated": total > MAX_CHARS,
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        doc.close()


# ── Health ─────────────────────────────────────────────────────────────────────

async def _health(request):
    return JSONResponse({"status": "ok", "service": "pdf-mcp"})


# ── Build ASGI app ────────────────────────────────────────────────────────────

app = mcp.streamable_http_app()
app.router.routes.insert(
    0,
    __import__("starlette.routing", fromlist=["Route"]).Route("/health", _health, methods=["GET"]),
)
