import asyncio
from app import income_statement as is_mod
data = asyncio.run(is_mod.build_income_statement(year=2025))
print(f"data_source: {data.get('data_source')!r}")
print(f"Income SGD:   {data['totals']['income_sgd']:>10,.2f}")
print(f"Expenses SGD: {data['totals']['expenses_sgd']:>10,.2f}")
print(f"Net SGD:      {data['totals']['net_income_sgd']:>10,.2f}")
print(f"Income lines: {len(data['income'])}, Expense lines: {len(data['expenses'])}")
print(f"Top 5 income:")
for r in data['income'][:5]:
    print(f"  {r['name']:<45} ${r['sgd']:>10,.2f}")
print(f"Top 5 expenses:")
for r in data['expenses'][:5]:
    print(f"  {r['name']:<45} ${r['sgd']:>10,.2f}")
years = asyncio.run(is_mod.available_years())
print(f"\navailable_years(): {years}")
