"""Sentinel Shared Brain — Mini App REST + WebSocket endpoints (Phases 4-5).

Mounts /api/brain/* on the existing Flask app. Auth is inherited from
bridge.py's `before_request` middleware (session_token required for
every /api/* path), so the routes themselves only worry about mapping
the validated session to a brain_store user_id.

POST /api/brain/threads/{id}/messages blocks during chat_turn (~30s
warm, longer cold). Phase 5's /ws/brain push gives every other surface
real-time message/thread events while the originating client still
sees the synchronous reply.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path

from flask import Blueprint, jsonify, request
from flask_sock import Sock

# Ensure metamcp-local repo root is importable so `openclaw.*` resolves.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from openclaw.brain_store import BrainStore, Thread  # noqa: E402
from openclaw.brain_wrapper import chat_turn_begin, chat_turn_finish  # noqa: E402
from openclaw.eventbus import listen_events  # noqa: E402
from openclaw.tokenizer import count_tokens  # noqa: E402


logger = logging.getLogger("sentinel.miniapp.brain")

SURFACE = "miniapp"
OWNER_USER = "azfar"

# Comet side-panel / browser surface (surface-unification step 1). The panel's
# turns route through the SHARED brain under this surface with their OWN thread —
# a distinct default_thread_name keeps them from fusing into the miniapp/DM
# "default" thread (get_active_thread keys conversations by (user_id, name)).
PANEL_SURFACE = "comet"
PANEL_THREAD_NAME = "comet"
PANEL_MAX_CHARS = 8000

# Constructed lazily so import-time DB connect failures don't kill bridge boot.
_store: BrainStore | None = None


def _get_store() -> BrainStore:
    global _store
    if _store is None:
        _store = BrainStore(token_counter=count_tokens)
    return _store


brain_bp = Blueprint("brain", __name__, url_prefix="/api/brain")


def _resolve_user(owner_id: int, session_info_fn) -> tuple[str, str] | tuple[None, str]:
    """Validate the request's session and return (user_id, surface_account)
    for brain_store. Returns (None, reason) if rejected."""
    tok = request.headers.get("X-Session-Token", "") or request.args.get("session", "")
    info = session_info_fn(tok)
    if not info:
        return None, "session_required"
    tg_id = info.get("tg_id")
    if tg_id != owner_id:
        return None, "access_denied"
    return OWNER_USER, str(tg_id)


def _thread_summary(store: BrainStore, t: Thread) -> dict:
    """Thread payload enriched with message count + last_message_at for the UI."""
    msgs = store.message_count(t.id)
    d = t.to_dict()
    d["message_count"] = msgs
    return d


def register(app, *, owner_id: int, session_info_fn, mirror_fn=None):
    """Mount this blueprint on the Flask app.  bridge.py calls this once
    on boot, passing its OWNER_ID + `_session_info` so we don't have a
    circular import.
    """

    @brain_bp.route("/threads", methods=["GET"])
    def list_threads():
        uid, acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": acct}), 401
        store = _get_store()
        include_archived = request.args.get("archived") == "1"
        threads = store.list_threads(user_id=uid, include_archived=include_archived)
        # Decorate with active marker (per-surface binding)
        try:
            active = store.get_active_thread(SURFACE, acct, user_id=uid)
            active_id = active.id
        except Exception as exc:
            logger.warning("active thread resolve failed: %s", exc)
            active_id = None
        return jsonify({
            "threads": [
                {**_thread_summary(store, t), "is_active": (t.id == active_id)}
                for t in threads
            ],
            "active_thread_id": str(active_id) if active_id else None,
        })

    @brain_bp.route("/threads", methods=["POST"])
    def create_thread():
        uid, acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": acct}), 401
        data = request.json or {}
        name = (data.get("name") or "").strip()
        kind = (data.get("kind") or "general").strip()
        switch = bool(data.get("switch", True))
        if not name:
            return jsonify({"error": "name required"}), 400
        if not (1 <= len(name) <= 40):
            return jsonify({"error": "name must be 1..40 chars"}), 400
        store = _get_store()
        try:
            t = store.create_thread(user_id=uid, name=name, kind=kind)
        except Exception as exc:
            return jsonify({"error": f"create failed: {exc}"}), 409
        if switch:
            store.set_active_thread(SURFACE, acct, t.id, user_id=uid)
        return jsonify(_thread_summary(store, t))

    @brain_bp.route("/threads/<thread_id>/name", methods=["POST"])
    def rename_thread(thread_id: str):
        uid, _acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": _acct}), 401
        try:
            tid = uuid.UUID(thread_id)
        except ValueError:
            return jsonify({"error": "bad thread_id"}), 400
        data = request.json or {}
        name = (data.get("name") or "").strip()
        if not (1 <= len(name) <= 40):
            return jsonify({"error": "name must be 1..40 chars"}), 400
        store = _get_store()
        t = store.get_thread(tid)
        if t is None or t.user_id != uid:
            return jsonify({"error": "thread not found"}), 404
        store.rename_thread(tid, name)
        return jsonify(_thread_summary(store, store.get_thread(tid)))

    @brain_bp.route("/active", methods=["GET"])
    def get_active():
        uid, acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": acct}), 401
        store = _get_store()
        t = store.get_active_thread(SURFACE, acct, user_id=uid)
        return jsonify(_thread_summary(store, t))

    @brain_bp.route("/active", methods=["POST"])
    def set_active():
        uid, acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": acct}), 401
        data = request.json or {}
        tid_raw = (data.get("thread_id") or "").strip()
        try:
            tid = uuid.UUID(tid_raw)
        except ValueError:
            return jsonify({"error": "bad thread_id"}), 400
        store = _get_store()
        t = store.get_thread(tid)
        if t is None or t.user_id != uid:
            return jsonify({"error": "thread not found"}), 404
        store.set_active_thread(SURFACE, acct, tid, user_id=uid)
        return jsonify({"ok": True, "thread": _thread_summary(store, t)})

    @brain_bp.route("/threads/<thread_id>/tools", methods=["GET"])
    def list_thread_tools(thread_id: str):
        """Return MetaMCP's tool inventory for the Default namespace,
        decorated with this thread's per-thread override state. The
        sidebar uses this to render per-thread toggles."""
        uid, _acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": _acct}), 401
        try:
            tid = uuid.UUID(thread_id)
        except ValueError:
            return jsonify({"error": "bad thread_id"}), 400
        store = _get_store()
        t = store.get_thread(tid)
        if t is None or t.user_id != uid:
            return jsonify({"error": "thread not found"}), 404
        # Pull the thread's overrides
        overrides = store.get_thread_tool_overrides(tid)
        # Read MetaMCP server + tool inventory via direct psql (Mini App pattern)
        import subprocess, json as _j
        ns = "0a83b85b-24ea-4491-b24b-17104bc9bba0"  # Default
        sql = f"""
            SELECT json_agg(json_build_object(
              'name', s.name,
              'type', s.type,
              'tools_total', (SELECT COUNT(*) FROM tools t WHERE t.mcp_server_uuid = s.uuid),
              'tools', (
                SELECT COALESCE(json_agg(json_build_object(
                  'tool_uuid', t.uuid::text,
                  'tool_name', t.name,
                  'description', t.description,
                  'mcp_server_uuid', t.mcp_server_uuid::text
                ) ORDER BY t.name), '[]'::json)
                FROM tools t WHERE t.mcp_server_uuid = s.uuid
              )
            ) ORDER BY s.name) FROM mcp_servers s
            JOIN namespace_server_mappings nsm ON nsm.mcp_server_uuid = s.uuid
            WHERE nsm.namespace_uuid = '{ns}';
        """
        r = subprocess.run(
            ["docker", "exec", "-i", "metamcp-pg", "psql", "-U", "metamcp_user",
             "-d", "metamcp_db", "-At", "-c", sql],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return jsonify({"error": f"psql failed: {r.stderr[:200]}"}), 500
        servers_raw = _j.loads(r.stdout.strip() or "[]") or []
        # Decorate every tool with this thread's enabled flag
        for s in servers_raw:
            for tool in (s.get("tools") or []):
                tu = tool.get("tool_uuid")
                if tu in overrides:
                    tool["enabled"] = overrides[tu]
                    tool["override"] = True
                else:
                    tool["enabled"] = True
                    tool["override"] = False
            on = sum(1 for tt in (s.get("tools") or []) if tt.get("enabled"))
            s["tools_enabled"] = on
            s["tools_disabled"] = (s.get("tools_total") or 0) - on
        return jsonify({"thread_id": str(tid), "servers": servers_raw})

    @brain_bp.route("/threads/<thread_id>/tools/toggle", methods=["POST"])
    def toggle_thread_tool(thread_id: str):
        """Toggle a single tool's enabled state for this thread only.
        Body: {tool_uuid, server_uuid, enabled}"""
        uid, _acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": _acct}), 401
        try:
            tid = uuid.UUID(thread_id)
        except ValueError:
            return jsonify({"error": "bad thread_id"}), 400
        store = _get_store()
        t = store.get_thread(tid)
        if t is None or t.user_id != uid:
            return jsonify({"error": "thread not found"}), 404
        data = request.json or {}
        try:
            tool_uuid = uuid.UUID(str(data.get("tool_uuid") or ""))
            server_uuid = uuid.UUID(str(data.get("server_uuid") or ""))
        except ValueError:
            return jsonify({"error": "bad uuids"}), 400
        enabled = bool(data.get("enabled"))
        store.set_thread_tool_override(tid, tool_uuid, server_uuid, enabled)
        return jsonify({"ok": True, "thread_id": str(tid),
                        "tool_uuid": str(tool_uuid), "enabled": enabled})

    @brain_bp.route("/skills", methods=["GET"])
    def list_skills():
        """Read OpenClaw's skill registry — both the published `skills/` dir
        and the local `plugin-skills/` dir. Read-only (skills aren't runtime
        toggleable from this side; OpenClaw decides what to load at boot)."""
        uid, _acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": _acct}), 401
        from _paths import OPENCLAW_DIR  # type: ignore
        skills_root = OPENCLAW_DIR / "skills"
        plugin_skills_root = OPENCLAW_DIR / "plugin-skills"
        out: list[dict] = []

        def _read_one(skill_dir, kind):
            meta_path = skill_dir / "_meta.json"
            doc_path = skill_dir / "SKILL.md"
            entry = {
                "id": skill_dir.name,
                "name": skill_dir.name,
                "kind": kind,
                "disabled": False,
            }
            if meta_path.is_file():
                try:
                    import json as _j
                    with open(meta_path, encoding="utf-8") as f:
                        meta = _j.load(f)
                    if meta.get("displayName"):
                        entry["name"] = meta["displayName"]
                    if meta.get("slug"):
                        entry["id"] = meta["slug"]
                    if meta.get("latest", {}).get("version"):
                        entry["version"] = meta["latest"]["version"]
                    if meta.get("owner"):
                        entry["owner"] = meta["owner"]
                except Exception as exc:
                    logger.warning("skill _meta.json parse failed for %s: %s", skill_dir.name, exc)
            if doc_path.is_file():
                try:
                    with open(doc_path, encoding="utf-8") as f:
                        head = f.read(400)
                    entry["doc_head"] = head.splitlines()[0][:120] if head else None
                except Exception:
                    pass
            return entry

        try:
            if skills_root.is_dir():
                for d in sorted(skills_root.iterdir()):
                    if d.is_dir() and not d.name.startswith("."):
                        out.append(_read_one(d, "published"))
            if plugin_skills_root.is_dir():
                for d in sorted(plugin_skills_root.iterdir()):
                    if d.is_dir() and not d.name.startswith("."):
                        out.append(_read_one(d, "plugin"))
        except Exception as exc:
            logger.exception("skill listing failed")
            return jsonify({"error": str(exc), "skills": []}), 500
        return jsonify({"skills": out})

    @brain_bp.route("/threads/<thread_id>/messages", methods=["GET"])
    def list_messages(thread_id: str):
        uid, acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": acct}), 401
        try:
            tid = uuid.UUID(thread_id)
        except ValueError:
            return jsonify({"error": "bad thread_id"}), 400
        store = _get_store()
        t = store.get_thread(tid)
        if t is None or t.user_id != uid:
            return jsonify({"error": "thread not found"}), 404
        # Use load_for_llm with a generous budget for display (NOT for routing).
        # We want raw rows for the UI, not the budget-pruned set.
        try:
            since = int(request.args.get("since", "0"))
        except ValueError:
            since = 0
        limit = min(int(request.args.get("limit", "200")), 500)
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(store.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM brain.messages
                 WHERE conv_id = %s AND id > %s
                 ORDER BY created_at ASC, id ASC
                 LIMIT %s
                """,
                (tid, since, limit),
            )
            rows = cur.fetchall()
        out = []
        for row in rows:
            out.append({
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "surface": row.get("surface"),
                "model": row.get("model"),
                "tokens_in": row.get("tokens_in"),
                "tokens_out": row.get("tokens_out"),
                "is_summary": row.get("is_summary", False),
                "pinned": row.get("pinned", False),
                "streaming_done": row.get("streaming_done", True),
                "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            })
        return jsonify({
            "thread_id": str(tid),
            "messages": out,
            "next_since": out[-1]["id"] if out else since,
        })

    @brain_bp.route("/upload", methods=["POST"])
    def upload_file():
        """Accept a multipart-form file upload from the /chat composer.
        Saves to <tempdir>/sentinel-chat-uploads/<user_id>/<file_id>__<filename>
        and returns {file_id, name, size, path}. The client stores file_id and
        sends it as part of the next message's `attachments` array.
        Real LLM integration (PDF text extraction, image vision input) is a
        follow-up; this just lands the file safely and gives the chat surface
        a stable handle to reference."""
        import tempfile, secrets as _sec
        uid, acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": acct}), 401
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "no file"}), 400
        # 50 MB cap per file (matches Telegram's bot limit, sensible default
        # for the chat-attachment use case; raise if needed later).
        MAX_BYTES = 50 * 1024 * 1024
        # Stream-check size cheaply via Content-Length where set.
        try:
            cl = int(request.content_length or 0)
        except Exception:
            cl = 0
        if cl and cl > MAX_BYTES:
            return jsonify({"error": "file too large (max 50 MB)"}), 413
        # Sanitize filename — strip path, keep basename only.
        safe_name = os.path.basename(f.filename).replace("\x00", "")[:200]
        file_id = _sec.token_urlsafe(16)
        dest_dir = Path(tempfile.gettempdir()) / "sentinel-chat-uploads" / str(uid)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{file_id}__{safe_name}"
        f.save(str(dest))
        # Post-write size check (Content-Length isn't authoritative on chunked
        # uploads). Cleans up oversized writes so we don't leak disk.
        try:
            size = dest.stat().st_size
        except OSError:
            size = 0
        if size > MAX_BYTES:
            try: dest.unlink()
            except OSError: pass
            return jsonify({"error": "file too large (max 50 MB)"}), 413
        return jsonify({
            "file_id": file_id,
            "name":    safe_name,
            "size":    size,
            "path":    str(dest),
        })

    @brain_bp.route("/threads/<thread_id>/messages", methods=["POST"])
    def post_message(thread_id: str):
        uid, acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": acct}), 401
        try:
            tid = uuid.UUID(thread_id)
        except ValueError:
            return jsonify({"error": "bad thread_id"}), 400
        store = _get_store()
        t = store.get_thread(tid)
        if t is None or t.user_id != uid:
            return jsonify({"error": "thread not found"}), 404
        data = request.json or {}
        text = (data.get("content") or "").strip()
        if not text:
            return jsonify({"error": "content required"}), 400
        if len(text) > 32_000:
            return jsonify({"error": "content too long (max 32k chars)"}), 413
        # enforce_tool: optional string like "mcp:Filesystem" or "skill:my-skill"
        enforce = None
        raw_enforce = (data.get("enforce_tool") or "").strip()
        if raw_enforce and ":" in raw_enforce:
            kind, name = raw_enforce.split(":", 1)
            kind = kind.lower().strip()
            name = name.strip()
            if kind in ("mcp", "skill") and name and len(name) <= 80:
                enforce = {"kind": kind, "name": name}
        # attachments: optional list of file_ids from POST /upload. We
        # resolve each one back to a local path under
        # <tempdir>/sentinel-chat-uploads/<uid>/<file_id>__<name>, extract
        # text via the shared attachment_processor (same module the TG
        # bot uses), and PREPEND the extracted content to the user
        # message. The LLM sees one combined turn with file content
        # spliced in below the user's actual question.
        attach_ids = data.get("attachments") or []
        if attach_ids:
            logger.info(
                "chat: thread=%s user=%s message includes %d attachment(s): %s",
                thread_id, uid, len(attach_ids), attach_ids[:5],
            )
            try:
                import tempfile as _tf
                from pathlib import Path as _Path
                # Import the shared extractor. It lives in the openclaw
                # subtree because both surfaces depend on it; bridge.py's
                # __init__.py already adds the parent metamcp-local dir to
                # sys.path. If that import path ever breaks, fall back to
                # logging instead of dropping the attachments silently.
                try:
                    from openclaw.tg_bot.attachment_processor import extract_from_file
                except ImportError:
                    import sys as _sys
                    _sys.path.insert(0, "/c/Users/azfar/metamcp-local")
                    from openclaw.tg_bot.attachment_processor import extract_from_file

                upload_dir = _Path(_tf.gettempdir()) / "sentinel-chat-uploads" / str(uid)
                blobs = []
                for fid in attach_ids:
                    # Files were saved as "<fid>__<safe_name>" by /upload
                    matches = list(upload_dir.glob(f"{fid}__*"))
                    if not matches:
                        logger.warning("attachment %s not found on disk for uid=%s", fid, uid)
                        continue
                    fpath = matches[0]
                    display = fpath.name.split("__", 1)[1] if "__" in fpath.name else fpath.name
                    blobs.append(extract_from_file(fpath, display_name=display))
                if blobs:
                    text = text + "\n\n" + "\n\n---\n\n".join(blobs)
            except Exception as e:
                logger.exception("attachment processing failed: %s", e)
        # Async turn (kills the Cloudflare 524): persist the user message +
        # reserve the assistant row synchronously, ACK with 202, then run the
        # slow OpenClaw turn in a background thread. The brain_events WS push
        # delivers the user row (message.new) and the finished reply
        # (message.complete) to every surface — the Mini App relies on that
        # instead of the POST body for the reply.
        try:
            begun = chat_turn_begin(
                thread_id=tid, user_msg=text, surface=SURFACE, store=store,
            )
        except Exception as e:
            logger.exception("chat_turn_begin failed")
            return jsonify({"error": str(e)}), 500

        def _run_turn() -> None:
            # Fresh store: the request's store object is fine to reuse
            # (per-call psycopg connections, no shared cursor) but a new one
            # keeps the worker fully independent of request teardown.
            worker_store = BrainStore(token_counter=count_tokens)
            try:
                result = chat_turn_finish(
                    thread_id=tid, user_msg=text,
                    assistant_message_id=begun["assistant_message_id"],
                    store=worker_store, enforce=enforce,
                )
            except Exception as e:
                logger.exception("chat_turn_finish failed in background: %s", e)
                # Surface the failure into the reserved row so the WS push
                # clears the client's "thinking" state instead of hanging.
                try:
                    worker_store.finalize(
                        message_id=begun["assistant_message_id"],
                        content=f"[bridge_error] {e}",
                    )
                except Exception:
                    logger.exception("failed to finalize errored assistant row")
                return
            if result.get("ok"):
                try:
                    worker_store.set_active_thread(SURFACE, acct, tid, user_id=uid)
                except Exception as e:
                    logger.warning("set_active_thread failed: %s", e)
                # Cross-surface mirror to the owner's TG DM (best-effort).
                if mirror_fn is not None:
                    reply_text = result.get("reply") or result.get("text") or ""
                    try:
                        mirror_fn(text, reply_text)
                    except Exception as e:
                        logger.warning("mirror_fn raised: %s", e)

        threading.Thread(
            target=_run_turn, daemon=True, name=f"chat-turn-{tid}",
        ).start()
        return jsonify({
            "accepted": True,
            "thread_id": str(tid),
            "user_message_id": begun["user_message_id"],
            "assistant_message_id": begun["assistant_message_id"],
        }), 202

    @brain_bp.route("/threads/<thread_id>/summarise_all", methods=["POST"])
    def summarise_thread(thread_id: str):
        """Eager-compress a thread to one summary row (Reset Context)."""
        uid, _acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": _acct}), 401
        try:
            tid = uuid.UUID(thread_id)
        except ValueError:
            return jsonify({"error": "bad thread_id"}), 400
        store = _get_store()
        t = store.get_thread(tid)
        if t is None or t.user_id != uid:
            return jsonify({"error": "thread not found"}), 404
        try:
            new_id = store.summarise_all(tid)
        except Exception as exc:
            logger.exception("summarise_all failed")
            return jsonify({"error": str(exc)}), 500
        if new_id is None:
            return jsonify({"ok": False, "reason": "nothing_to_summarise_or_lm_unavailable"}), 200
        return jsonify({"ok": True, "summary_message_id": new_id})

    @brain_bp.route("/threads/<thread_id>/archive", methods=["POST"])
    def archive_thread(thread_id: str):
        uid, acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": acct}), 401
        try:
            tid = uuid.UUID(thread_id)
        except ValueError:
            return jsonify({"error": "bad thread_id"}), 400
        store = _get_store()
        t = store.get_thread(tid)
        if t is None or t.user_id != uid:
            return jsonify({"error": "thread not found"}), 404
        store.archive(tid)
        # If we archived the active thread, rebind to default
        active = store.get_active_thread(SURFACE, acct, user_id=uid)
        return jsonify({"ok": True, "active_thread_id": str(active.id)})

    @brain_bp.route("/panel/chat", methods=["POST"])
    def panel_chat():
        """Comet side-panel chat — the unified-surface replacement for the
        unauth :8101 bridge's own `node openclaw` spawn (surface-unification
        step 1). Inherits the bridge's `before_request` auth (session token),
        then routes through brain_wrapper into the SHARED brain_store under the
        `comet` surface with its OWN thread (NOT fused into the DM/miniapp
        "default"). Synchronous: the panel hits 127.0.0.1 directly (no Cloudflare
        edge), so a ~30s blocking turn is fine and matches the panel's POST model.
        Additive — touches no existing route, auth gate, or the Mini App.
        Request:  {"message": "..."}   Response: {"ok", "reply", "thread_id", ...}
        """
        uid, acct = _resolve_user(owner_id, session_info_fn)
        if uid is None:
            return jsonify({"error": acct}), 401
        data = request.json or {}
        text = (data.get("message") or "").strip()
        if not text:
            return jsonify({"error": "message required"}), 400
        if len(text) > PANEL_MAX_CHARS:
            return jsonify({"error": f"message too long (>{PANEL_MAX_CHARS} chars)"}), 400
        store = _get_store()
        # The panel's own thread under the `comet` surface (auto-created + bound).
        t = store.get_active_thread(
            PANEL_SURFACE, acct, user_id=uid, default_thread_name=PANEL_THREAD_NAME,
        )
        tid = t.id
        try:
            begun = chat_turn_begin(
                thread_id=tid, user_msg=text, surface=PANEL_SURFACE, store=store,
            )
        except Exception as e:
            logger.exception("panel chat_turn_begin failed")
            return jsonify({"error": str(e)}), 500
        # chat_turn_finish never raises for turn failures (it synthesizes a
        # [bridge_error] reply + finalizes the row); run it inline (synchronous).
        result = chat_turn_finish(
            thread_id=tid, user_msg=text,
            assistant_message_id=begun["assistant_message_id"], store=store,
        )
        if result.get("ok"):
            try:
                store.set_active_thread(PANEL_SURFACE, acct, tid, user_id=uid)
            except Exception as e:
                logger.warning("panel set_active_thread failed: %s", e)
            return jsonify({
                "ok": True,
                "reply": result.get("reply") or "",
                "thread_id": str(tid),
                "assistant_message_id": result.get("assistant_message_id"),
                "media": result.get("media") or [],
                "model": result.get("model"),
            })
        return jsonify({
            "ok": False,
            "error": result.get("error") or "turn_failed",
            "error_detail": result.get("error_detail"),
            "reply": result.get("reply") or "",
            "thread_id": str(tid),
        }), 502

    app.register_blueprint(brain_bp)
    logger.info(
        "brain_routes registered: /api/brain/{threads,active,threads/<id>/messages,archive,panel/chat}"
    )

    # ── Phase 5: WebSocket push at /ws/brain ─────────────────────────
    sock = Sock(app)

    # Subscriber registry: ws → set of filter keys it's subscribed to.
    # Filter keys are e.g. "thread:<uuid>" or "user:<id>".
    # Single-process; a plain dict + lock is fine at single-owner scale.
    _subs_lock = threading.Lock()
    _subs: dict[object, set[str]] = {}

    def _filters_for_event(ev: dict) -> set[str]:
        keys: set[str] = set()
        tid = ev.get("thread_id")
        if tid:
            keys.add(f"thread:{tid}")
        uid = ev.get("user_id")
        if uid:
            keys.add(f"user:{uid}")
        # Wildcard channel — useful for "show all activity" admin views
        keys.add("*")
        return keys

    def _broadcast(ev: dict) -> None:
        keys = _filters_for_event(ev)
        msg = json.dumps(ev, default=str)
        with _subs_lock:
            targets = [ws for ws, filt in _subs.items() if filt & keys]
        for ws in targets:
            try:
                ws.send(msg)
            except Exception as exc:
                logger.warning("ws send failed (will drop): %s", exc)
                with _subs_lock:
                    _subs.pop(ws, None)

    def _listen_thread() -> None:
        """Background: tail brain_events Postgres channel, broadcast to WS subs."""
        # Build DSN once via a probe BrainStore so we honour the same env override
        dsn = BrainStore().dsn
        logger.info("ws/brain LISTEN thread starting (dsn=%s)", dsn)
        for ev in listen_events(dsn, poll_timeout=1.0):
            try:
                _broadcast(ev)
            except Exception as exc:
                logger.warning("broadcast failed: %s", exc)

    threading.Thread(target=_listen_thread, daemon=True, name="brain-ws-listen").start()

    @sock.route("/ws/brain")
    def ws_brain(ws):  # type: ignore[no-untyped-def]
        # Auth: ?session=<token>  (browsers can't set headers on the upgrade)
        tok = request.args.get("session", "")
        info = session_info_fn(tok)
        if not info or info.get("tg_id") != owner_id:
            try:
                ws.send(json.dumps({"kind": "error", "error": "auth_required"}))
            except Exception:
                pass
            return
        # Initial subscription set — empty; client must subscribe explicitly
        with _subs_lock:
            _subs[ws] = set()
        try:
            ws.send(json.dumps({"kind": "hello", "owner": OWNER_USER}))
            while True:
                raw = ws.receive(timeout=30)
                if raw is None:
                    # Heartbeat — keep the connection alive past the 30s nudge
                    try:
                        ws.send(json.dumps({"kind": "ping", "ts": time.time()}))
                    except Exception:
                        break
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    ws.send(json.dumps({"kind": "error", "error": "bad_json"}))
                    continue
                if "subscribe" in msg:
                    keys = msg.get("subscribe") or []
                    if isinstance(keys, list):
                        with _subs_lock:
                            _subs.setdefault(ws, set()).update(str(k) for k in keys)
                        ws.send(json.dumps({"kind": "subscribed", "keys": keys}))
                if "unsubscribe" in msg:
                    keys = msg.get("unsubscribe") or []
                    if isinstance(keys, list):
                        with _subs_lock:
                            _subs.setdefault(ws, set()).difference_update(str(k) for k in keys)
                        ws.send(json.dumps({"kind": "unsubscribed", "keys": keys}))
        finally:
            with _subs_lock:
                _subs.pop(ws, None)

    logger.info("brain_routes: /ws/brain registered (flask-sock)")
