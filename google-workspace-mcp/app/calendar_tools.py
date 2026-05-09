import asyncio
from datetime import datetime, timezone

from googleapiclient.discovery import build

from .auth import get_credentials


def _service():
    creds = get_credentials()
    if not creds:
        raise RuntimeError("Not authenticated. Visit http://localhost:8089/oauth to authorize.")
    return build("calendar", "v3", credentials=creds)


def _date_field(s: str) -> dict:
    """Return {"date": "YYYY-MM-DD"} for all-day or {"dateTime": ...} for timed events."""
    if s and len(s) == 10 and "T" not in s:
        return {"date": s}
    return {"dateTime": s}


async def list_calendars() -> list:
    loop = asyncio.get_running_loop()
    def _run():
        result = _service().calendarList().list().execute()
        return [
            {"id": c["id"], "summary": c["summary"], "primary": c.get("primary", False)}
            for c in result.get("items", [])
        ]
    return await loop.run_in_executor(None, _run)


async def list_events(
    calendar_id: str = "primary",
    max_results: int = 10,
    time_min: str = None,
) -> list:
    loop = asyncio.get_running_loop()
    def _run():
        t_min = time_min or datetime.now(timezone.utc).isoformat()
        result = _service().events().list(
            calendarId=calendar_id,
            timeMin=t_min,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = []
        for e in result.get("items", []):
            start = e.get("start", {})
            end = e.get("end", {})
            events.append({
                "id": e["id"],
                "summary": e.get("summary"),
                "start": start.get("dateTime") or start.get("date"),
                "end": end.get("dateTime") or end.get("date"),
                "allDay": "date" in start,
                "colorId": e.get("colorId"),
                "location": e.get("location"),
                "description": (e.get("description") or "")[:200],
            })
        return events
    return await loop.run_in_executor(None, _run)


async def create_event(
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    description: str = None,
    location: str = None,
    color_id: str = None,
    recurrence: str | None = None,
) -> dict:
    loop = asyncio.get_running_loop()
    def _run():
        body = {
            "summary": summary,
            "start": _date_field(start),
            "end": _date_field(end),
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if color_id is not None:
            body["colorId"] = color_id
        if recurrence:
            rule = recurrence if recurrence.startswith("RRULE:") else f"RRULE:{recurrence}"
            body["recurrence"] = [rule]
        event = _service().events().insert(calendarId=calendar_id, body=body).execute()
        return {"id": event["id"], "summary": event.get("summary"), "htmlLink": event.get("htmlLink")}
    return await loop.run_in_executor(None, _run)


async def update_event(
    event_id: str,
    calendar_id: str = "primary",
    summary: str = None,
    start: str = None,
    end: str = None,
    description: str = None,
    color_id: str = None,
    recurrence: str | None = None,
) -> dict:
    loop = asyncio.get_running_loop()
    def _run():
        svc = _service()
        event = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
        if summary is not None:
            event["summary"] = summary
        if start is not None:
            event["start"] = _date_field(start)
        if end is not None:
            event["end"] = _date_field(end)
        if description is not None:
            event["description"] = description
        if color_id is not None:
            event["colorId"] = color_id
        if recurrence is not None:
            rule = recurrence if recurrence.startswith("RRULE:") else f"RRULE:{recurrence}"
            event["recurrence"] = [rule]
        updated = svc.events().update(calendarId=calendar_id, eventId=event_id, body=event).execute()
        return {"id": updated["id"], "summary": updated.get("summary"), "colorId": updated.get("colorId")}
    return await loop.run_in_executor(None, _run)


async def delete_event(event_id: str, calendar_id: str = "primary") -> dict:
    loop = asyncio.get_running_loop()
    def _run():
        _service().events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return {"deleted": True, "event_id": event_id}
    return await loop.run_in_executor(None, _run)
