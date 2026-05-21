"""Show ILP node breakdown from build_balance_sheet — where the $36k comes from."""
import asyncio
import json
from app import balance_sheet as bs

data = asyncio.run(bs.build_balance_sheet())
# Walk non_current nodes to find ILP
def find(nodes, target):
    for n in nodes:
        if n.get("id") == target: return n
        r = find(n.get("children", []), target)
        if r: return r
    return None

ilp = find(data["assets"]["non_current"]["nodes"], "ilp")
print("ILP node:")
print(json.dumps(ilp, indent=2)[:3000])
