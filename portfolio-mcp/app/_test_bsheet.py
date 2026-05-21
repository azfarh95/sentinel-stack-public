import asyncio
from app import balance_sheet as bs

data = asyncio.run(bs.build_balance_sheet())
print(f"=== Balance Sheet snapshot ===")
print(f"  Total Assets SGD:      ${data['assets']['sgd']:>12,.2f}")
print(f"  Total Liabilities SGD: ${data['liabilities']['sgd']:>12,.2f}")
print(f"  Net Worth SGD:         ${data['net_worth_sgd']:>12,.2f}")
print(f"  USD/SGD fx:            {data['usd_to_sgd']}")

print(f"\n=== Current Assets (top nodes) ===")
for n in data['assets']['current']['nodes'][:10]:
    print(f"  {n.get('label','?'):<40} SGD ${n.get('sgd',0):>12,.2f}")

print(f"\n=== Non-Current Assets (top nodes) ===")
for n in data['assets']['non_current']['nodes'][:10]:
    print(f"  {n.get('label','?'):<40} SGD ${n.get('sgd',0):>12,.2f}")
