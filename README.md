# mcp-pdp-resolver

MCP-native PDP resolver: turn messy product queries into canonical product-detail URLs + structured fields (price, stock, confidence) using [Firecrawl](https://firecrawl.dev) under the hood. Built for **MCP client authors** and **agent builders**.

---

## Architecture

Two entrypoints converge on the same core pipeline in `resolver.py`:

```
┌──────────────────────────────────────────────────────────────────┐
│  (A) MCP path — demo_agent.py                    PRIMARY DEMO   │
│                                                                  │
│  CLI query                                                       │
│    → LangChain ChatOpenAI (gpt-4o)                               │
│      → MCPAgent                                                  │
│        → MCPClient  ← StripNullToolArgumentsMiddleware           │
│          → stdio subprocess  ← mcp.json                          │
│            → mcp_server.py (FastMCP)                             │
│              → resolver.py  → Firecrawl API                      │
│                                                                  │
│  Requires: FIRECRAWL_API_KEY + OPENAI_API_KEY                    │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  (B) Web path — server.py + web/             OPTIONAL SIDECAR   │
│                                                                  │
│  Browser → aiohttp (localhost:5001)                              │
│    POST /api/run                                                 │
│      → resolver.run_resolver()  ← reads local_files/context.json │
│        → resolver.py  → Firecrawl API                            │
│                                                                  │
│  ⚠ Does NOT use demo_agent.py, MCPAgent, or mcp-use.            │
│    Direct call to the resolver — no LLM, no MCP transport.       │
│    Visual way to exercise the same core pipeline without          │
│    full LLM + MCP setup.                                         │
│                                                                  │
│  Requires: FIRECRAWL_API_KEY only                                │
└──────────────────────────────────────────────────────────────────┘
```

## mcp-use in this repo

**Path A only** — [`mcp-use`](https://pypi.org/project/mcp-use/) `>=1.7`.

| What | Where |
|------|-------|
| `MCPAgent` + `MCPClient` driving `mcp.json` | `demo_agent.py` |
| `StripNullToolArgumentsMiddleware` on the client (strips LLM-emitted `null` args before `tools/call`) | `resolver.py` |

FastMCP keeps the server as a small stdio tool host for standard MCP clients; **mcp-use** is intentionally used on the agent/client side only.

---

## Quickstart — Path A: MCP + mcp-use (primary demo)

```bash
# 1. Clone & install
git clone <repo-url> && cd mcp-pdp-resolver
pip install -r requirements.txt

# 2. Configure secrets
cp .env.example .env
# → set FIRECRAWL_API_KEY and OPENAI_API_KEY

# 3. Run the demo agent (cwd must be repo root)
python demo_agent.py "navy chinos from Bonobos"
```

**What happens:** `demo_agent.py` → `ChatOpenAI` (gpt-4o) → `MCPAgent` → `MCPClient` (with `StripNullToolArgumentsMiddleware`) → spawns `mcp_server.py` as a **stdio subprocess** via `mcp.json` → tool calls resolve the query → JSON result to stdout.

> `**mcp.json` cwd:** must be repo root. `demo_agent.py` resolves script paths relative to its own location and injects real API keys from `.env` into the subprocess environment at runtime — placeholder values in `mcp.json` are never sent to Firecrawl.

---

## Quickstart — Path B: Web UI (optional sidecar)

```bash
# Only FIRECRAWL_API_KEY needed in .env
python server.py
# → http://localhost:5001
```


| Endpoint         | Method | Description                                                     |
| ---------------- | ------ | --------------------------------------------------------------- |
| `/api/run`       | POST   | Start resolver job → `{ "job_id": "...", "status": "running" }` |
| `/api/jobs/{id}` | GET    | Poll job → `{ "status": "done"|"error", "result": {...} }`      |
| `/api/context`   | GET    | Return `local_files/context.json` contents                      |


`POST /api/run` calls `resolver.run_resolver()` directly against `local_files/context.json`. No LLM, no MCP transport, no `demo_agent.py`.

---

## MCP Tools

Exposed by `mcp_server.py` (FastMCP server named `pdp-resolver`):


| Tool              | Signature                                | Contract                                                                                                                                       |
| ----------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `resolve_product` | `(query: str, retailer?: str) → dict`    | Full 5-step pipeline (SEARCH → MAP → FILTER → SCRAPE → VALIDATE). Returns canonical URL, price, stock, confidence.                             |
| `search_products` | `(query: str, max_results?: int) → dict` | Firecrawl search — ranked candidates with URL, title, description. Useful for exploration before committing to full resolution.                |
| `map_site`        | `(url: str, search?: str) → dict`        | Discover product URLs on a domain via Firecrawl map. Optional `search` keyword biases results. Pair with `validate_pdp` to confirm candidates. |
| `validate_pdp`    | `(url: str) → dict`                      | Heuristic URL classification + live scrape-extract. Returns `is_pdp`, confidence, page type, extracted product schema.                         |


---

## `StripNullToolArgumentsMiddleware`

When an LLM emits tool-call arguments, optional parameters frequently arrive as explicit `null` rather than being omitted entirely. Strict MCP servers — including FastMCP backed by Pydantic validation — reject these: they expect the key to be absent, not present-with-`null`. `StripNullToolArgumentsMiddleware` (defined in `resolver.py`, applied in `demo_agent.py`) intercepts every `tools/call` at the `**MCPClient` level** and recursively strips any key whose value is `None` before the request reaches the server. It lives on the client — not duplicated on the agent — so it fires exactly once per tool call regardless of which agent drives the session.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest                         # unit + mocked tests
pytest -m integration          # live Firecrawl tests (needs real FIRECRAWL_API_KEY)
```

Config: see `pytest.ini` — `asyncio_mode = auto`, test discovery in `tests/`.

Test files:


| File                            | Scope                                                       |
| ------------------------------- | ----------------------------------------------------------- |
| `test_resolver_helpers.py`      | URL classification, tracking-param stripping, name cleaning |
| `test_resolver_pipeline.py`     | 5-step pipeline with mocked Firecrawl                       |
| `test_validate_and_repair.py`   | Schema validation + repair logic                            |
| `test_mcp_server_tools.py`      | FastMCP tool contracts                                      |
| `test_mcp_json_launch.py`       | `mcp.json` subprocess launch                                |
| `test_integration_firecrawl.py` | Live Firecrawl calls (marker: `integration`)                |


---

## Design Notes


| Decision                                      | Rationale                                                                                                                                                                                                                                                                        |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **stdio transport** (not HTTP/SSE)            | stdio is the default MCP transport and maximises host compatibility — any MCP client that can spawn a subprocess works out of the box. No port management, no CORS.                                                                                                              |
| **Middleware on `MCPClient`, not `MCPAgent`** | Single enforcement point. Every tool call passes through the client regardless of which agent (or direct script) initiates it. No duplication risk.                                                                                                                              |
| **Runtime env injection into `mcp.json`**     | MCP's stdio transport inherits a minimal safe-list of env vars (`HOME`, `PATH`, `SHELL`). `demo_agent.py` reads real keys from `os.environ` and patches them into the server config dict before spawning the subprocess. Placeholder values in `mcp.json` never reach Firecrawl. |
| **`resolver.py` as shared core**                | Both entrypoints (MCP tools and web API) call the same pipeline functions. Zero logic duplication between paths A and B.                                                                                                                                                         |
| **Concurrency cap (`Semaphore(3)`)**          | `run_resolver()` resolves products in parallel but limits concurrent Firecrawl calls to 3 to stay under rate limits.                                                                                                                                                             |
| **`sys.executable` for subprocess**           | `demo_agent.py` replaces the `command` in `mcp.json` with `sys.executable` so the subprocess always uses the same Python / venv that launched the agent.                                                                                                                         |


---

## Limitations

- **External API dependency** — Firecrawl availability and rate limits directly affect reliability. Scrape timeout: 30s. Web UI job timeout: 300s.
- **PLP fallback** — When no PDP candidate survives filtering, the pipeline returns the best category/listing URL it found. `url_type` distinguishes `"pdp"` vs `"plp"`; consumers must handle both.
- **LLM non-determinism (Path A only)** — The agent path routes through GPT-4o. Identical queries may yield different tool-call sequences or argument phrasing across runs.
- **Heuristic URL classification** — PLP detection uses regex on URL paths (`/category/`, `/collection/`, etc.). Retailers with non-standard URL schemes may be misclassified.
- **No auth/session support** — Firecrawl scrapes are unauthenticated. Paywalled or login-gated product pages will fail extraction.
- **No caching** — Every run hits Firecrawl. Repeated queries for the same product consume API credits each time.

