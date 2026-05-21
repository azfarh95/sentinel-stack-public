import asyncio
from app import category_drill as cd

print("=== /category?slug=4110 (AZ United Salary, 2025 YTD) ===")
data = asyncio.run(cd.list_category_transactions("4110", "deposit", 2025))
print(f"  source: {data.get('data_source')!r}  count: {data['totals']['count']}  total: ${data['totals']['sgd']:,.2f}")
for t in data['transactions'][:5]:
    print(f"  {t['date']} ${t['amount']:>9,.2f}  {t['description'][:70]}")

print("\n=== /category?slug=4900 (Other Income, 2025) ===")
data = asyncio.run(cd.list_category_transactions("4900", "deposit", 2025))
print(f"  source: {data.get('data_source')!r}  count: {data['totals']['count']}  total: ${data['totals']['sgd']:,.2f}")
for t in data['transactions'][:5]:
    print(f"  {t['date']} ${t['amount']:>9,.2f}  {t['description'][:70]}")

print("\n=== /category?slug=pending ===")
data = asyncio.run(cd.list_category_transactions("pending", "any", 2026))
print(f"  source: {data.get('data_source')!r}  count: {data['totals']['count']}  total: ${data['totals']['sgd']:,.2f}")
for t in data['transactions'][:3]:
    print(f"  {t['date']} ${t['amount']:>9,.2f}  {t['description'][:70]}")
