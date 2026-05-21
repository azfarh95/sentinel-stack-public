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


# ── Labels ─────────────────────────────────────────────────────────────────────

def _resolve_label(svc, label: str) -> str:
    """Resolve a label_id or label_name to a label_id. Raises if not found."""
    if label.startswith(("Label_", "INBOX", "SENT", "DRAFT", "SPAM", "TRASH",
                         "STARRED", "UNREAD", "IMPORTANT", "CHAT", "CATEGORY_")):
        return label
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == label:
            return lbl["id"]
    raise RuntimeError(f"Label not found: {label!r}")


async def list_labels() -> list:
    """Return all Gmail labels (system + user) with id, name, type."""
    loop = asyncio.get_running_loop()
    def _run():
        result = _service().users().labels().list(userId="me").execute()
        return [
            {"id": l["id"], "name": l["name"], "type": l.get("type", "user")}
            for l in result.get("labels", [])
        ]
    return await loop.run_in_executor(None, _run)


async def create_label(name: str) -> dict:
    """
    Create a user label (idempotent — returns existing if name matches).
    Use '/' for nested labels, e.g. 'Bank Statements/HSBC Statement'.
    """
    loop = asyncio.get_running_loop()
    def _run():
        svc = _service()
        existing = svc.users().labels().list(userId="me").execute().get("labels", [])
        for lbl in existing:
            if lbl["name"] == name:
                return {"id": lbl["id"], "name": lbl["name"], "created": False}
        result = svc.users().labels().create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ).execute()
        return {"id": result["id"], "name": result["name"], "created": True}
    return await loop.run_in_executor(None, _run)


async def apply_label(email_id: str, label: str) -> dict:
    """
    Add a label to an email. `label` accepts label_id or label_name.
    """
    loop = asyncio.get_running_loop()
    def _run():
        svc = _service()
        label_id = _resolve_label(svc, label)
        svc.users().messages().modify(
            userId="me", id=email_id, body={"addLabelIds": [label_id]}
        ).execute()
        return {"applied": True, "email_id": email_id, "label_id": label_id}
    return await loop.run_in_executor(None, _run)


async def remove_label(email_id: str, label: str) -> dict:
    """Remove a label from an email. `label` accepts label_id or label_name."""
    loop = asyncio.get_running_loop()
    def _run():
        svc = _service()
        label_id = _resolve_label(svc, label)
        svc.users().messages().modify(
            userId="me", id=email_id, body={"removeLabelIds": [label_id]}
        ).execute()
        return {"removed": True, "email_id": email_id, "label_id": label_id}
    return await loop.run_in_executor(None, _run)


async def delete_label(label: str) -> dict:
    """Delete a user label (system labels can't be deleted). Accepts id or name."""
    loop = asyncio.get_running_loop()
    def _run():
        svc = _service()
        label_id = _resolve_label(svc, label)
        svc.users().labels().delete(userId="me", id=label_id).execute()
        return {"deleted": True, "label_id": label_id}
    return await loop.run_in_executor(None, _run)


# ── Filters ────────────────────────────────────────────────────────────────────

async def list_filters() -> list:
    """Return all Gmail filter rules with their criteria and actions."""
    loop = asyncio.get_running_loop()
    def _run():
        result = _service().users().settings().filters().list(userId="me").execute()
        return result.get("filter", [])
    return await loop.run_in_executor(None, _run)


async def create_filter(
    add_labels: list[str],
    from_addr: str | None = None,
    to_addr: str | None = None,
    subject: str | None = None,
    query: str | None = None,
    has_attachment: bool | None = None,
    remove_labels: list[str] | None = None,
) -> dict:
    """
    Create a Gmail filter rule.
    add_labels / remove_labels: list of label_ids OR label_names (auto-resolved).
    Criteria — provide at least one of: from_addr, to_addr, subject, query, has_attachment.
        from_addr: matches the From header (substring or full address)
        query: full Gmail search syntax for advanced criteria, e.g. 'from:a OR from:b subject:statement'
    """
    loop = asyncio.get_running_loop()
    def _run():
        svc = _service()
        criteria = {}
        if from_addr: criteria["from"] = from_addr
        if to_addr: criteria["to"] = to_addr
        if subject: criteria["subject"] = subject
        if query: criteria["query"] = query
        if has_attachment is not None: criteria["hasAttachment"] = has_attachment
        if not criteria:
            raise ValueError("At least one criterion required (from_addr, query, etc.)")
        action = {}
        action["addLabelIds"] = [_resolve_label(svc, l) for l in add_labels]
        if remove_labels:
            action["removeLabelIds"] = [_resolve_label(svc, l) for l in remove_labels]
        result = svc.users().settings().filters().create(
            userId="me", body={"criteria": criteria, "action": action}
        ).execute()
        return result
    return await loop.run_in_executor(None, _run)


async def delete_filter(filter_id: str) -> dict:
    """Delete a filter rule by its ID (use list_filters to find the ID)."""
    loop = asyncio.get_running_loop()
    def _run():
        _service().users().settings().filters().delete(
            userId="me", id=filter_id
        ).execute()
        return {"deleted": True, "filter_id": filter_id}
    return await loop.run_in_executor(None, _run)


async def apply_label_to_query(label: str, query: str, max_results: int = 200) -> dict:
    """
    Retroactively apply a label to all emails matching a Gmail search query.
    Useful immediately after creating a filter (filters only apply to NEW mail).
    Returns count of messages affected.
    """
    loop = asyncio.get_running_loop()
    def _run():
        svc = _service()
        label_id = _resolve_label(svc, label)
        msgs = []
        page_token = None
        while len(msgs) < max_results:
            params = {"userId": "me", "q": query, "maxResults": min(500, max_results - len(msgs))}
            if page_token:
                params["pageToken"] = page_token
            result = svc.users().messages().list(**params).execute()
            msgs.extend(result.get("messages", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        if not msgs:
            return {"applied": 0, "label_id": label_id, "query": query}
        # Batch modify (max 1000 per call)
        ids = [m["id"] for m in msgs]
        for i in range(0, len(ids), 1000):
            svc.users().messages().batchModify(
                userId="me",
                body={"ids": ids[i:i+1000], "addLabelIds": [label_id]},
            ).execute()
        return {"applied": len(ids), "label_id": label_id, "query": query}
    return await loop.run_in_executor(None, _run)
