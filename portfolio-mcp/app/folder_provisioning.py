"""Sentinel Finance — auto-create the standard folder tree on connected clouds.

When the user clicks "Provision folders" from /config/connectors, this module
creates `Sentinel Finance/` + the standard subfolders on every cloud that's
detected as connected. Operations are idempotent — folders that already exist
are left in place and reported as such.

Backends:
  * Google Drive — via google-api-python-client using the token mounted at
    /google-workspace-mcp/data/token.json. Requires `drive` or
    `drive.file` scope; if missing, returns a scope_missing status.
  * OneDrive (local mount) — writes directly to /onedrive/Sentinel Finance/,
    which is `C:\\Users\\azfar\\OneDrive` bind-mounted into the container.
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PARENT = "Sentinel Finance"
# Folder layout: numbered top-level buckets by document type, plus inbox /
# archive / output staging. Reorg'd 2026-05-14.
SUBFOLDERS = (
    "_INBOX",
    "_QUEUE",
    "_OUT",
    "_ARCHIVE",
    "01_Bank statements",
    "01_Bank statements/DBS_POSB Savings",
    "01_Bank statements/Maybank Ar Rihla",
    "01_Bank statements/Maybank Savings",
    "01_Bank statements/Standard Chartered",
    "01_Bank statements/Wise",
    "02_Credit card statements",
    "03_Credit facilities",
    "03_Credit facilities/DBS Cashline",
    "03_Credit facilities/Moneylender",
    "03_Credit facilities/Maybank CreditAble",
    "04_Loan agreements",
    "04_Loan agreements/Forms",
    "05_Payslips",
    "06_CPF",
    "06_CPF/CPF Statements",
    "06_CPF/CPF IS",
    "07_ILP",
    "08_Insurance",
    "08_Insurance/Policy Documents",
    "09_Tax",
    "10_Crypto",
    "10_Crypto/Coinbase exports",
    "Cashflow forecast",
    # Auto-import drop zones — watcher picks up new CSVs and pushes to Firefly
    "Auto-import",
    "Auto-import/POSB",
    "Auto-import/Maybank",
    "Auto-import/SC",
    "Auto-import/Credit cards",
    "Auto-import/_processed",
)

GOOGLE_TOKEN_PATH = Path("/google-workspace-mcp/data/token.json")
ONEDRIVE_MOUNT = Path("/onedrive")


# ─────────────────────────────────────────────────────────────────────────────
# Google Drive backend
# ─────────────────────────────────────────────────────────────────────────────

def _drive_service():
    """Build a Drive API client from the mounted token, or None if missing."""
    if not GOOGLE_TOKEN_PATH.exists():
        return None, "google token.json not mounted into this container"
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file(
            str(GOOGLE_TOKEN_PATH),
            ["https://www.googleapis.com/auth/drive",
             "https://www.googleapis.com/auth/drive.file"])
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        return svc, None
    except Exception as e:
        return None, str(e)[:120]


def _drive_find(svc, name: str, parent_id: str | None) -> str | None:
    """Return the Drive folder ID for `name` under `parent_id`, or None."""
    q = (f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
         f"and trashed=false")
    if parent_id:
        q += f" and '{parent_id}' in parents"
    r = svc.files().list(q=q, fields="files(id,name)", pageSize=2).execute()
    items = r.get("files", [])
    return items[0]["id"] if items else None


def _drive_create(svc, name: str, parent_id: str | None) -> str:
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        body["parents"] = [parent_id]
    return svc.files().create(body=body, fields="id").execute()["id"]


def provision_google_drive() -> dict:
    """Idempotent: create `Sentinel Finance/<sub>` for every entry in SUBFOLDERS."""
    svc, err = _drive_service()
    if svc is None:
        return {"ok": False, "backend": "google_drive", "error": err, "folders": []}
    try:
        results = []
        # Parent
        parent_id = _drive_find(svc, PARENT, None)
        if parent_id:
            results.append({"name": PARENT, "status": "exists", "id": parent_id})
        else:
            parent_id = _drive_create(svc, PARENT, None)
            results.append({"name": PARENT, "status": "created", "id": parent_id})
        # Subfolders (supports `A/B` nesting)
        for sub in SUBFOLDERS:
            segments = sub.split("/")
            cur_parent = parent_id
            for i, seg in enumerate(segments):
                fid = _drive_find(svc, seg, cur_parent)
                full = "/".join(segments[: i + 1])
                if fid:
                    results.append({"name": f"{PARENT}/{full}", "status": "exists", "id": fid})
                else:
                    fid = _drive_create(svc, seg, cur_parent)
                    results.append({"name": f"{PARENT}/{full}", "status": "created", "id": fid})
                cur_parent = fid
        return {"ok": True, "backend": "google_drive", "folders": results}
    except Exception as e:
        logger.exception("Google Drive provisioning failed")
        return {"ok": False, "backend": "google_drive",
                "error": str(e)[:200], "folders": []}


# ─────────────────────────────────────────────────────────────────────────────
# OneDrive backend (via local bind mount)
# ─────────────────────────────────────────────────────────────────────────────

def provision_onedrive_local() -> dict:
    """Create the same tree under /onedrive/Sentinel Finance/."""
    if not ONEDRIVE_MOUNT.exists():
        return {"ok": False, "backend": "onedrive",
                "error": f"{ONEDRIVE_MOUNT} not bind-mounted into container",
                "folders": []}
    try:
        results = []
        parent = ONEDRIVE_MOUNT / PARENT
        if parent.exists():
            results.append({"name": PARENT, "status": "exists", "path": str(parent)})
        else:
            parent.mkdir(parents=True, exist_ok=False)
            results.append({"name": PARENT, "status": "created", "path": str(parent)})
        for sub in SUBFOLDERS:
            target = parent / sub
            if target.exists():
                results.append({"name": f"{PARENT}/{sub}", "status": "exists",
                                "path": str(target)})
            else:
                target.mkdir(parents=True, exist_ok=True)
                results.append({"name": f"{PARENT}/{sub}", "status": "created",
                                "path": str(target)})
        return {"ok": True, "backend": "onedrive", "folders": results}
    except Exception as e:
        logger.exception("OneDrive provisioning failed")
        return {"ok": False, "backend": "onedrive",
                "error": str(e)[:200], "folders": []}


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def provision_all() -> dict:
    """Run every available backend and return a combined report."""
    return {
        "google_drive": provision_google_drive(),
        "onedrive": provision_onedrive_local(),
    }
