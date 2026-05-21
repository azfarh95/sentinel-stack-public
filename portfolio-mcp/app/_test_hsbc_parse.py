"""Test HSBC parse via OCR-cache-backed universal parser."""
from pathlib import Path
from app.universal_pdf_parser import load_all_schemas, parse_pdf

schemas = load_all_schemas()
pdf = Path("/onedrive/Sentinel Finance/02_Credit card statements/Apr'26/HSBC CC Apr'26.pdf")
r = parse_pdf(pdf, schemas)
print(f"schema={r.schema_name}  date={r.statement_date}  tx_count={len(r.transactions)}")
for t in r.transactions[:10]:
    tx_type = (t.tx_type or "")[:35]
    print(f"  {t.date_iso}  {tx_type:<35}  W=${t.withdrawal_amount:>9,.2f}  D=${t.deposit_amount:>9,.2f}")
