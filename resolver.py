"""
resolver.py — PDP resolution utilities

Self-contained helpers for:
- URL classification (PLP vs PDP)
- Tracking-param stripping
- Item-name normalisation
- Firecrawl direct API calls (search / map / scrape-extract)
- 5-step product URL resolution pipeline
- JSON extraction and schema validation
- MCP config loading
- MCP middleware (strip null tool arguments)
- Startup preflight checks
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp
from dotenv import load_dotenv
from mcp import types as mcp_types
from mcp_use.client.middleware.middleware import Middleware, MiddlewareContext

load_dotenv()

logger = logging.getLogger("resolver")

ROOT = Path(__file__).parent
MCP_CONFIG = ROOT / "mcp.json"

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_PLP_PATH_RE = re.compile(
    r"/(?:o|c|category|categories|collection|collections|"
    r"search|browse|listing|list|plp|grid)(?:/|$)",
    re.IGNORECASE,
)

_TRACKING_PARAMS = frozenset({
    "srsltid", "gclid", "fbclid", "msclkid", "dclid", "twclid",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_", "tag", "psc",
})


def is_plp_url(url: str) -> bool:
    """Return True when the URL is clearly a category/listing page, not a PDP."""
    if not url:
        return False
    try:
        return bool(_PLP_PATH_RE.search(urlparse(url).path))
    except Exception:
        return False


def strip_tracking_params(url: str) -> str:
    """Remove known tracking query parameters, preserve variant selectors."""
    try:
        parsed = urlparse(url)
        clean_qs = [
            (k, v) for k, v in parse_qsl(parsed.query)
            if k not in _TRACKING_PARAMS and not k.startswith("utm_")
        ]
        return urlunparse(parsed._replace(query=urlencode(clean_qs)))
    except Exception:
        return url


# ---------------------------------------------------------------------------
# Item-name cleaning helpers
# ---------------------------------------------------------------------------

_SYMBOL_RE = re.compile(r"[®™©]")
_HYPHEN_RE = re.compile(r"[-–—]")
_COLOR_RE = re.compile(
    r"\b(?:blue|red|green|navy|black|white|grey|gray|beige|tan|khaki|olive|"
    r"charcoal|cream|ivory|burgundy|maroon|teal|coral|pink|brown|bright|dark|"
    r"light|yellow|orange|purple|indigo)\b",
    re.IGNORECASE,
)
_SIZE_RE = re.compile(
    r"\b(?:xs|small|medium|large|xl|xxl|xxxl|2xl|3xl)\b",
    re.IGNORECASE,
)

_CLOTHING_TERMS = frozenset({
    "blazer", "jacket", "coat", "shirt", "pants", "trousers", "shoes",
    "boots", "sweater", "cardigan", "vest", "tie", "suit", "chinos",
    "jeans", "shorts", "polo", "tee", "sneakers", "loafers", "oxfords",
    "belt", "parka", "hoodie", "overcoat", "topcoat",
})

_RETAILER_DOMAINS: dict[str, str] = {
    "bonobos": "bonobos.com",
    "j.crew": "jcrew.com",
    "j crew": "jcrew.com",
    "jcrew": "jcrew.com",
    "uniqlo": "uniqlo.com",
    "everlane": "everlane.com",
    "nordstrom": "nordstrom.com",
    "jos. a. bank": "josbank.com",
    "jos a bank": "josbank.com",
    "josbank": "josbank.com",
    "banana republic": "bananarepublic.gap.com",
    "gap": "gap.com",
    "zara": "zara.com",
    "h&m": "hm.com",
    "brooks brothers": "brooksbrothers.com",
    "charles tyrwhitt": "ctshirts.com",
    "cole haan": "colehaan.com",
    "taylor stitch": "taylorstitch.com",
    "amazon": "amazon.com",
    "macy's": "macys.com",
    "macys": "macys.com",
    "target": "target.com",
    "asos": "asos.com",
}

_FILTER_KEEP_RE = re.compile(r"/(?:products?|p|item|shop)/", re.IGNORECASE)
_FILTER_DROP_RE = re.compile(r"/(?:o|c|category|collection|search|l)/", re.IGNORECASE)

MAX_PDP_CANDIDATES = 3
FC_TIMEOUT_DEFAULT = 20
FC_TIMEOUT_SCRAPE = 30
SERVER_JOB_TIMEOUT = 300
SERVER_PORT = 5001

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "product_name": {"type": "string"},
        "canonical_url": {"type": "string"},
        "price_usd": {"type": "number"},
        "in_stock": {"type": "boolean"},
        "page_type": {
            "type": "string",
            "enum": ["pdp", "plp", "other"],
        },
    },
}


def _clean_item_name(name: str) -> str:
    """Strip colors, sizes, symbols, hyphens → core item name for search."""
    if "," in name:
        name = name[: name.rfind(",")]
    name = _SYMBOL_RE.sub("", name)
    name = _COLOR_RE.sub("", name)
    name = _SIZE_RE.sub("", name)
    name = _HYPHEN_RE.sub(" ", name)
    name = re.sub(r"\s*,\s*", " ", name)
    return " ".join(name.split()).strip()


def _short_item_name(cleaned: str) -> str:
    """Extract a 2-3 word core name for the MAP search parameter."""
    words = cleaned.lower().split()
    for i, w in enumerate(words):
        if w in _CLOTHING_TERMS:
            start = max(0, i - 2)
            return " ".join(words[start : i + 1])
    return " ".join(words[-3:]) if len(words) > 3 else cleaned


def _domain_for_retailer(url: str, source: str) -> str:
    """Extract domain from URL, or guess from retailer source name."""
    if url:
        try:
            netloc = urlparse(url).netloc
            if netloc:
                return netloc
        except Exception:
            pass
    src = (source or "").strip().lower()
    for key, domain in _RETAILER_DOMAINS.items():
        if key in src:
            return domain
    slug = re.sub(r"[^a-z0-9]", "", src)
    return f"{slug}.com" if slug else ""


# ---------------------------------------------------------------------------
# Firecrawl direct API helpers (no MCP, no LLM)
# ---------------------------------------------------------------------------

async def _fc_request(
    fc_key: str,
    endpoint: str,
    body: dict,
    timeout: int = 20,
    *,
    session: aiohttp.ClientSession | None = None,
) -> dict:
    """POST to a Firecrawl v1 endpoint and return the parsed JSON response.

    When *session* is supplied the caller owns its lifecycle and connections are
    reused across calls (preferred for pipeline use).  When omitted a temporary
    session is created for this single request (safe for standalone callers).
    """
    url = f"https://api.firecrawl.dev/v1/{endpoint}"
    headers = {
        "Authorization": f"Bearer {fc_key}",
        "Content-Type": "application/json",
    }
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    async def _post(s: aiohttp.ClientSession) -> dict:
        async with s.post(url, json=body, headers=headers, timeout=client_timeout) as resp:
            resp.raise_for_status()
            return await resp.json()

    if session is not None:
        return await _post(session)
    async with aiohttp.ClientSession() as s:
        return await _post(s)


async def fc_search(
    fc_key: str,
    query: str,
    limit: int = 5,
    *,
    session: aiohttp.ClientSession | None = None,
) -> list[dict]:
    data = await _fc_request(fc_key, "search", {"query": query, "limit": limit}, session=session)
    return data.get("data", [])


async def fc_map(
    fc_key: str,
    url: str,
    search: str = "",
    *,
    session: aiohttp.ClientSession | None = None,
) -> list[str]:
    body: dict = {"url": url}
    if search:
        body["search"] = search
    data = await _fc_request(fc_key, "map", body, session=session)
    return data.get("links", [])


async def fc_scrape_extract(
    fc_key: str,
    url: str,
    *,
    session: aiohttp.ClientSession | None = None,
) -> dict | None:
    data = await _fc_request(
        fc_key,
        "scrape",
        {"url": url, "formats": ["extract"], "extract": {"schema": _EXTRACT_SCHEMA}},
        timeout=FC_TIMEOUT_SCRAPE,
        session=session,
    )
    return data.get("data", {}).get("extract")


# ---------------------------------------------------------------------------
# 5-step product URL pipeline  (SEARCH → MAP → FILTER → SCRAPE → result)
# ---------------------------------------------------------------------------

async def resolve_product_url(
    item_name: str,
    brand: str,
    source: str,
    current_url: str,
    department: str,
    fc_key: str,
) -> tuple[str, str, dict | None]:
    """
    Returns (url, url_type, extract) where url_type is ``"pdp"`` or ``"plp"``.
    ``extract`` is the structured data dict from the confirming scrape (or None).
    Never returns an empty URL when *any* URL was discovered at any stage.

    A single ``aiohttp.ClientSession`` is created for the duration of the
    pipeline so TCP connections are reused across the 5 steps.
    """
    async with aiohttp.ClientSession() as session:
        return await _resolve_product_url_with_session(
            item_name, brand, source, current_url, department, fc_key, session
        )


async def _resolve_product_url_with_session(
    item_name: str,
    brand: str,
    source: str,
    current_url: str,
    department: str,
    fc_key: str,
    session: aiohttp.ClientSession,
) -> tuple[str, str, dict | None]:
    domain = _domain_for_retailer(current_url, source)
    core_name = _clean_item_name(item_name)
    short_name = _short_item_name(core_name)

    best_plp = current_url or ""

    # Step 1 — SEARCH
    site_clause = f"site:{domain}" if domain else ""
    search_query = f"{department} {core_name} {site_clause}".strip()
    logger.info("Step 1 — SEARCH: %s", search_query)

    search_url = ""
    try:
        results = await fc_search(fc_key, search_query, limit=3, session=session)
        if results:
            search_url = (results[0].get("url") or "").strip()
            if search_url and not best_plp:
                best_plp = search_url
    except Exception as exc:
        logger.warning("Step 1 failed: %s", exc)

    if not search_url:
        logger.warning("Step 1 yielded no results")
        return (strip_tracking_params(best_plp), "plp", None) if best_plp else ("", "plp", None)

    # Step 2 — MAP
    logger.info("Step 2 — MAP: %s  search=%r", search_url, short_name)
    mapped_urls: list[str] = []
    try:
        mapped_urls = await fc_map(fc_key, search_url, search=short_name, session=session)
    except Exception as exc:
        logger.warning("Step 2 failed: %s", exc)

    # Step 3 — FILTER
    candidates: list[str] = []
    for u in mapped_urls:
        path = urlparse(u).path
        if _FILTER_DROP_RE.search(path):
            continue
        if _FILTER_KEEP_RE.search(path):
            candidates.append(u)
    candidates = candidates[:MAX_PDP_CANDIDATES]
    logger.info(
        "Step 3 — FILTER: %d PDP candidates from %d mapped URLs",
        len(candidates), len(mapped_urls),
    )

    if not candidates:
        plp = strip_tracking_params(best_plp or search_url)
        return (plp, "plp", None)

    # Step 4 — SCRAPE + VALIDATE
    for i, candidate in enumerate(candidates):
        logger.info("Step 4 — SCRAPE %d/%d: %s", i + 1, len(candidates), candidate)
        try:
            extract = await fc_scrape_extract(fc_key, candidate, session=session)
            if not extract:
                continue
            page_type = (extract.get("page_type") or "").lower()
            price = extract.get("price_usd")
            in_stock = extract.get("in_stock")

            if page_type != "pdp":
                logger.info("  page_type=%s, skipping", page_type)
                continue
            if price is None:
                logger.info("  no price, skipping")
                continue
            if in_stock is False:
                logger.info("  out of stock, skipping")
                continue

            final = strip_tracking_params(extract.get("canonical_url") or candidate)
            logger.info("  CONFIRMED PDP: %s", final)
            return (final, "pdp", extract)
        except Exception as exc:
            logger.warning("  scrape failed: %s", exc)

    plp = strip_tracking_params(best_plp or search_url)
    return (plp, "plp", None)


# ---------------------------------------------------------------------------
# JSON extraction — handles all common LLM wrapping patterns
# ---------------------------------------------------------------------------

def extract_json(text: str) -> dict:
    """
    Extract a JSON object from agent output that may be wrapped in:
    - Plain JSON
    - ```json ... ``` fences
    - ``` ... ``` fences
    - Prose before/after the JSON block
    Raises json.JSONDecodeError if no valid JSON object is found.
    """
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        return json.loads(fence_match.group(1))

    # Try non-greedy first to avoid over-matching when multiple JSON objects
    # or stray braces are present; fall back to greedy for nested objects.
    for pattern in (r"\{.*?\}", r"\{.*\}"):
        brace_match = re.search(pattern, text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                continue

    raise json.JSONDecodeError("No JSON object found in agent output", text, 0)


# ---------------------------------------------------------------------------
# Recommendation schema validation + repair
# ---------------------------------------------------------------------------

REQUIRED_KEYS = {"gap", "buy", "reasoning"}
BUY_KEYS = {"item", "brand", "price_usd", "source", "url"}


def _coerce_number(value: object, field: str) -> float:
    """Convert a value to float, stripping currency symbols if needed."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.\-]", "", value)
        if cleaned:
            return float(cleaned)
    raise ValueError(f"Field '{field}' is not a valid number: {value!r}")


def validate_and_repair(raw: dict) -> dict:
    """
    Validate the agent output dict and repair common issues.

    Returns a new dict — the input ``raw`` is not mutated.
    - Missing top-level keys -> raise ValueError with details
    - Numeric fields stored as strings -> coerce to float
    - Missing nested keys -> fill with safe defaults
    """
    missing = REQUIRED_KEYS - raw.keys()
    if missing:
        raise ValueError(f"Recommendation missing required keys: {missing}")

    out = {**raw}
    buy = {**out.get("buy", {})}

    for k in BUY_KEYS:
        if k not in buy:
            buy[k] = "" if k in ("url", "source", "brand", "item") else 0

    buy["price_usd"] = _coerce_number(buy["price_usd"], "buy.price_usd")

    out["buy"] = buy

    if "events_covered" not in out:
        out["events_covered"] = []
    if "total_wear_days" not in out or not isinstance(out["total_wear_days"], (int, float)):
        out["total_wear_days"] = 1

    return out


# ---------------------------------------------------------------------------
# MCP config — merge secrets from .env over mcp.json
# ---------------------------------------------------------------------------

_PLACEHOLDER_FIRECRAWL = frozenset(
    {"", "YOUR_FIRECRAWL_API_KEY", "your_firecrawl_key_here"}
)


def _strip_none_values(obj: object) -> object:
    """Recursively remove dict keys whose value is None."""
    if isinstance(obj, dict):
        return {k: _strip_none_values(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none_values(x) for x in obj]
    return obj


class StripNullToolArgumentsMiddleware(Middleware):
    """Strip None/null from tools/call args so optionals are omitted, not sent as null (strict schemas often reject explicit null)."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next,
    ) -> mcp_types.CallToolResult:
        params = context.params
        if params.arguments:
            cleaned = _strip_none_values(dict(params.arguments))
            if cleaned != params.arguments:
                context.params = params.model_copy(update={"arguments": cleaned})
        return await call_next(context)


def load_mcp_config() -> dict:
    """Load mcp.json and overlay FIRECRAWL_API_KEY from the environment when set.

    The key is injected into the ``pdp-resolver`` server block — the server
    name declared in mcp.json — so the config stays internally consistent.
    """
    cfg = json.loads(MCP_CONFIG.read_text())
    env_fc = (os.getenv("FIRECRAWL_API_KEY") or "").strip()
    if env_fc and env_fc not in _PLACEHOLDER_FIRECRAWL:
        server = cfg.setdefault("mcpServers", {}).setdefault("pdp-resolver", {})
        server.setdefault("env", {})["FIRECRAWL_API_KEY"] = env_fc

    return cfg


def _effective_firecrawl_key(cfg: dict) -> str:
    """Return the Firecrawl API key from the merged config, or '' if absent."""
    return (
        cfg.get("mcpServers", {})
        .get("pdp-resolver", {})
        .get("env", {})
        .get("FIRECRAWL_API_KEY", "")
        or ""
    ).strip()


# ---------------------------------------------------------------------------
# Startup preflight checks
# ---------------------------------------------------------------------------

def preflight_checks() -> None:
    """Fail fast with actionable messages before touching any network."""
    errors: list[str] = []

    if not MCP_CONFIG.exists():
        errors.append(f"mcp.json not found at {MCP_CONFIG}")
    else:
        merged = load_mcp_config()
        fc_key = _effective_firecrawl_key(merged)
        if fc_key in _PLACEHOLDER_FIRECRAWL:
            errors.append(
                "FIRECRAWL_API_KEY is not set. Add it to .env "
                "(non-placeholder value required)."
            )

    if errors:
        msg = "Resolver cannot start:\n" + "\n".join(f"  {i}. {e}" for i, e in enumerate(errors, 1))
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_resolver() -> dict:
    """
    Public entry point for server.py and CLI.

    Loads products from local_files/context.json, runs the 5-step URL
    resolution pipeline on each, and returns a structured result dict::

        {
            "resolved": [
                {
                    "item": str,
                    "brand": str,
                    "source": str,
                    "canonical_url": str,
                    "url_type": "pdp" | "plp",
                    "price_usd": float | None,
                    "in_stock": bool | None,
                    "product_name": str,
                }
            ],
            "summary": str,
        }
    """
    preflight_checks()

    context_path = ROOT / "local_files" / "context.json"
    if context_path.exists():
        try:
            context: dict = json.loads(context_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read context.json: {exc}") from exc
    else:
        context = {"products": []}

    products: list[dict] = context.get("products") or []
    if not products:
        return {
            "resolved": [],
            "summary": (
                "No products found in context.json. "
                "Add entries to local_files/context.json to resolve."
            ),
        }

    fc_key = _effective_firecrawl_key(load_mcp_config())

    _CONCURRENCY = 3  # limit parallel Firecrawl calls
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _resolve_one(product: dict) -> dict:
        item_name = (product.get("item") or "").strip()
        brand = (product.get("brand") or "").strip()
        source = (product.get("source") or "").strip()
        start_url = (product.get("url") or "").strip()
        department = (product.get("department") or "").strip()

        logger.info("Resolving: %s %s", brand, item_name)

        async with sem:
            resolved_url, url_type, extract = await resolve_product_url(
                item_name=item_name,
                brand=brand,
                source=source,
                current_url=start_url,
                department=department,
                fc_key=fc_key,
            )

        entry: dict = {
            "item": item_name,
            "brand": brand,
            "source": source,
            "canonical_url": resolved_url,
            "url_type": url_type,
            "price_usd": None,
            "in_stock": None,
            "product_name": None,
        }

        # Use the extract already obtained during the pipeline (no double-scrape).
        if extract:
            entry["price_usd"] = extract.get("price_usd")
            entry["in_stock"] = extract.get("in_stock")
            raw_name = extract.get("product_name")
            entry["product_name"] = raw_name or f"{brand} {item_name}".strip()
            canonical = extract.get("canonical_url")
            if canonical:
                entry["canonical_url"] = strip_tracking_params(canonical)

        if not entry["product_name"]:
            entry["product_name"] = f"{brand} {item_name}".strip() or item_name

        return entry

    resolved = list(await asyncio.gather(*[_resolve_one(p) for p in products]))

    total = len(resolved)
    pdp_count = sum(1 for r in resolved if r["url_type"] == "pdp")

    result = {
        "resolved": resolved,
        "summary": f"Resolved {pdp_count} of {total} product(s) to a confirmed PDP.",
    }

    # Persist for the CLI and for the web UI fallback reference.
    web_dir = ROOT / "web"
    web_dir.mkdir(exist_ok=True)
    (web_dir / "recommendation.json").write_text(json.dumps(result, indent=2))

    return result


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    async def _main() -> None:
        try:
            result = await run_resolver()
            print(json.dumps(result, indent=2))
        except (RuntimeError, FileNotFoundError, ValueError) as exc:
            print(f"\n❌  {exc}", file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            sys.exit(0)

    asyncio.run(_main())
