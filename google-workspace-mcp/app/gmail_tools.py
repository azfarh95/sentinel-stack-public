import asyncio
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from .auth import get_credentials


def _service():
    creds = get_credentials()
    if not creds:
        raise RuntimeError("Not authenticated. Visit http://localhost:8089/oauth to authorize.")
    return build("gmail", "v1", credentials=creds)


def _decode_body(payload: dict) -> str:
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
    # Fall back to HTML part if no plain text
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
    return ""


def _make_raw(to: str, subject: str, body: str) -> str:
    msg = MIMEMultipart("alternative")
    msg["to"] = to
    msg["subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


async def list_emails(
    max_results: int = 10,
    label: str = "INBOX",
    query: str = "",
) -> list:
    loop = asyncio.get_running_loop()
    def _run():
        svc = _service()
        params = {"userId": "me", "maxResults": max_results, "labelIds": [label]}
        if query:
            params["q"] = query
        result = svc.users().messages().list(**params).execute()
        emails = []
        for m in result.get("messages", []):
            msg = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            emails.append({
                "id": msg["id"],
                "subject": headers.get("Subject"),
                "from": headers.get("From"),
                "date": headers.get("Date"),
                "snippet": msg.get("snippet"),
            })
        return emails
    return await loop.run_in_executor(None, _run)


async def read_email(email_id: str) -> dict:
    loop = asyncio.get_running_loop()
    def _run():
        svc = _service()
        msg = svc.users().messages().get(userId="me", id=email_id, format="full").execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        body = _decode_body(msg.get("payload", {}))
        return {
            "id": msg["id"],
            "subject": headers.get("Subject"),
            "from": headers.get("From"),
            "to": headers.get("To"),
            "date": headers.get("Date"),
            "body": body[:3000],
            "labels": msg.get("labelIds", []),
        }
    return await loop.run_in_executor(None, _run)


async def send_email(to: str, subject: str, body: str) -> dict:
    loop = asyncio.get_running_loop()
    def _run():
        result = _service().users().messages().send(
            userId="me", body={"raw": _make_raw(to, subject, body)}
        ).execute()
        return {"id": result["id"], "threadId": result.get("threadId")}
    return await loop.run_in_executor(None, _run)


async def search_emails(query: str, max_results: int = 10) -> list:
    loop = asyncio.get_running_loop()
    def _run():
        svc = _service()
        result = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        emails = []
        for m in result.get("messages", []):
            msg = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            emails.append({
                "id": msg["id"],
                "subject": headers.get("Subject"),
                "from": headers.get("From"),
                "date": headers.get("Date"),
                "snippet": msg.get("snippet"),
            })
        return emails
    return await loop.run_in_executor(None, _run)


async def trash_email(email_id: str) -> dict:
    loop = asyncio.get_running_loop()
    def _run():
        _service().users().messages().trash(userId="me", id=email_id).execute()
        return {"trashed": True, "email_id": email_id}
    return await loop.run_in_executor(None, _run)


async def create_draft(to: str, subject: str, body: str) -> dict:
    loop = asyncio.get_running_loop()
    def _run():
        draft = _service().users().drafts().create(
            userId="me", body={"message": {"raw": _make_raw(to, subject, body)}}
        ).execute()
        return {"id": draft["id"]}
    return await loop.run_in_executor(None, _run)
