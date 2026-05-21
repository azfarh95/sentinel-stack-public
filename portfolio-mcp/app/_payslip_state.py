"""Inspect payslip_registry + count POSB suspense items waiting for a payslip match."""
from app import database as db, ledger
from sqlalchemy import select, func, text

db.init_db()
s = db.SessionLocal()

n = s.scalar(select(func.count(ledger.PayslipRegistry.id)))
print(f"payslip_registry rows: {n}")
rows = s.execute(select(
    ledger.PayslipRegistry.employer_key, func.count(ledger.PayslipRegistry.id),
    func.min(ledger.PayslipRegistry.period_end), func.max(ledger.PayslipRegistry.period_end),
    func.sum(ledger.PayslipRegistry.gross_pay), func.sum(ledger.PayslipRegistry.net_pay),
    func.sum(ledger.PayslipRegistry.employee_cpf), func.sum(ledger.PayslipRegistry.employer_cpf)
).group_by(ledger.PayslipRegistry.employer_key)).all()
for r in rows:
    gross = float(r[4] or 0); net = float(r[5] or 0); ee = float(r[6] or 0); er = float(r[7] or 0)
    print(f"  {r[0]:<18} n={r[1]:>3}  {r[2]}..{r[3]}  gross=${gross:>10,.2f}  net=${net:>10,.2f}  ee_cpf=${ee:>8,.2f}  er_cpf=${er:>8,.2f}")
n_with_jid = s.scalar(select(func.count(ledger.PayslipRegistry.id)).where(ledger.PayslipRegistry.journal_id.isnot(None)))
print(f"  rows linked to a GL journal: {n_with_jid} / {n}")

print()
print("=== POSB salary suspense items (the candidates for matching) ===")
rows = s.execute(text("""
    SELECT j.journal_date, j.narration, gl.debit
    FROM journals j
    JOIN general_ledger gl ON gl.journal_id = j.id
    JOIN chart_of_accounts coa ON coa.id = gl.account_id
    WHERE j.source_doc = 'POSB_PDF_DIRECT'
      AND j.status = 'posted'
      AND coa.account_code IN ('1190', '4900')
      AND UPPER(j.narration) LIKE '%SALARY%'
    ORDER BY j.journal_date
""")).all()
print(f"  count: {len(rows)}")
# Re-pull with both debit AND credit (1190 leg of an inflow holds the credit)
rows = s.execute(text("""
    SELECT j.journal_date, j.narration, gl.debit, gl.credit
    FROM journals j
    JOIN general_ledger gl ON gl.journal_id = j.id
    JOIN chart_of_accounts coa ON coa.id = gl.account_id
    WHERE j.source_doc = 'POSB_PDF_DIRECT'
      AND j.status = 'posted'
      AND coa.account_code IN ('1190', '4900')
      AND UPPER(j.narration) LIKE '%SALARY%'
    ORDER BY j.journal_date
""")).all()
for r in rows[:10]:
    amt = float(r[2] or 0) + float(r[3] or 0)
    print(f"    {str(r[0])[:10]}  ${amt:>9,.2f}  {(r[1] or '')[:80]}")
if len(rows) > 10:
    print(f"    ... and {len(rows) - 10} more")

print()
print("=== Existing payslip journals — anatomy of the 10 ===")
rows = s.execute(text("""
    SELECT j.id, j.journal_date, j.narration, j.source_doc, j.source_ref
    FROM journals j
    WHERE j.id IN (SELECT journal_id FROM payslip_registry WHERE journal_id IS NOT NULL)
      AND j.status = 'posted'
    ORDER BY j.journal_date
""")).all()
for r in rows[:3]:
    print(f"  jid={r[0]}  {r[1]}  src={r[3]}  ref={r[4]}")
    legs = s.execute(text("""
      SELECT coa.account_code, coa.account_name, gl.debit, gl.credit, gl.narration
      FROM general_ledger gl
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE gl.journal_id = :j
    """), {"j": r[0]}).all()
    for l in legs:
        dr = float(l[2] or 0); cr = float(l[3] or 0)
        print(f"    {l[0]} {l[1][:30]:<30}  Dr=${dr:>9,.2f}  Cr=${cr:>9,.2f}  {(l[4] or '')[:60]}")
s.close()
