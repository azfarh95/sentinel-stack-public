"""Extract statement dates from each April 2026 CC statement PDF.
Compares against what Sentinel reported in the Bills calendar benchmark."""
import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

try:
    import pypdf
except ImportError:
    print("pypdf not installed; pip install pypdf")
    sys.exit(1)

APR26 = Path(r"C:\Users\azfar\OneDrive\CC_Statement\Apr'26")
files = [
    "DBS Cashline Apr'26.pdf",
    "DBS CC Apr'26.pdf",
    "HSBC CC Apr'26.pdf",
    "Maybank CA Apr'26.pdf",
    "Platinum Visa Card _25 April 2026.pdf",
    "SC Apr'26.pdf",
    "UOB Apr'26.pdf",
]

# Date patterns to look for: "25 April 2026", "25 Apr 2026", "25/04/2026", "2026-04-25", etc.
DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2})[\s/-]+(?:Apr(?:il)?|04)[\s/-]+(?:20)?(?:26)\b", re.I),
    re.compile(r"\b(\d{1,2})[\s/-]+(\d{1,2})[\s/-]+(?:20)?26\b"),  # numeric-only fallback
    re.compile(r"\bApril\s+(\d{1,2}),?\s+2026\b", re.I),
    re.compile(r"\b2026[\s/-]+04[\s/-]+(\d{1,2})\b"),  # YYYY-MM-DD
]

# Keywords that often precede the statement date
DATE_KEY_PATTERNS = [
    re.compile(r"(statement\s+date|statement\s+as\s+at|as\s+at|date\s+of\s+statement|cycle\s+ends?)[:\s]+([^\n]{0,60})", re.I),
    re.compile(r"(payment\s+due\s+date|due\s+date)[:\s]+([^\n]{0,60})", re.I),
]

print(f"=== April 2026 CC statement extraction ===\n")
print(f"Source: {APR26}\n")

for fname in files:
    path = APR26 / fname
    print("=" * 70)
    print(f"FILE: {fname}")
    if not path.exists():
        print(f"  NOT FOUND")
        continue

    try:
        reader = pypdf.PdfReader(str(path))
        pages = len(reader.pages)
        print(f"  pages: {pages}, encrypted: {reader.is_encrypted}")
        if reader.is_encrypted:
            # Try with empty password
            try:
                reader.decrypt("")
                if reader.is_encrypted:
                    print(f"  (encrypted, no password — skipping text extraction)")
                    continue
            except Exception as e:
                print(f"  (encryption blocks extraction: {e})")
                continue

        text = ""
        for i in range(min(2, pages)):
            text += reader.pages[i].extract_text() or ""

        # Find statement date / due date phrases
        print(f"  date phrases found:")
        for pat in DATE_KEY_PATTERNS:
            for m in pat.finditer(text):
                print(f"    {m.group(0).strip()[:90]!r}")

        # Find ALL April-2026-shaped dates as fallback
        all_dates = set()
        for pat in DATE_PATTERNS:
            for m in pat.finditer(text):
                all_dates.add(m.group(0).strip())
        if all_dates:
            print(f"  all April 2026 date strings: {sorted(all_dates)[:10]}")

    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
    print()
