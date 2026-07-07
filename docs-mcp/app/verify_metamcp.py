"""Verify docs-mcp surfaces through the MetaMCP aggregated endpoint (the same
endpoint OpenClaw consumes). Run inside any container on metamcp-network:
    METAMCP_API_KEY=... python -m app.verify_metamcp
"""
import asyncio
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("METAMCP_URL", "http://metamcp:12008/metamcp/default/mcp")
KEY = os.environ["METAMCP_API_KEY"]


async def main() -> None:
    headers = {"Authorization": f"Bearer {KEY}"}
    async with streamablehttp_client(URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            docs = [n for n in names if "docs" in n.lower()]
            print("total tools:", len(names))
            print("docs tools:", docs)
            if docs:
                search = next(n for n in docs if "search" in n)
                res = await s.call_tool(search, {"query": "funding arbitrage"})
                out = json.loads(res.content[0].text)
                print("aggregated search hits:",
                      [h["path"] for h in out["results"][:3]])


asyncio.run(main())
