"""Universal first-step normalizer.

Every document dropped in _INBOX (PDF / JPG / PNG / TIFF / HEIC) emerges from
this module as a canonical word-list JSON cached at /data/ocr_cache/<hash>.ocr.json.
Downstream classifier + parsers consume that JSON — they never call pdfplumber
or tesseract directly anymore.

Decisions:
  • Cache location: centralized at /data/ocr_cache/ (hash-keyed; survives renames)
  • Cache invalidation: source-mtime > recorded-mtime → re-OCR
  • OCR engine: tesseract via pytesseract
  • Languages: eng + chi_sim (Chinese for merchant names)
  • Failure: write sentinel .failed.json, don't retry until source changes

Output schema (the contract for downstream parsers):
{
  "source_hash": "sha256:...",
  "source_path": "...",
  "source_mtime": "ISO",
  "extraction_method": "pdfplumber" | "tesseract",
  "ocr_engine": "tesseract-5.3.1" | null,
  "languages": ["eng", "chi_sim"],
  "min_confidence": 0.95,
  "pages": [
    {
      "page": 1, "width": 612, "height": 792,
      "words": [{"text": "Apr", "x0": 72.1, "y0": 100.4, "x1": 95.3, "y1": 112.8, "confidence": 0.99}],
      "text": "full page text..."
    }
  ]
}

CLI:
    python -m app.ocr_normalize <file_path>
    python -m app.ocr_normalize --inbox /onedrive/...   # bulk normalize a folder
    python -m app.ocr_normalize --rescan                # force re-OCR all
    python -m app.ocr_normalize --status                # show cache stats
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

CACHE_DIR = Path("/data/ocr_cache")
LANGUAGES = "eng+chi_sim"
DPI = 300
LOW_CONFIDENCE_THRESHOLD = 0.60
SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".heic"}


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_path_for(source_hash: str) -> Path:
    return CACHE_DIR / f"{source_hash}.ocr.json"


def _failed_path_for(source_hash: str) -> Path:
    return CACHE_DIR / f"{source_hash}.failed.json"


def _cache_fresh(source_path: Path, cache_path: Path) -> bool:
    if not cache_path.exists():
        return False
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        src_mtime = datetime.fromtimestamp(source_path.stat().st_mtime).isoformat()
        return cached.get("source_mtime", "") >= src_mtime
    except Exception:
        return False


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _extract_pdfplumber(pdf_path: Path) -> Optional[list[dict]]:
    """Try text-PDF extraction via pdfplumber. Returns page list or None on fail."""
    try:
        import pdfplumber
    except ImportError:
        return None
    pages = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                words = page.extract_words(keep_blank_chars=False) or []
                text = page.extract_text() or ""
                pages.append({
                    "page": i,
                    "width": float(page.width), "height": float(page.height),
                    "words": [
                        {
                            "text": w["text"],
                            "x0": float(w["x0"]), "y0": float(w["top"]),
                            "x1": float(w["x1"]), "y1": float(w["bottom"]),
                            "confidence": 1.0,  # text PDFs are exact
                        }
                        for w in words
                    ],
                    "text": text,
                })
    except Exception:
        return None
    return pages


def _looks_blank(pages: list[dict]) -> bool:
    """Heuristic: < 50 total words across all pages → likely image-PDF."""
    total = sum(len(p.get("words", [])) for p in pages)
    return total < 50


def _ocr_image(image_path: Path, languages: str = LANGUAGES) -> tuple[list[dict], list[dict], float]:
    """Run tesseract on a single image. Returns (words, page_dict_extras, p25_confidence).

    Uses 25th-percentile confidence as the quality metric — robust to single outliers
    (logos, borders) that would skew min, but catches systematic OCR failure.
    """
    import pytesseract
    from PIL import Image
    img = Image.open(image_path)
    data = pytesseract.image_to_data(img, lang=languages, output_type=pytesseract.Output.DICT)
    words = []
    confidences = []
    n = len(data["text"])
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text: continue
        conf = float(data["conf"][i]) / 100.0   # tesseract returns 0-100
        if conf < 0: continue                    # tesseract returns -1 for low conf — skip
        confidences.append(conf)
        words.append({
            "text": text,
            "x0": float(data["left"][i]),
            "y0": float(data["top"][i]),
            "x1": float(data["left"][i] + data["width"][i]),
            "y1": float(data["top"][i] + data["height"][i]),
            "confidence": conf,
        })
    # 25th-percentile confidence — robust against logo/border outliers
    quality_conf = 0.0
    if confidences:
        confidences.sort()
        idx = max(0, int(len(confidences) * 0.25))
        quality_conf = confidences[idx]
    return words, {"width": float(img.width), "height": float(img.height)}, quality_conf


def _words_to_lines(words: list[dict], y_tol: int = 8) -> str:
    """Reconstruct line-structured text from positioned words by grouping on y0.
    Critical for regex parsers that match per-line patterns (HSBC, etc)."""
    if not words: return ""
    # Sort by y, then x
    sorted_words = sorted(words, key=lambda w: (round(w["y0"] / y_tol), w["x0"]))
    lines = []
    current_line_y = None
    current_line = []
    for w in sorted_words:
        row = round(w["y0"] / y_tol)
        if current_line_y is None or row == current_line_y:
            current_line.append(w["text"])
            current_line_y = row
        else:
            lines.append(" ".join(current_line))
            current_line = [w["text"]]
            current_line_y = row
    if current_line:
        lines.append(" ".join(current_line))
    return "\n".join(lines)


def _ocr_pdf_via_rasterize(pdf_path: Path, languages: str = LANGUAGES) -> tuple[list[dict], float]:
    """Rasterize each PDF page to PNG, OCR each, return canonical page list."""
    try:
        from pdf2image import convert_from_path
    except ImportError:
        # Fallback: use pdftoppm directly
        return _ocr_pdf_via_pdftoppm(pdf_path, languages)
    pages = []
    all_confs = []
    with tempfile.TemporaryDirectory() as tmpdir:
        images = convert_from_path(str(pdf_path), dpi=DPI, output_folder=tmpdir)
        for i, img in enumerate(images, start=1):
            tmp_png = Path(tmpdir) / f"page_{i}.png"
            img.save(tmp_png, "PNG")
            words, geom, min_conf = _ocr_image(tmp_png, languages)
            page_text = _words_to_lines(words)
            pages.append({
                "page": i, "width": geom["width"], "height": geom["height"],
                "words": words, "text": page_text,
            })
            if words: all_confs.append(min_conf)
    overall_min = min(all_confs) if all_confs else 0.0
    return pages, overall_min


def _ocr_pdf_via_pdftoppm(pdf_path: Path, languages: str = LANGUAGES) -> tuple[list[dict], float]:
    """Fallback if pdf2image not available — use poppler's pdftoppm directly."""
    pages = []
    all_confs = []
    with tempfile.TemporaryDirectory() as tmpdir:
        out_prefix = Path(tmpdir) / "page"
        subprocess.run(["pdftoppm", "-r", str(DPI), "-png", str(pdf_path), str(out_prefix)],
                        check=True, capture_output=True)
        for i, png_path in enumerate(sorted(Path(tmpdir).glob("page-*.png")), start=1):
            words, geom, min_conf = _ocr_image(png_path, languages)
            page_text = " ".join(w["text"] for w in words)
            pages.append({
                "page": i, "width": geom["width"], "height": geom["height"],
                "words": words, "text": page_text,
            })
            if words: all_confs.append(min_conf)
    overall_min = min(all_confs) if all_confs else 0.0
    return pages, overall_min


def _heic_to_png(heic_path: Path, dst_dir: Path) -> Path:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    from PIL import Image
    img = Image.open(heic_path)
    out = dst_dir / (heic_path.stem + ".png")
    img.save(out, "PNG")
    return out


def _tesseract_version() -> str:
    try:
        import pytesseract
        return f"tesseract-{pytesseract.get_tesseract_version()}"
    except Exception:
        return "tesseract-unknown"


def normalize(file_path: Path, force: bool = False) -> dict:
    """Normalize a single document. Returns the cached extraction (loaded JSON).

    Idempotent: if cache exists and is fresher than source, returns cached.
    On failure: writes a .failed.json sentinel and raises.
    """
    file_path = Path(file_path).resolve()
    if not file_path.exists():
        raise FileNotFoundError(file_path)
    ext = file_path.suffix.lower()
    if ext not in SUPPORTED:
        raise ValueError(f"Unsupported format: {ext}")

    source_hash = _sha256_of(file_path)
    cache_path = _cache_path_for(source_hash)
    if cache_path.exists() and not force and _cache_fresh(file_path, cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    src_mtime = datetime.fromtimestamp(file_path.stat().st_mtime).isoformat()
    src_size = file_path.stat().st_size
    extraction_method = None
    ocr_engine = None
    pages = []
    min_conf = 1.0

    try:
        if ext == ".pdf":
            pages = _extract_pdfplumber(file_path) or []
            if not pages or _looks_blank(pages):
                pages, min_conf = _ocr_pdf_via_rasterize(file_path)
                extraction_method = "tesseract"
                ocr_engine = _tesseract_version()
            else:
                extraction_method = "pdfplumber"
        elif ext == ".heic":
            with tempfile.TemporaryDirectory() as tmpdir:
                png = _heic_to_png(file_path, Path(tmpdir))
                words, geom, min_conf = _ocr_image(png)
            text = " ".join(w["text"] for w in words)
            pages = [{"page": 1, **geom, "words": words, "text": text}]
            extraction_method = "tesseract"
            ocr_engine = _tesseract_version()
        else:  # jpg/png/tiff/bmp
            words, geom, min_conf = _ocr_image(file_path)
            text = " ".join(w["text"] for w in words)
            pages = [{"page": 1, **geom, "words": words, "text": text}]
            extraction_method = "tesseract"
            ocr_engine = _tesseract_version()
    except Exception as e:
        failed = {
            "source_hash": source_hash,
            "source_path": str(file_path),
            "source_mtime": src_mtime,
            "error": str(e),
            "failed_at": datetime.utcnow().isoformat(),
        }
        _write_atomic(_failed_path_for(source_hash), failed)
        _log_to_db(source_hash, file_path, src_mtime, src_size, ext, None, None,
                   None, 0, 0, None, "failed", str(e))
        raise

    result = {
        "source_hash": source_hash,
        "source_path": str(file_path),
        "source_mtime": src_mtime,
        "extraction_method": extraction_method,
        "ocr_engine": ocr_engine,
        "languages": ["eng", "chi_sim"] if extraction_method == "tesseract" else [],
        "min_confidence": round(min_conf, 4),    # NB: 25th-percentile (not absolute min)
        "page_count": len(pages),
        "word_count": sum(len(p.get("words", [])) for p in pages),
        "extracted_at": datetime.utcnow().isoformat(),
        "pages": pages,
    }
    _write_atomic(cache_path, result)

    status = "low_confidence" if (extraction_method == "tesseract" and min_conf < LOW_CONFIDENCE_THRESHOLD) else "ready"
    _log_to_db(source_hash, file_path, src_mtime, src_size, ext.lstrip("."),
               extraction_method, ocr_engine, "+".join(result["languages"]) or None,
               len(pages), result["word_count"], round(min_conf, 4),
               status, None)
    return result


def _log_to_db(source_hash, source_path, src_mtime, src_size, file_format,
                method, engine, languages, page_count, word_count, min_conf,
                status, error_msg):
    """Upsert into ocr_normalize_log table (idempotent on source_hash)."""
    try:
        from app import database as db
        from sqlalchemy import text
        db.init_db()
        s = db.SessionLocal()
        s.execute(text("""
          CREATE TABLE IF NOT EXISTS ocr_normalize_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_hash VARCHAR NOT NULL UNIQUE,
            source_path VARCHAR NOT NULL,
            source_mtime DATETIME,
            source_size INTEGER,
            file_format VARCHAR,
            extraction_method VARCHAR,
            ocr_engine VARCHAR,
            languages VARCHAR,
            page_count INTEGER,
            word_count INTEGER,
            min_confidence FLOAT,
            cache_path VARCHAR,
            status VARCHAR NOT NULL,
            error_msg VARCHAR,
            extracted_at DATETIME NOT NULL
          )
        """))
        s.execute(text("CREATE INDEX IF NOT EXISTS ix_ocrlog_status ON ocr_normalize_log(status)"))
        # Upsert
        existing = s.execute(text("SELECT id FROM ocr_normalize_log WHERE source_hash=:h"),
                              {"h": source_hash}).fetchone()
        params = {
            "h": source_hash, "p": str(source_path), "mt": src_mtime,
            "sz": src_size, "fmt": file_format, "method": method, "eng": engine,
            "lang": languages, "pc": page_count, "wc": word_count,
            "mc": min_conf, "cp": str(_cache_path_for(source_hash)),
            "st": status, "err": error_msg,
        }
        if existing:
            s.execute(text("""
              UPDATE ocr_normalize_log
              SET source_path=:p, source_mtime=:mt, source_size=:sz, file_format=:fmt,
                  extraction_method=:method, ocr_engine=:eng, languages=:lang,
                  page_count=:pc, word_count=:wc, min_confidence=:mc, cache_path=:cp,
                  status=:st, error_msg=:err, extracted_at=CURRENT_TIMESTAMP
              WHERE source_hash=:h
            """), params)
        else:
            s.execute(text("""
              INSERT INTO ocr_normalize_log
                (source_hash, source_path, source_mtime, source_size, file_format,
                 extraction_method, ocr_engine, languages, page_count, word_count,
                 min_confidence, cache_path, status, error_msg, extracted_at)
              VALUES (:h, :p, :mt, :sz, :fmt, :method, :eng, :lang, :pc, :wc,
                      :mc, :cp, :st, :err, CURRENT_TIMESTAMP)
            """), params)
        s.commit()
        s.close()
    except Exception as e:
        print(f"[ocr_normalize] DB log failed: {e}")


def load_cached(file_path: Path) -> Optional[dict]:
    """Read-only: return cached extraction if present, else None."""
    file_path = Path(file_path).resolve()
    if not file_path.exists():
        return None
    h = _sha256_of(file_path)
    cp = _cache_path_for(h)
    if not cp.exists():
        return None
    with open(cp, "r", encoding="utf-8") as f:
        return json.load(f)


def bulk_normalize(folder: Path, recursive: bool = True, force: bool = False) -> dict:
    """Walk a folder and normalize every supported file. Returns counts."""
    folder = Path(folder)
    if recursive:
        files = [p for p in folder.rglob("*") if p.suffix.lower() in SUPPORTED]
    else:
        files = [p for p in folder.iterdir() if p.suffix.lower() in SUPPORTED]
    stats = {"total": len(files), "extracted": 0, "cached": 0, "failed": 0}
    for f in files:
        try:
            h = _sha256_of(f)
            cache_path = _cache_path_for(h)
            if cache_path.exists() and not force and _cache_fresh(f, cache_path):
                stats["cached"] += 1
                continue
            r = normalize(f, force=force)
            method = r["extraction_method"]
            wc = r["word_count"]
            mc = r["min_confidence"]
            stats["extracted"] += 1
            print(f"  ✓ {f.name[:60]:<60}  method={method:<11}  words={wc:>5}  min_conf={mc:.2f}")
        except Exception as e:
            stats["failed"] += 1
            print(f"  ✗ {f.name[:60]:<60}  ERROR: {str(e)[:80]}")
    return stats


def show_status():
    """Print summary of /data/ocr_cache + DB log."""
    from app import database as db
    from sqlalchemy import text
    db.init_db()
    s = db.SessionLocal()
    rows = s.execute(text("""
        SELECT status, COUNT(*), AVG(min_confidence), AVG(word_count),
               COUNT(DISTINCT file_format)
        FROM ocr_normalize_log GROUP BY status
    """)).all()
    print("\n=== ocr_normalize_log ===\n")
    for r in rows:
        avg_conf = float(r[2] or 0)
        avg_words = float(r[3] or 0)
        print(f"  {r[0]:<18}  {r[1]:>4} docs  avg_conf={avg_conf:.2f}  avg_words={avg_words:.0f}")
    if CACHE_DIR.exists():
        json_count = len(list(CACHE_DIR.glob("*.ocr.json")))
        failed_count = len(list(CACHE_DIR.glob("*.failed.json")))
        total_bytes = sum(f.stat().st_size for f in CACHE_DIR.glob("*.json"))
        print(f"\n  cache_dir: {CACHE_DIR}")
        print(f"  cached jsons: {json_count}  failed: {failed_count}")
        print(f"  total cache size: {total_bytes / 1024 / 1024:.1f} MB")
    s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="Path to a single file to normalize")
    ap.add_argument("--inbox", help="Bulk normalize all files under this folder (recursive)")
    ap.add_argument("--rescan", action="store_true", help="Force re-OCR ignoring cache")
    ap.add_argument("--status", action="store_true", help="Show cache + DB stats")
    args = ap.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if args.status:
        show_status()
        return
    if args.inbox:
        stats = bulk_normalize(Path(args.inbox), recursive=True, force=args.rescan)
        print(f"\nDone: {stats['total']} total, {stats['extracted']} extracted, "
              f"{stats['cached']} cached, {stats['failed']} failed")
        return
    if args.path:
        r = normalize(Path(args.path), force=args.rescan)
        print(f"\n✓ Normalized {args.path}")
        print(f"  method={r['extraction_method']}  pages={r['page_count']}  "
              f"words={r['word_count']}  min_conf={r['min_confidence']}")
        print(f"  cache → /data/ocr_cache/{r['source_hash']}.ocr.json")
        return
    ap.print_help()


if __name__ == "__main__":
    main()
