"""Vision MCP — converged perception (OCR + documents + structured extraction).

ADR AI-016 + the reliability plan (ai/planning/vision-mcp-reliability-plan.md).
One intent-routed front door over RapidOCR (PaddleOCR PP-OCR on ONNXRuntime,
CPU) with a Tesseract fallback engine, PyMuPDF for documents, and the local Qwen
for schema-structured extraction. Mirrors the pdf-mcp / maps-mcp FastMCP pattern.

Reliability posture (P1a/P1b):
  • Uniform {ok, ...} / {ok:false, error} envelope — never a raw throw.
  • extract DEGRADES GRACEFULLY: if the Qwen structuring step is busy / cold /
    times out, it returns the OCR text with degraded=true rather than failing.
  • OcrEngine fallback: RapidOCR (Latin) with auto-routed Tesseract for
    non-Latin / low-confidence inputs.

`source` everywhere = a file under /data (read-only inbox), an http(s) URL, or a
`data:image/...;base64,...` URI.
"""

import asyncio
import base64
import io
import json
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
import numpy as np
from PIL import Image
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DATA_DIR = "/data"
MAX_CHARS = 120_000
MAX_DIM = int(os.environ.get("VISION_MAX_DIM", "2400"))
QWEN_BASE = os.environ.get("QWEN_BASE", "http://host.docker.internal:1234/v1").rstrip("/")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen/qwen3.6-27b")
# P1a: a generous-but-bounded structuring timeout. Past it, extract degrades to
# OCR-only rather than hanging on a cold-loading / FLUX-held Qwen.
QWEN_TIMEOUT = float(os.environ.get("VISION_QWEN_TIMEOUT", "75"))
DESCRIBE_ENABLED = os.environ.get("VISION_DESCRIBE_ENABLED", "0") == "1"
# Below this RapidOCR mean confidence (Latin inputs) we cross-check Tesseract.
RAPID_CONF_FLOOR = float(os.environ.get("VISION_RAPID_CONF_FLOOR", "0.5"))
PHASE = 1

# Scripts RapidOCR's default (Latin) recogniser is weak on → route to Tesseract.
_NONLATIN = {"ru", "rus", "uk", "be", "bg", "sr", "ar", "ara", "fa", "he",
             "ja", "jpn", "zh", "chi", "chi_sim", "ko", "kor", "th", "hi", "el"}
# advisory lang → Tesseract lang-pack code
_TESS = {"en": "eng", "ru": "rus", "de": "deu", "fr": "fra", "es": "spa",
         "pt": "por", "ar": "ara", "zh": "chi_sim", "ja": "jpn"}

# OCR concurrency control (see ai/planning/vision-mcp-reliability-plan.md + the
# 2026-06-24 CPU-starvation incident). ONNX defaults to ALL cores per inference,
# so N concurrent OCR = N× oversubscription. Two knobs bound it: each OCR uses a
# few intra-op threads, and at most _OCR_CONCURRENCY run at once (rest queue).
# Backstopped by the container's hard `cpus` limit in compose.
_OCR_THREADS = int(os.environ.get("VISION_OCR_THREADS", "2"))
_OCR_CONCURRENCY = int(os.environ.get("VISION_OCR_CONCURRENCY", "3"))
_OCR = None          # lazily-built RapidOCR (loads bundled ONNX models on first use)
_ocr_sem = None      # lazily-built (needs a running loop) concurrency semaphore


def _ocr_engine():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        try:
            _OCR = RapidOCR(intra_op_num_threads=_OCR_THREADS, inter_op_num_threads=1)
        except TypeError:
            _OCR = RapidOCR()   # older API w/o thread kwargs — OMP_NUM_THREADS still bounds it
    return _OCR


def _get_ocr_sem():
    global _ocr_sem
    if _ocr_sem is None:
        _ocr_sem = asyncio.Semaphore(_OCR_CONCURRENCY)
    return _ocr_sem


async def _bounded(fn, *args):
    """Run a blocking OCR job in a thread, gated by the concurrency semaphore — a
    burst of requests QUEUES instead of oversubscribing the CPU."""
    async with _get_ocr_sem():
        return await asyncio.to_thread(fn, *args)


@asynccontextmanager
async def _lifespan(server: FastMCP):
    logger.info("Vision MCP starting (phase=%s, describe=%s, qwen=%s, t/o=%ss)",
                PHASE, DESCRIBE_ENABLED, QWEN_BASE, QWEN_TIMEOUT)
    yield
    logger.info("Vision MCP shutting down")


mcp = FastMCP(
    "Vision",
    lifespan=_lifespan,
    instructions=(
        "Converged perception — one tool for reading images and documents. "
        "read_text: OCR an image to plain text. extract: OCR + layout, then "
        "structure into a JSON schema (receipts, rosters, invoices, tables) — "
        "degrades to OCR-only if the structuring model is busy. read_document: "
        "PDF/EPUB → markdown (scanned pages auto-OCR'd). describe: visual "
        "reasoning (Phase 2, disabled). Every `source` is a file under /data, an "
        "http(s) URL, or a data:image/...;base64,... URI."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*",
                       "host.docker.internal:*", "vision-mcp:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                         "http://host.docker.internal:*", "http://vision-mcp:*"],
    ),
)


# ── source resolution ────────────────────────────────────────────────────────

def _load_bytes(source: str) -> bytes:
    s = (source or "").strip()
    if not s:
        raise ValueError("source is required")
    if s.startswith("data:"):
        b64 = s.split(",", 1)[1] if "," in s else s
        return base64.b64decode(b64)
    if s.lower().startswith(("http://", "https://")):
        with httpx.Client(follow_redirects=True, timeout=60) as c:
            r = c.get(s)
            r.raise_for_status()
            return r.content
    cand = s if os.path.isabs(s) else os.path.join(DATA_DIR, s)
    if not os.path.exists(cand):
        alt = os.path.join(DATA_DIR, os.path.basename(s))
        if os.path.exists(alt):
            cand = alt
        else:
            raise ValueError(f"file not found: {s} (looked under {DATA_DIR})")
    with open(cand, "rb") as f:
        return f.read()


def _load_image(source: str):
    img = Image.open(io.BytesIO(_load_bytes(source))).convert("RGB")
    w, h = img.size
    scale = 1.0
    if max(w, h) > MAX_DIM:
        scale = MAX_DIM / max(w, h)
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    return np.asarray(img), {"w": w, "h": h, "scale": round(scale, 3)}


# ── OCR engines (P1b: RapidOCR primary + Tesseract fallback) ─────────────────

def _rapidocr_lines(arr):
    result, _ = _ocr_engine()(arr)
    lines = []
    for row in (result or []):
        box, text, score = row[0], row[1], row[2]
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        lines.append({"text": text,
                      "bbox": [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))],
                      "conf": round(float(score), 3)})
    return lines


def _tess_lang(lang: str) -> str:
    parts = (lang or "en").lower().replace("+", " ").split()
    return "+".join(_TESS.get(p, p) for p in parts) or "eng"


def _tesseract_lines(arr, lang):
    import pytesseract
    from pytesseract import Output
    d = pytesseract.image_to_data(arr, lang=_tess_lang(lang), output_type=Output.DICT)
    lines = []
    for i in range(len(d["text"])):
        txt = (d["text"][i] or "").strip()
        try:
            conf = float(d["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if txt and conf >= 0:
            x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
            lines.append({"text": txt, "bbox": [x, y, x + w, y + h], "conf": round(conf / 100, 3)})
    return lines


def _is_nonlatin(lang: str) -> bool:
    return any(t in _NONLATIN for t in (lang or "").lower().replace("+", " ").split())


def _run_ocr(source: str, lang: str = "en", engine: str = "auto"):
    """Resolve → OCR. Returns (lines, meta, engine_used). `engine`: auto |
    rapidocr | tesseract. Auto routes non-Latin / low-confidence to Tesseract."""
    arr, meta = _load_image(source)
    eng = (engine or "auto").lower()
    if eng == "tesseract":
        return _tesseract_lines(arr, lang), meta, "tesseract"
    if eng == "rapidocr":
        return _rapidocr_lines(arr), meta, "rapidocr"
    # auto
    if _is_nonlatin(lang):
        return _tesseract_lines(arr, lang), meta, "tesseract"
    lines = _rapidocr_lines(arr)
    mean = (sum(l["conf"] for l in lines) / len(lines)) if lines else 0.0
    if not lines or mean < RAPID_CONF_FLOOR:
        try:
            t = _tesseract_lines(arr, lang)
            if sum(len(l["text"]) for l in t) > sum(len(l["text"]) for l in lines):
                return t, meta, "tesseract(fallback)"
        except Exception as e:           # tesseract missing/failure → keep RapidOCR
            logger.warning("tesseract fallback failed: %s", e)
    return lines, meta, "rapidocr"


# ── JSON / schema helpers + the structuring call ─────────────────────────────

def _extract_json(s: str):
    s = (s or "").strip()
    if s.startswith("```"):
        inner = s.split("```", 2)
        s = inner[1] if len(inner) > 1 else s
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
        s = s.strip().strip("`").strip()
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b != -1 and b > a:
        s = s[a:b + 1]
    return json.loads(s)


def _schema_ok(data, schema) -> bool:
    if not isinstance(data, dict):
        return False
    if not isinstance(schema, dict):
        return True
    req = schema.get("required")
    if isinstance(req, list):
        return all(k in data for k in req)
    return True


async def _structure(layout_text: str, schema, instruction: str):
    """OCR text → JSON via the local Qwen. temp 0, JSON-only. One connect-retry
    (Qwen mid-reload); a read timeout raises → caller degrades. Returns (data, model)."""
    sys = (
        "You extract structured data from OCR'd text. Each line is prefixed with "
        "its [x,y] pixel position so you can reconstruct columns and tables. "
        "Return ONLY a single JSON object — no prose, no markdown fences."
    )
    parts = []
    if instruction:
        parts.append("Task: " + instruction)
    if schema:
        parts.append("Return JSON matching this schema:\n" + json.dumps(schema))
    else:
        parts.append("Return a JSON object with the key fields; render any table "
                     "as an array of row objects.")
    parts.append("OCR text:\n" + layout_text[:MAX_CHARS])
    body = {"model": QWEN_MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": sys},
                         {"role": "user", "content": "\n\n".join(parts)}]}
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=QWEN_TIMEOUT) as c:
                r = await c.post(f"{QWEN_BASE}/chat/completions", json=body)
                r.raise_for_status()
                j = r.json()
            content = j["choices"][0]["message"]["content"]
            return _extract_json(content), j.get("model", QWEN_MODEL)
        except httpx.ConnectError:
            if attempt == 2:
                raise
            await asyncio.sleep(1.0)


# ── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
async def read_text(source: str, lang: str = "en", detail: bool = False,
                    engine: str = "auto") -> dict:
    """
    OCR an image to plain text. CPU, sub-second on a typical screenshot.

    source : file under /data, an http(s) URL, or a data:image/...;base64,... URI.
    lang   : advisory — drives engine routing. RapidOCR (Latin) is the default;
             non-Latin (ru, ar, ja, zh, …) auto-routes to Tesseract.
    detail : include per-line boxes + confidence.
    engine : "auto" (default) | "rapidocr" | "tesseract".
    """
    t0 = time.time()
    try:
        lines, meta, eng = await _bounded(_run_ocr, source, lang, engine)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    text = "\n".join(l["text"] for l in lines)
    out = {"ok": True, "text": text[:MAX_CHARS], "engine": eng, "lang": lang,
           "ms": int((time.time() - t0) * 1000), "image": meta}
    if detail:
        out["lines"] = lines
    return out


@mcp.tool()
async def extract(source: str, schema: dict | None = None, instruction: str = "",
                  lang: str = "en", engine: str = "auto") -> dict:
    """
    Structured extraction: OCR + layout, then the local Qwen structures it into
    your schema. The receipt / roster / invoice / table → JSON path.

    DEGRADES GRACEFULLY: if the structuring model is busy / cold / times out, the
    result comes back with degraded=true and structured=false plus the OCR
    raw_text — it never just fails when the OCR succeeded.

    source      : image under /data, an http(s) URL, or a data: URI.
    schema      : JSON Schema (or a field map). null = best-effort key fields + tables.
    instruction : optional natural-language hint.
    lang/engine : see read_text.
    """
    t0 = time.time()
    try:
        lines, meta, eng = await _bounded(_run_ocr, source, lang, engine)
    except Exception as e:
        return {"ok": False, "error": "ocr failed: " + str(e)[:160]}
    raw_text = "\n".join(l["text"] for l in lines)
    if not lines:
        return {"ok": True, "data": None, "structured": False, "degraded": True,
                "reason": "no text detected in image", "raw_text": "",
                "engine": eng, "ms": int((time.time() - t0) * 1000)}
    layout = "\n".join(f"[{l['bbox'][0]},{l['bbox'][1]}] {l['text']}" for l in lines)
    try:
        data, model = await _structure(layout, schema, instruction)
    except Exception as e:
        # P1a — Qwen unavailable/busy/timeout → OCR-only, still ok:true.
        return {"ok": True, "data": None, "structured": False, "degraded": True,
                "reason": "structuring unavailable: " + str(e)[:120],
                "raw_text": raw_text[:MAX_CHARS], "engine": eng,
                "ms": int((time.time() - t0) * 1000), "image": meta}
    return {"ok": True, "data": data, "structured": True, "degraded": False,
            "raw_text": raw_text[:MAX_CHARS], "schema_valid": _schema_ok(data, schema),
            "model": model, "engine": eng, "ms": int((time.time() - t0) * 1000),
            "image": meta}


def _parse_pages(spec: str, n: int):
    spec = (spec or "").strip()
    if not spec:
        return None
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a) - 1, int(b)))
        else:
            out.append(int(part) - 1)
    out = sorted({i for i in out if 0 <= i < n})
    return out or None


def _read_document_blocking(source: str, ocr: str, pages: str) -> dict:
    # Plain PyMuPDF text per page + our OWN RapidOCR for scanned/empty pages.
    # (We deliberately don't use pymupdf4llm's auto-OCR — its bundled RapidOCR
    # integration is version-incompatible and crashes; this path is dependable
    # regardless. Markdown-table structure via pymupdf4llm is a future option
    # once that dependency is pinned.)
    import fitz
    raw = _load_bytes(source)
    is_pdf = source.lower().split("?")[0].endswith(".pdf") or raw[:5] == b"%PDF-"
    doc = fitz.open(stream=raw, filetype="pdf") if is_pdf else fitz.open(stream=raw)
    try:
        sel = _parse_pages(pages, doc.page_count)
        idxs = sel if sel is not None else list(range(doc.page_count))
        force = ocr.strip().lower() in ("on", "force", "true", "yes")
        off = ocr.strip().lower() in ("off", "no", "false")
        out, any_ocr = [], False
        for i in idxs:
            page = doc[i]
            txt = "" if force else page.get_text("text").strip()
            if (not txt) and not off:           # scanned / empty page → RapidOCR
                png = page.get_pixmap(dpi=200).tobytes("png")
                arr = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
                txt = "\n".join(l["text"] for l in _rapidocr_lines(arr))
                any_ocr = True
            out.append(txt)
        md = "\n\n".join(out)
        return {"ok": True, "markdown": md[:MAX_CHARS], "pages": doc.page_count,
                "pages_read": len(idxs), "ocr_used": any_ocr,
                "truncated": len(md) > MAX_CHARS,
                "engine": "rapidocr" if any_ocr else "pymupdf"}
    finally:
        doc.close()


@mcp.tool()
async def read_document(source: str, ocr: str = "auto", pages: str = "") -> dict:
    """
    PDF/EPUB → LLM-ready markdown. Digital pages via PyMuPDF4LLM; scanned pages
    fall through to RapidOCR.

    source : document under /data, or an http(s) URL.
    ocr    : "auto" (detect scanned), "on" (force), or "off".
    pages  : optional 1-based selection like "1-3,5" (empty = all).
    """
    try:
        return await _bounded(_read_document_blocking, source, ocr, pages)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@mcp.tool()
async def describe(source: str, question: str = "") -> dict:
    """
    Visual reasoning over a non-text image (caption / VQA) via a swap-in VLM.
    **Phase 2 — disabled by default** (AI-016). For text use read_text / extract.
    """
    if not DESCRIBE_ENABLED:
        return {"ok": False, "phase": 2,
                "error": "describe is Phase 2 and disabled — use read_text/extract "
                         "for text-bearing images"}
    return {"ok": False, "error": "describe backend not yet wired"}


@mcp.tool()
async def capabilities() -> dict:
    """What this vision tool can currently do — so an agent can discover its sight."""
    return {
        "ok": True, "phase": PHASE,
        "tools": ["read_text", "extract", "read_document", "capabilities"]
        + (["describe"] if DESCRIBE_ENABLED else []),
        "describe_enabled": DESCRIBE_ENABLED,
        "ocr_engines": ["rapidocr", "tesseract"],
        "ocr_default": "auto (rapidocr Latin; tesseract for non-Latin / low-conf)",
        "ocr_concurrency": _OCR_CONCURRENCY,
        "ocr_threads_per_run": _OCR_THREADS,
        "structure_model": QWEN_MODEL,
        "structure_degrades": True,
        "langs_strong": ["en", "de", "fr", "es", "pt"],
        "langs_via_tesseract": list(_TESS.keys()),
        "note": "extract degrades to OCR-only when the Qwen structuring step is unavailable.",
    }


# ── Health + ASGI app ────────────────────────────────────────────────────────

async def _health(request):
    return JSONResponse({"status": "ok", "service": "vision-mcp", "phase": PHASE})


async def _selftest(request):
    """Functional probe — actually OCRs a known in-memory image and confirms the
    text comes back. Catches "green on /health but the OCR engine is broken"
    (the Doctor `vision.functional` check consumes this). CPU, ~sub-second once
    the models are loaded."""
    t0 = time.time()
    try:
        from PIL import ImageDraw
        img = Image.new("RGB", (360, 120), "white")
        d = ImageDraw.Draw(img)
        d.text((14, 18), "SELFTEST 12345", fill="black")
        d.text((14, 64), "vision ocr ok", fill="black")
        lines = _rapidocr_lines(np.asarray(img))
        joined = " ".join(l["text"] for l in lines)
        low = joined.lower().replace(" ", "")
        ocr_ok = ("12345" in low) or ("selftest" in low)
        return JSONResponse({"ok": bool(ocr_ok), "service": "vision-mcp", "ocr_ok": bool(ocr_ok),
                             "ocr_lines": len(lines), "text": joined[:120], "engine": "rapidocr",
                             "describe_enabled": DESCRIBE_ENABLED,
                             "ms": int((time.time() - t0) * 1000)})
    except Exception as e:
        return JSONResponse({"ok": False, "service": "vision-mcp", "ocr_ok": False,
                             "error": str(e)[:200]}, status_code=500)


app = mcp.streamable_http_app()
_Route = __import__("starlette.routing", fromlist=["Route"]).Route
app.router.routes.insert(0, _Route("/health", _health, methods=["GET"]))
app.router.routes.insert(0, _Route("/selftest", _selftest, methods=["GET"]))
