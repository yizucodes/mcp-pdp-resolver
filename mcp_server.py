"""
mcp_server.py — FastMCP server exposing three PDP-resolver tools.

Tools:
  resolve_product(query, retailer?)   Full 5-step pipeline → canonical PDP URL + structured data
  search_products(query, max_results?) Return ranked product candidates with URLs
  validate_pdp(url)                   Confirm whether a URL is a real PDP, return confidence + schema

Run:
  python mcp_server.py
"""

import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from resolver import (
    fc_map,
    fc_scrape_extract,
    fc_search,
    is_plp_url,
    resolve_product_url,
    strip_tracking_params,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp_server")

# ---------------------------------------------------------------------------
# Fail fast if the API key is missing
# ---------------------------------------------------------------------------

_FIRECRAWL_KEY = (os.getenv("FIRECRAWL_API_KEY") or "").strip()
if not _FIRECRAWL_KEY:
    sys.exit(
        "ERROR: FIRECRAWL_API_KEY is not set. "
        "Add it to your .env file before starting the MCP server."
    )

# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("pdp-resolver")


# ---------------------------------------------------------------------------
# Tool 1 — resolve_product
# ---------------------------------------------------------------------------

@mcp.tool()
async def resolve_product(
    query: str,
    retailer: Optional[str] = None,
) -> dict:
    """
    Run the full 5-step PDP resolution pipeline for a natural-language product query.

    Steps: SEARCH → MAP → FILTER → SCRAPE → VALIDATE

    Args:
        query:    Natural-language product description, e.g. "navy chinos from Bonobos".
        retailer: Optional retailer name or domain hint, e.g. "bonobos" or "bonobos.com".
                  When omitted the retailer is inferred from the query.

    Returns a dict with:
        canonical_url   Cleaned PDP URL (or best PLP fallback).
        url_type        "pdp" | "plp"
        product_name    Extracted product name (may be empty on fallback).
        price_usd       Extracted price as a float, or null.
        in_stock        Boolean stock status, or null.
        confidence      "high" when url_type=="pdp" and price is present, else "low".
        query           Echo of the original query.
    """
    logger.info("resolve_product: query=%r retailer=%r", query, retailer)

    resolved_url, url_type, extract = await resolve_product_url(
        item_name=query,
        brand="",
        source=retailer or "",
        current_url="",
        department="",
        fc_key=_FIRECRAWL_KEY,
    )

    result: dict = {
        "query": query,
        "canonical_url": resolved_url,
        "url_type": url_type,
        "product_name": None,
        "price_usd": None,
        "in_stock": None,
        "confidence": "low",
    }

    # Use the extract already obtained during the pipeline (no double-scrape).
    if extract:
        result["product_name"] = extract.get("product_name")
        result["price_usd"] = extract.get("price_usd")
        result["in_stock"] = extract.get("in_stock")
        canonical = extract.get("canonical_url")
        if canonical:
            result["canonical_url"] = strip_tracking_params(canonical)
        if result["price_usd"] is not None:
            result["confidence"] = "high"

    return result


# ---------------------------------------------------------------------------
# Tool 2 — search_products
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_products(
    query: str,
    max_results: int = 5,
) -> dict:
    """
    Search for product candidates using Firecrawl and return ranked results.

    Useful when you want to explore options before committing to a full resolution.

    Args:
        query:       Natural-language search query, e.g. "black blazer under $300".
        max_results: Maximum number of results to return (1–10, default 5).

    Returns a dict with:
        query    Echo of the original query.
        results  List of candidates, each with: url, title, description.
        count    Number of results returned.
    """
    max_results = max(1, min(max_results, 10))
    logger.info("search_products: query=%r max_results=%d", query, max_results)

    raw = await fc_search(_FIRECRAWL_KEY, query, limit=max_results)

    candidates = [
        {
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "description": item.get("description", ""),
        }
        for item in raw
        if item.get("url")
    ]

    return {
        "query": query,
        "results": candidates,
        "count": len(candidates),
    }


# ---------------------------------------------------------------------------
# Tool 3 — map_site
# ---------------------------------------------------------------------------

@mcp.tool()
async def map_site(
    url: str,
    search: Optional[str] = None,
) -> dict:
    """
    Discover product URLs on a site using Firecrawl's map endpoint.

    Given a starting URL (typically a retailer homepage or category page),
    returns URLs found on that domain. Optionally filter by a search term
    to narrow results to relevant products.

    Useful when you have a retailer URL but need to find specific product
    pages before scraping. Pair with validate_pdp to confirm candidates.

    Args:
        url:    Starting URL to map, e.g. "https://bonobos.com".
        search: Optional keyword filter, e.g. "slim chinos". When provided,
                Firecrawl biases results toward pages matching this term.

    Returns a dict with:
        url         Echo of the starting URL.
        search      Echo of the search term (or null).
        links       List of discovered URLs.
        count       Number of URLs returned.
    """
    logger.info("map_site: url=%r search=%r", url, search)

    links = await fc_map(_FIRECRAWL_KEY, url, search=search or "", session=None)

    return {
        "url": url,
        "search": search,
        "links": links,
        "count": len(links),
    }


# ---------------------------------------------------------------------------
# Tool 4 — validate_pdp
# ---------------------------------------------------------------------------

@mcp.tool()
async def validate_pdp(url: str) -> dict:
    """
    Validate whether a URL is a real Product Detail Page (PDP).

    Combines heuristic URL classification with live Firecrawl scrape-extract
    to produce a confidence score and structured product schema.

    Args:
        url: The URL to validate.

    Returns a dict with:
        url           The (cleaned) URL that was validated.
        is_pdp        True when the page is confirmed as a PDP.
        confidence    "high" | "medium" | "low"
        page_type     "pdp" | "plp" | "other" | "unknown"
        product_name  Extracted name, or null.
        price_usd     Extracted price, or null.
        in_stock      Boolean stock status, or null.
        reason        Human-readable explanation of the verdict.
    """
    logger.info("validate_pdp: url=%r", url)

    clean_url = strip_tracking_params(url)

    # Fast path: URL structure alone signals a PLP.
    if is_plp_url(clean_url):
        return {
            "url": clean_url,
            "is_pdp": False,
            "confidence": "high",
            "page_type": "plp",
            "product_name": None,
            "price_usd": None,
            "in_stock": None,
            "reason": "URL path matches a known category/listing pattern.",
        }

    # Slow path: scrape and extract structured data.
    try:
        extract = await fc_scrape_extract(_FIRECRAWL_KEY, clean_url)
    except Exception as exc:
        logger.warning("validate_pdp scrape failed: %s", exc)
        return {
            "url": clean_url,
            "is_pdp": False,
            "confidence": "low",
            "page_type": "unknown",
            "product_name": None,
            "price_usd": None,
            "in_stock": None,
            "reason": "Scrape failed for the given URL.",
        }

    if not extract:
        return {
            "url": clean_url,
            "is_pdp": False,
            "confidence": "low",
            "page_type": "unknown",
            "product_name": None,
            "price_usd": None,
            "in_stock": None,
            "reason": "Scrape returned no structured data.",
        }

    page_type = (extract.get("page_type") or "unknown").lower()
    price = extract.get("price_usd")
    in_stock = extract.get("in_stock")
    product_name = extract.get("product_name")
    canonical = extract.get("canonical_url")
    if canonical:
        clean_url = strip_tracking_params(canonical)

    is_pdp = page_type == "pdp"

    if is_pdp and price is not None:
        confidence = "high"
        reason = "Page type is PDP and price was extracted."
    elif is_pdp:
        confidence = "medium"
        reason = "Page type is PDP but no price was found."
    elif page_type == "plp":
        confidence = "high"
        reason = "Firecrawl classified this as a listing page."
    else:
        confidence = "low"
        reason = f"Page type reported as '{page_type}'."

    return {
        "url": clean_url,
        "is_pdp": is_pdp,
        "confidence": confidence,
        "page_type": page_type,
        "product_name": product_name,
        "price_usd": price,
        "in_stock": in_stock,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
