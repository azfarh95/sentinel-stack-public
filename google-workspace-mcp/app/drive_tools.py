import asyncio
import io

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from .auth import get_credentials

_EXPORT_MIME = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

_TEXT_MIME_PREFIXES = ("text/", "application/json", "application/xml")


def _service():
    creds = get_credentials()
    if not creds:
        raise RuntimeError("Not authenticated. Visit http://localhost:8089/oauth to authorize.")
    return build("drive", "v3", credentials=creds)


def _download_bytes(request) -> bytes:
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


async def list_files(
    max_results: int = 20,
    folder_id: str = None,
    query: str = "",
) -> list:
    loop = asyncio.get_running_loop()
    def _run():
        parts = ["trashed = false"]
        if folder_id:
            parts.append(f"'{folder_id}' in parents")
        if query:
            parts.append(f"name contains '{query}'")
        result = _service().files().list(
            q=" and ".join(parts),
            pageSize=max_results,
            fields="files(id,name,mimeType,size,modifiedTime,webViewLink)",
        ).execute()
        return result.get("files", [])
    return await loop.run_in_executor(None, _run)


async def search_files(query: str, max_results: int = 20) -> list:
    loop = asyncio.get_running_loop()
    def _run():
        result = _service().files().list(
            q=f"fullText contains '{query}' and trashed = false",
            pageSize=max_results,
            fields="files(id,name,mimeType,size,modifiedTime,webViewLink)",
        ).execute()
        return result.get("files", [])
    return await loop.run_in_executor(None, _run)


async def get_file(file_id: str) -> dict:
    loop = asyncio.get_running_loop()
    def _run():
        return _service().files().get(
            fileId=file_id,
            fields="id,name,mimeType,size,modifiedTime,webViewLink,parents",
        ).execute()
    return await loop.run_in_executor(None, _run)


async def read_file(file_id: str) -> dict:
    loop = asyncio.get_running_loop()
    def _run():
        svc = _service()
        meta = svc.files().get(fileId=file_id, fields="name,mimeType").execute()
        mime = meta.get("mimeType", "")

        if mime in _EXPORT_MIME:
            export_mime = _EXPORT_MIME[mime]
            raw = _download_bytes(svc.files().export_media(fileId=file_id, mimeType=export_mime))
        elif any(mime.startswith(p) for p in _TEXT_MIME_PREFIXES):
            raw = _download_bytes(svc.files().get_media(fileId=file_id))
        else:
            return {"name": meta["name"], "mimeType": mime, "content": None,
                    "note": "Binary file — content not extracted."}

        return {
            "name": meta["name"],
            "mimeType": mime,
            "content": raw.decode("utf-8", errors="ignore")[:5000],
        }
    return await loop.run_in_executor(None, _run)


async def create_folder(name: str, parent_id: str = None) -> dict:
    loop = asyncio.get_running_loop()
    def _run():
        body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            body["parents"] = [parent_id]
        return _service().files().create(body=body, fields="id,name,webViewLink").execute()
    return await loop.run_in_executor(None, _run)


async def share_file(file_id: str, email: str, role: str = "reader") -> dict:
    loop = asyncio.get_running_loop()
    def _run():
        perm = _service().permissions().create(
            fileId=file_id,
            body={"type": "user", "role": role, "emailAddress": email},
            sendNotificationEmail=True,
        ).execute()
        return {"permissionId": perm["id"], "role": role, "email": email}
    return await loop.run_in_executor(None, _run)
