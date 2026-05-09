from contextlib import asynccontextmanager
from pathlib import Path
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from . import auth
from . import calendar_tools, gmail_tools, drive_tools

CREDENTIALS_FILE = os.environ.get("CREDENTIALS_FILE", "/data/credentials.json")


@asynccontextmanager
async def _lifespan(server: FastMCP):
    if not Path(CREDENTIALS_FILE).exists():
        raise RuntimeError(
            f"credentials.json not found at {CREDENTIALS_FILE}. "
            "Mount it as a Docker volume."
        )
    yield


mcp = FastMCP(
    "GoogleWorkspace",
    lifespan=_lifespan,
    instructions=(
        "Access Gmail, Google Calendar, and Google Drive on behalf of the user. "
        "If a tool returns an auth error, tell the user to visit http://localhost:8089/oauth "
        "in their browser to re-authorize. "
        "Typical flows: use calendar_list_events to check upcoming events, "
        "gmail_list_emails / gmail_search_emails to read mail, "
        "drive_list_files / drive_search_files to find documents."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*", "localhost:*", "[::1]:*",
            "host.docker.internal:*", "google-workspace-mcp:*",
        ],
        allowed_origins=[
            "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
            "http://host.docker.internal:*", "http://google-workspace-mcp:*",
        ],
    ),
)


# ── Calendar ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def calendar_list_calendars() -> list:
    """List all Google Calendars accessible to the user."""
    try:
        return await calendar_tools.list_calendars()
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def calendar_list_events(
    calendar_id: str = "primary",
    max_results: int = 10,
    time_min: str = None,
) -> list:
    """
    List upcoming calendar events.
    calendar_id: calendar ID or 'primary' (default).
    max_results: number of events to return (default 10).
    time_min: ISO 8601 start time filter, e.g. '2026-05-01T00:00:00Z' (defaults to now).
    """
    try:
        return await calendar_tools.list_events(calendar_id, max_results, time_min)
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def calendar_create_event(
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    description: str = None,
    location: str = None,
    color_id: str = None,
    recurrence: str = None,
) -> dict:
    """
    Create a calendar event.
    start / end: 'YYYY-MM-DD' for all-day events, or ISO 8601 with timezone e.g. '2026-05-02T10:00:00+08:00' for timed events.
    color_id: optional Google Calendar color (1=Lavender, 2=Sage, 3=Grape, 4=Flamingo, 5=Banana, 6=Tangerine, 7=Peacock, 8=Blueberry, 9=Basil, 10=Tomato).
    recurrence: optional RRULE string for recurring events. Examples: 'RRULE:FREQ=WEEKLY' (every week), 'RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR' (Mon/Wed/Fri), 'RRULE:FREQ=MONTHLY' (monthly), 'RRULE:FREQ=DAILY;COUNT=30' (daily for 30 days), 'RRULE:FREQ=WEEKLY;UNTIL=20261231T000000Z' (weekly until end of year).
    """
    try:
        return await calendar_tools.create_event(summary, start, end, calendar_id, description, location, color_id, recurrence)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def calendar_update_event(
    event_id: str,
    calendar_id: str = "primary",
    summary: str = None,
    start: str = None,
    end: str = None,
    description: str = None,
    color_id: str = None,
    recurrence: str = None,
) -> dict:
    """
    Update fields on an existing calendar event. Only provided fields are changed.
    start / end: 'YYYY-MM-DD' for all-day events, or ISO 8601 with timezone for timed events.
    color_id: Google Calendar color (1=Lavender, 2=Sage, 3=Grape, 4=Flamingo, 5=Banana, 6=Tangerine, 7=Peacock, 8=Blueberry, 9=Basil, 10=Tomato).
    recurrence: RRULE string to add/change recurrence, e.g. 'RRULE:FREQ=WEEKLY;BYDAY=FR'. Pass empty string to clear recurrence.
    """
    try:
        return await calendar_tools.update_event(event_id, calendar_id, summary, start, end, description, color_id, recurrence)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def calendar_delete_event(event_id: str, calendar_id: str = "primary") -> dict:
    """Permanently delete a calendar event."""
    try:
        return await calendar_tools.delete_event(event_id, calendar_id)
    except Exception as e:
        return {"error": str(e)}


# ── Gmail ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def gmail_list_emails(
    max_results: int = 10,
    label: str = "INBOX",
    query: str = "",
) -> list:
    """
    List emails from a label (default: INBOX).
    label: INBOX | SENT | DRAFT | SPAM | TRASH or a custom label name.
    query: optional Gmail search filter applied on top of the label.
    """
    try:
        return await gmail_tools.list_emails(max_results, label, query)
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def gmail_read_email(email_id: str) -> dict:
    """Read the full body and metadata of an email by its ID."""
    try:
        return await gmail_tools.read_email(email_id)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def gmail_send_email(to: str, subject: str, body: str) -> dict:
    """Send a plain-text email."""
    try:
        return await gmail_tools.send_email(to, subject, body)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def gmail_search_emails(query: str, max_results: int = 10) -> list:
    """
    Search emails using Gmail query syntax.
    Examples: 'from:boss@company.com', 'subject:invoice after:2026/04/01', 'is:unread'.
    """
    try:
        return await gmail_tools.search_emails(query, max_results)
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def gmail_trash_email(email_id: str) -> dict:
    """Move an email to Trash."""
    try:
        return await gmail_tools.trash_email(email_id)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def gmail_create_draft(to: str, subject: str, body: str) -> dict:
    """Create an email draft without sending it."""
    try:
        return await gmail_tools.create_draft(to, subject, body)
    except Exception as e:
        return {"error": str(e)}


# ── Drive ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def drive_list_files(
    max_results: int = 20,
    folder_id: str = None,
    query: str = "",
) -> list:
    """
    List files in Google Drive.
    folder_id: restrict to a specific folder (optional).
    query: filter by filename fragment (optional).
    """
    try:
        return await drive_tools.list_files(max_results, folder_id, query)
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def drive_search_files(query: str, max_results: int = 20) -> list:
    """Full-text search across all Drive files and folders."""
    try:
        return await drive_tools.search_files(query, max_results)
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def drive_get_file(file_id: str) -> dict:
    """Get metadata (name, type, size, link) for a Drive file."""
    try:
        return await drive_tools.get_file(file_id)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def drive_read_file(file_id: str) -> dict:
    """
    Read file content from Drive.
    Google Docs → plain text, Sheets → CSV, Presentations → plain text.
    Binary files are not extracted.
    """
    try:
        return await drive_tools.read_file(file_id)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def drive_create_folder(name: str, parent_id: str = None) -> dict:
    """Create a new folder in Drive. Optionally nest inside parent_id."""
    try:
        return await drive_tools.create_folder(name, parent_id)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def drive_share_file(file_id: str, email: str, role: str = "reader") -> dict:
    """
    Share a Drive file or folder with a user.
    role: 'reader' | 'writer' | 'commenter'.
    """
    try:
        return await drive_tools.share_file(file_id, email, role)
    except Exception as e:
        return {"error": str(e)}


# ── ASGI app ───────────────────────────────────────────────────────────────────

async def _health(request: Request) -> JSONResponse:
    creds = auth.get_credentials()
    return JSONResponse({
        "status": "ok",
        "service": "google-workspace-mcp",
        "authenticated": creds is not None,
    })


async def _oauth_start(request: Request):
    if not Path(CREDENTIALS_FILE).exists():
        return JSONResponse({"error": f"credentials.json not found at {CREDENTIALS_FILE}"}, status_code=500)
    url = auth.start_flow()
    return RedirectResponse(url)


async def _oauth_callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        return JSONResponse({"error": "No authorization code received."}, status_code=400)
    try:
        auth.exchange_code(code)
        return JSONResponse({
            "status": "authorized",
            "message": "Google Workspace connected successfully. You can close this tab.",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


app = mcp.streamable_http_app()
app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))
app.router.routes.insert(1, Route("/oauth", _oauth_start, methods=["GET"]))
app.router.routes.insert(2, Route("/oauth/callback", _oauth_callback, methods=["GET"]))
