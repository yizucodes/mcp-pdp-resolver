"""Tests for ``mcp_server`` tool handlers (Firecrawl calls mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import mcp_server


@pytest.mark.asyncio
async def test_resolve_product_high_confidence_after_post_scrape() -> None:
    """PDP URL + structured extract with price → ``confidence: high``."""

    with (
        patch.object(
            mcp_server,
            "resolve_product_url",
            new=AsyncMock(return_value=("https://x.example/p/1", "pdp", {
                    "product_name": "Slim Chino",
                    "price_usd": 79.0,
                    "in_stock": True,
                    "canonical_url": "https://x.example/p/1?gclid=abc&color=navy",
                })),
        ),
    ):
        out = await mcp_server.resolve_product("navy chinos", retailer="x.example")

    assert out["url_type"] == "pdp"
    assert out["confidence"] == "high"
    assert out["price_usd"] == 79.0
    assert "gclid" not in (out.get("canonical_url") or "")
    assert "color=navy" in (out.get("canonical_url") or "")


@pytest.mark.asyncio
async def test_resolve_product_low_confidence_plp() -> None:
    with patch.object(
        mcp_server,
        "resolve_product_url",
        new=AsyncMock(return_value=("https://x.example/category/pants", "plp", None)),
    ):
        out = await mcp_server.resolve_product("navy chinos")

    assert out["url_type"] == "plp"
    assert out["confidence"] == "low"
    assert out["product_name"] is None


@pytest.mark.asyncio
async def test_resolve_product_pdp_but_no_price_keeps_low_confidence() -> None:
    with (
        patch.object(
            mcp_server,
            "resolve_product_url",
            new=AsyncMock(return_value=("https://x.example/p/2", "pdp", {
                    "product_name": "Widget",
                    "price_usd": None,
                    "in_stock": True,
                })),
        ),
    ):
        out = await mcp_server.resolve_product("widget")

    assert out["url_type"] == "pdp"
    assert out["confidence"] == "low"


@pytest.mark.asyncio
async def test_search_products_maps_results() -> None:
    fake = [
        {"url": "https://a.com/1", "title": "A", "description": "d"},
        {"url": "", "title": "skip"},
    ]
    with patch.object(mcp_server, "fc_search", new=AsyncMock(return_value=fake)):
        out = await mcp_server.search_products("q", max_results=5)

    assert out["count"] == 1
    assert out["results"][0]["url"] == "https://a.com/1"


@pytest.mark.asyncio
async def test_map_site_returns_links() -> None:
    """map_site returns discovered URLs and echoes inputs."""
    fake_links = [
        "https://shop.example/products/chinos",
        "https://shop.example/products/blazer",
    ]
    with patch.object(mcp_server, "fc_map", new=AsyncMock(return_value=fake_links)):
        out = await mcp_server.map_site("https://shop.example", search="chinos")

    assert out["url"] == "https://shop.example"
    assert out["search"] == "chinos"
    assert out["count"] == 2
    assert out["links"] == fake_links


@pytest.mark.asyncio
async def test_map_site_no_search_term() -> None:
    """map_site works without a search term (None → empty string to fc_map)."""
    with patch.object(mcp_server, "fc_map", new=AsyncMock(return_value=[])) as mock:
        out = await mcp_server.map_site("https://shop.example")

    assert out["search"] is None
    assert out["count"] == 0
    assert out["links"] == []
    # Verify None was converted to "" for the underlying call
    mock.assert_called_once_with(
        mcp_server._FIRECRAWL_KEY,
        "https://shop.example",
        search="",
        session=None,
    )


@pytest.mark.asyncio
async def test_map_site_empty_result() -> None:
    """map_site handles Firecrawl returning no links."""
    with patch.object(mcp_server, "fc_map", new=AsyncMock(return_value=[])):
        out = await mcp_server.map_site("https://dead.example", search="anything")

    assert out["count"] == 0
    assert out["links"] == []


@pytest.mark.asyncio
async def test_validate_pdp_fast_path_category_url() -> None:
    out = await mcp_server.validate_pdp("https://shop.example/category/men/shirts")

    assert out["is_pdp"] is False
    assert out["page_type"] == "plp"
    assert out["confidence"] == "high"


@pytest.mark.asyncio
async def test_validate_pdp_scrape_high_when_pdp_and_price() -> None:
    with patch.object(
        mcp_server,
        "fc_scrape_extract",
        new=AsyncMock(
            return_value={
                "page_type": "pdp",
                "price_usd": 12.0,
                "in_stock": True,
                "product_name": "Z",
                "canonical_url": "https://shop.example/p/9",
            }
        ),
    ):
        out = await mcp_server.validate_pdp("https://shop.example/p/9?gclid=1")

    assert out["is_pdp"] is True
    assert out["confidence"] == "high"
    assert "gclid" not in out["url"]


@pytest.mark.asyncio
async def test_validate_pdp_scrape_failed_returns_low() -> None:
    with patch.object(mcp_server, "fc_scrape_extract", new=AsyncMock(side_effect=OSError("boom"))):
        out = await mcp_server.validate_pdp("https://shop.example/p/1")

    assert out["is_pdp"] is False
    assert out["confidence"] == "low"
    assert "Scrape failed" in (out.get("reason") or "")
