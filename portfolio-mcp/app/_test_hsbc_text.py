"""Show pages 2-3 of HSBC OCR cache (transaction list)."""
import json, re
path = "/data/ocr_cache/d3d122449f356c396d8ea870e83b1a1cdd01ea4ba3a47f89d186ef9bf18957f7.ocr.json"
with open(path) as f: d = json.load(f)
for pn, page in enumerate(d["pages"], start=1):
    print(f"\n=== PAGE {pn} (last 30 lines) ===")
    lines = page["text"].split("\n")
    for line in lines[-30:]:
        print(f"  {line}")
    # Look for lines that look like tx (start with day-number)
    rx = re.compile(r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)")
    candidates = [l for l in lines if rx.match(l)]
    print(f"  [page {pn}] lines starting with day+month: {len(candidates)}")
    for c in candidates[:8]:
        print(f"    → {c}")
