"""
server.py — MCP PDP Resolver server

Serves the web UI and provides API endpoints for the resolver pipeline.

Usage:
    python server.py
    → Open http://localhost:5001

API:
    POST /api/run          → { "job_id": "...", "status": "running" }
    GET  /api/jobs/:id     → { "job_id": "...", "status": "running"|"done"|"error",
                               "phase": "...", "result": {...}|null, "error": "..."|null }
    GET  /api/context      → context.json contents
"""

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from aiohttp import web

logger = logging.getLogger("server")

ROOT = Path(__file__).parent
WEB_DIR = ROOT / "web"
LOCAL_DIR = ROOT / "local_files"
CONTEXT_FILE = LOCAL_DIR / "context.json"

_DEFAULT_CONTEXT = {
    "upcoming_events": [],
    "products": [],
    "fallback_recommendation": {
        "resolved": [],
        "summary": (
            "No resolver result yet — add your FIRECRAWL_API_KEY to .env and run again."
        ),
    },
}


def _ensure_context_file() -> None:
    """Create local_files/context.json with defaults if it doesn't exist."""
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    if not CONTEXT_FILE.exists():
        CONTEXT_FILE.write_text(json.dumps(_DEFAULT_CONTEXT, indent=2))

# Job store: job_id → { status, phase, result, error, created_at }
_jobs: dict = {}
_JOB_TTL_SECS = 3600  # evict completed jobs after 1 hour

# Enforce one run at a time
_agent_running = False


def _evict_stale_jobs() -> None:
    """Remove completed/errored jobs older than _JOB_TTL_SECS."""
    now = time.monotonic()
    stale = [
        jid for jid, j in _jobs.items()
        if j["status"] in ("done", "error")
        and now - j.get("created_at", now) > _JOB_TTL_SECS
    ]
    for jid in stale:
        del _jobs[jid]

try:
    from resolver import run_resolver, SERVER_JOB_TIMEOUT, SERVER_PORT
    _AGENT_OK = True
    _AGENT_ERR = ""
except Exception as exc:
    _AGENT_OK = False
    _AGENT_ERR = str(exc)          # kept for the local console only — never sent to clients
    SERVER_JOB_TIMEOUT = 300
    SERVER_PORT = 5001


async def handle_index(request):
    return web.FileResponse(WEB_DIR / "index.html")


async def handle_context(request):
    if not CONTEXT_FILE.exists():
        return web.json_response(_DEFAULT_CONTEXT)
    try:
        data = json.loads(CONTEXT_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return web.json_response({"error": "Failed to read context data."}, status=500)
    return web.json_response(data)


_PHASE_TIMELINE = [
    (0,   "Resolving product URL…"),
    (10,  "Extracting product data…"),
    (40,  "Validating schema…"),
    (60,  "Complete."),
]


async def _phase_ticker(job_id: str):
    """Advances the job phase label on a fixed timeline while the agent runs."""
    start = asyncio.get_running_loop().time()
    for delay_s, label in _PHASE_TIMELINE:
        now = asyncio.get_running_loop().time()
        wait = delay_s - (now - start)
        if wait > 0:
            await asyncio.sleep(wait)
        job = _jobs.get(job_id)
        if not job or job["status"] != "running":
            return
        job["phase"] = label


async def _run_job(job_id: str):
    """Background task: runs run_resolver() and updates the job store."""
    global _agent_running
    job = _jobs[job_id]
    ticker = asyncio.get_running_loop().create_task(_phase_ticker(job_id))
    try:
        result = await asyncio.wait_for(run_resolver(), timeout=SERVER_JOB_TIMEOUT)
        job["status"] = "done"
        job["phase"] = "Complete"
        job["result"] = result
    except asyncio.TimeoutError:
        job["status"] = "error"
        job["phase"] = "Timed out"
        job["error"] = f"Agent timed out after {SERVER_JOB_TIMEOUT}s"
    except SystemExit:
        job["status"] = "error"
        job["phase"] = "Configuration error"
        job["error"] = "Agent configuration error"
    except Exception as exc:
        job["status"] = "error"
        job["phase"] = "Failed"
        job["error"] = str(exc)
    finally:
        ticker.cancel()
        _agent_running = False


async def handle_run(request):
    global _agent_running

    if not _AGENT_OK:
        logger.error("Agent unavailable: %s", _AGENT_ERR)
        return web.json_response(
            {"error": "Agent unavailable: resolver failed to load. Check server logs."},
            status=500,
        )

    if _agent_running:
        # Return the existing running job so the client can poll it
        running = next(
            (jid for jid, j in _jobs.items() if j["status"] == "running"), None
        )
        if running:
            return web.json_response({"job_id": running, "status": "running"}, status=200)
        return web.json_response({"error": "Agent is already running"}, status=429)

    _evict_stale_jobs()

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "phase": "Starting…",
        "result": None,
        "error": None,
        "created_at": time.monotonic(),
    }
    _agent_running = True

    asyncio.get_running_loop().create_task(_run_job(job_id))

    return web.json_response({"job_id": job_id, "status": "running"})


async def handle_job_status(request):
    job_id = request.match_info["job_id"]
    job = _jobs.get(job_id)
    if not job:
        return web.json_response({"error": "Job not found"}, status=404)
    return web.json_response(job)


async def handle_web_file(request):
    name = request.match_info["name"]
    # Resolve both paths before comparing so that any encoded traversal sequences
    # (%2F, ..) are normalised by the OS before the containment check.
    resolved = (WEB_DIR / name).resolve()
    if resolved.is_file() and resolved.is_relative_to(WEB_DIR.resolve()):
        return web.FileResponse(resolved)
    raise web.HTTPNotFound()


def create_app():
    app = web.Application()
    app.router.add_get("/api/context", handle_context)
    app.router.add_post("/api/run", handle_run)
    app.router.add_get("/api/jobs/{job_id}", handle_job_status)
    app.router.add_get("/", handle_index)
    app.router.add_get("/{name}", handle_web_file)
    return app


if __name__ == "__main__":
    _ensure_context_file()
    print("\nMCP PDP Resolver")
    print(f"   → http://localhost:{SERVER_PORT}")
    if not _AGENT_OK:
        print(f"   ⚠ Agent not available: {_AGENT_ERR}")
        print("     The /api/run endpoint will return errors.")
        print("     The UI will use the fallback recommendation.\n")
    else:
        print("   ✓ Agent loaded — /api/run will trigger the real pipeline\n")
    web.run_app(create_app(), host="127.0.0.1", port=SERVER_PORT, print=None)
