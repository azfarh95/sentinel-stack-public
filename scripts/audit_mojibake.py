"""Quick audit: scan all public issues for mojibake patterns."""
import subprocess, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

out = subprocess.run(
    ["gh", "issue", "list", "--repo", "YOUR_GITHUB_USERNAME/sentinel-stack-public",
     "--state", "all", "--limit", "50", "--json", "number,body"],
    capture_output=True, text=True, encoding="utf-8"
).stdout
issues = json.loads(out)

# Common UTF-8-mis-decoded-as-cp1252 markers
mojibake_patterns = ["â†'",      # â†' (was →)
                     "â€”",  # â€" (was —)
                     "âœ…",  # âœ… (was ✅)
                     "ðŸŸ",  # ðŸŸ (was 🟢/🟡 family)
                     "âšª",  # âšª (was ⚪)
                     ]

leaks = []
for issue in issues:
    body = issue.get("body", "") or ""
    for p in mojibake_patterns:
        if p in body:
            leaks.append((issue["number"], p))
            break

print(f"Audited {len(issues)} issues")
if not leaks:
    print("  CLEAN - no mojibake patterns found")
else:
    print(f"  {len(leaks)} issues still contain mojibake:")
    for n, p in leaks:
        print(f"    #{n}: contains {repr(p)}")

# Spot-print public#25 first 8 lines
print()
print("=== public#25 (roadmap) first 8 lines ===")
out2 = subprocess.run(
    ["gh", "issue", "view", "25", "--repo", "YOUR_GITHUB_USERNAME/sentinel-stack-public", "--json", "body"],
    capture_output=True, text=True, encoding="utf-8"
).stdout
body = json.loads(out2)["body"]
for line in body.splitlines()[:8]:
    print(f"  {line}")
