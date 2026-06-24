"""End-to-end self-test: drive the running server over streamable HTTP exactly
like MetaMCP will. Run inside the container:  python -m app.selftest
"""
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main() -> None:
    async with streamablehttp_client("http://localhost:8107/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("TOOLS:", sorted(t.name for t in tools.tools))

            res = await s.call_tool("docs_search", {"query": "funding rate risk defi"})
            out = json.loads(res.content[0].text)
            print("SEARCH count:", out["count"])
            for hit in out["results"][:3]:
                print("  -", hit["path"], "|", hit["title"])

            first = out["results"][0]["path"]
            res = await s.call_tool("docs_page", {"path": first})
            page = json.loads(res.content[0].text)
            print("PAGE:", page["path"], page["total_chars"], "chars, truncated:",
                  page["truncated"])

            res = await s.call_tool("docs_toc", {})
            toc = json.loads(res.content[0].text)
            print("TOC:", toc["pages"], "pages, sections:",
                  {k: len(v) for k, v in toc["sections"].items()})

            res = await s.call_tool("docs_recent", {"limit": 5})
            rec = json.loads(res.content[0].text)
            print("RECENT:", [x["path"] for x in rec["results"]])


asyncio.run(main())
