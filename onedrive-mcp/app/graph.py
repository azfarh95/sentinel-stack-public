import httpx
from . import auth

GRAPH = "https://graph.microsoft.com/v1.0"
_SELECT = "id,name,size,lastModifiedDateTime,createdDateTime,folder,file,parentReference"


async def _get(path: str, params: dict = None) -> dict:
    token = await auth.get_access_token()
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GRAPH}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


async def _download(path: str) -> bytes:
    token = await auth.get_access_token()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.get(
            f"{GRAPH}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )
        r.raise_for_status()
        return r.content


async def list_children(folder_path: str = "") -> list[dict]:
    if folder_path:
        path = f"/me/drive/root:/{folder_path.strip('/')}:/children"
    else:
        path = "/me/drive/root/children"
    data = await _get(path, {"$top": 200, "$select": _SELECT})
    return data.get("value", [])


async def search(query: str, limit: int = 20) -> list[dict]:
    data = await _get(
        f"/me/drive/search(q='{query}')",
        {"$top": limit, "$select": _SELECT},
    )
    return data.get("value", [])


async def get_item(item_id: str) -> dict:
    return await _get(f"/me/drive/items/{item_id}")


async def download_item(item_id: str) -> bytes:
    return await _download(f"/me/drive/items/{item_id}/content")


async def recent(limit: int = 10) -> list[dict]:
    data = await _get("/me/drive/recent", {"$top": limit, "$select": _SELECT})
    return data.get("value", [])
