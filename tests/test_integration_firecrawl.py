"""
Optional live Firecrawl exercises (slow, network, may flake on retailer HTML).

Skipped by default. To run::

    RUN_FIRECRAWL_INTEGRATION=1 pytest tests/test_integration_firecrawl.py -v

Use a real ``FIRECRAWL_API_KEY`` in the environment (not the ``conftest`` stub).
"""

from __future__ import annotations

import os

import pytest

import resolver

_KEY = (os.environ.get("FIRECRAWL_API_KEY") or "").strip()
_REAL_KEY = bool(_KEY) and _KEY != "pytest-stub-firecrawl-key"
_LIVE = os.environ.get("RUN_FIRECRAWL_INTEGRATION", "").strip() == "1"

pytestmark = pytest.mark.skipif(
    not (_LIVE and _REAL_KEY),
    reason="Set RUN_FIRECRAWL_INTEGRATION=1 and a real FIRECRAWL_API_KEY to run live tests.",
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_resolve_gap_chinos_often_hits_pdp_or_plp() -> None:
    """
    Representative 'happy-ish' query: large retailer, ``/products/`` URLs.

    We only assert schema + non-crash: live HTML and Firecrawl signals vary.
    """

    url, url_type, _extract = await resolver.resolve_product_url(
        item_name="men's slim fit khaki chinos navy from Gap",
        brand="",
        source="gap",
        current_url="",
        department="men",
        fc_key=_KEY,
    )

    assert url_type in ("pdp", "plp")
    assert url.startswith("http")
    if url_type == "pdp":
        assert "/products/" in url or "/p/" in url or "gap.com" in url


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_resolve_gibberish_query_returns_plp_or_empty() -> None:

    url, url_type, _extract = await resolver.resolve_product_url(
        item_name="nonexistent product xyz123 nonsense",
        brand="",
        source="",
        current_url="",
        department="",
        fc_key=_KEY,
    )

    assert url_type == "plp"
    assert url == "" or url.startswith("http")
