import asyncio
from app import home as home_mod
data = asyncio.run(home_mod.build_home_summary())
print("=== Home glance summary ===")
for k in ["bank", "crypto", "ilp", "cpf", "loans", "cc", "recurring", "pending", "net_worth"]:
    v = data.get(k, {})
    if isinstance(v, dict):
        sgd = v.get('sgd', 0); usd = v.get('usd', 0)
        extra = f" [{v.get('count')} tx]" if 'count' in v else ""
        print(f"  {k:<11}  SGD ${sgd:>11,.2f}   USD ${usd:>11,.2f}{extra}")
    else:
        print(f"  {k:<11}  {v}")
