"""
Scenario tests for ``_resolve_product_url`` with Firecrawl I/O mocked.

These map to the kinds of queries you would try manually (PDP happy path,
category-style URLs, empty search, stock/price gates, etc.).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import resolver


def _pdp_extract(
    *,
    url: str = "https://retailer.example/products/widget",
    price: float = 49.0,
    in_stock: bool = True,
    page_type: str = "pdp",
) -> dict:
    return {
        "page_type": page_type,
        "price_usd": price,
        "in_stock": in_stock,
        "canonical_url": url,
        "product_name": "Example Widget",
    }


@pytest.mark.asyncio
async def test_pdp_happy_path_first_candidate() -> None:
    """Firecrawl classifies PDP, price present, in stock → confirmed PDP."""

    async def search(*_a, **_k):
        return [{"url": "https://retailer.example/shop/pants"}]

    async def map_urls(*_a, **_k):
        return ["https://retailer.example/products/slim-chino-navy"]

    scrape = AsyncMock(side_effect=[_pdp_extract(url="https://retailer.example/products/slim-chino-navy")])

    with (
        patch.object(resolver, "fc_search", side_effect=search),
        patch.object(resolver, "fc_map", side_effect=map_urls),
        patch.object(resolver, "fc_scrape_extract", scrape),
    ):
        url, url_type, _extract = await resolver.resolve_product_url(
            item_name="navy slim chinos",
            brand="",
            source="retailer.example",
            current_url="",
            department="men",
            fc_key="fake-key",
        )

    assert url_type == "pdp"
    assert "/products/" in url
    scrape.assert_awaited()


@pytest.mark.asyncio
async def test_pdp_second_candidate_after_first_is_plp() -> None:
    """Subcategory URL misclassified as PLP; next `/products/` URL wins."""

    async def search(*_a, **_k):
        return [{"url": "https://retailer.example/c/pants"}]

    async def map_urls(*_a, **_k):
        return [
            "https://retailer.example/shop/pants/subcategory-chinos",
            "https://retailer.example/products/core-chino",
        ]

    async def scrape(_key, u, **_k):
        if "subcategory" in u:
            return _pdp_extract(
                url=u,
                page_type="plp",
                price=49.0,
                in_stock=True,
            )
        return _pdp_extract(url=u, price=89.0)

    with (
        patch.object(resolver, "fc_search", side_effect=search),
        patch.object(resolver, "fc_map", side_effect=map_urls),
        patch.object(resolver, "fc_scrape_extract", side_effect=scrape),
    ):
        url, url_type, _extract = await resolver.resolve_product_url(
            item_name="navy chinos",
            brand="",
            source="retailer.example",
            current_url="",
            department="",
            fc_key="fake-key",
        )

    assert url_type == "pdp"
    assert "core-chino" in url


@pytest.mark.asyncio
async def test_plp_fallback_when_all_scrapes_are_plp() -> None:
    """Bonobos-style outcome: MAP finds URLs but every extract is PLP → best PLP."""

    async def search(*_a, **_k):
        return [{"url": "https://bonobos.com/shop/clothing/pants/chinos-casual-pants"}]

    async def map_urls(*_a, **_k):
        return [
            "https://bonobos.com/shop/clothing/pants/chinos-casual-pants/lightweight-chinos",
        ]

    async def scrape(_key, u, **_k):
        return {
            "page_type": "plp",
            "price_usd": 49.0,
            "in_stock": True,
            "canonical_url": u,
            "product_name": None,
        }

    with (
        patch.object(resolver, "fc_search", side_effect=search),
        patch.object(resolver, "fc_map", side_effect=map_urls),
        patch.object(resolver, "fc_scrape_extract", side_effect=scrape),
    ):
        url, url_type, _extract = await resolver.resolve_product_url(
            item_name="navy chinos from Bonobos",
            brand="",
            source="bonobos",
            current_url="",
            department="",
            fc_key="fake-key",
        )

    assert url_type == "plp"
    assert "bonobos.com" in url


@pytest.mark.asyncio
async def test_plp_when_search_returns_no_results() -> None:
    async def search(*_a, **_k):
        return []

    with patch.object(resolver, "fc_search", side_effect=search):
        url, url_type, _extract = await resolver.resolve_product_url(
            item_name="nonexistent product xyz123",
            brand="",
            source="",
            current_url="",
            department="",
            fc_key="fake-key",
        )

    assert url_type == "plp"
    assert url == ""


@pytest.mark.asyncio
async def test_plp_when_map_has_no_product_path_candidates() -> None:
    """Mapped links do not match ``_FILTER_KEEP_RE`` → fall back to search URL."""

    async def search(*_a, **_k):
        return [{"url": "https://retailer.example/category/o/123"}]

    async def map_urls(*_a, **_k):
        return [
            "https://retailer.example/category/o/123/listing",
        ]

    with (
        patch.object(resolver, "fc_search", side_effect=search),
        patch.object(resolver, "fc_map", side_effect=map_urls),
    ):
        url, url_type, _extract = await resolver.resolve_product_url(
            item_name="widget",
            brand="",
            source="retailer.example",
            current_url="",
            department="",
            fc_key="fake-key",
        )

    assert url_type == "plp"
    assert "category/o" in url or url.endswith("123")


@pytest.mark.asyncio
async def test_skips_pdp_without_price() -> None:
    async def search(*_a, **_k):
        return [{"url": "https://retailer.example/p/1"}]

    async def map_urls(*_a, **_k):
        return ["https://retailer.example/products/one"]

    async def scrape(_key, _u, **_k):
        return {
            "page_type": "pdp",
            "price_usd": None,
            "in_stock": True,
            "canonical_url": "https://retailer.example/products/one",
        }

    with (
        patch.object(resolver, "fc_search", side_effect=search),
        patch.object(resolver, "fc_map", side_effect=map_urls),
        patch.object(resolver, "fc_scrape_extract", side_effect=scrape),
    ):
        url, url_type, _extract = await resolver.resolve_product_url(
            item_name="mystery item",
            brand="",
            source="retailer.example",
            current_url="",
            department="",
            fc_key="fake-key",
        )

    assert url_type == "plp"


@pytest.mark.asyncio
async def test_skips_pdp_when_out_of_stock() -> None:
    async def search(*_a, **_k):
        return [{"url": "https://retailer.example/p/1"}]

    async def map_urls(*_a, **_k):
        return ["https://retailer.example/products/oos-item"]

    async def scrape(_key, _u, **_k):
        return _pdp_extract(
            url="https://retailer.example/products/oos-item",
            price=10.0,
            in_stock=False,
        )

    with (
        patch.object(resolver, "fc_search", side_effect=search),
        patch.object(resolver, "fc_map", side_effect=map_urls),
        patch.object(resolver, "fc_scrape_extract", side_effect=scrape),
    ):
        url, url_type, _extract = await resolver.resolve_product_url(
            item_name="sold out thing",
            brand="",
            source="retailer.example",
            current_url="",
            department="",
            fc_key="fake-key",
        )

    assert url_type == "plp"


@pytest.mark.asyncio
async def test_skips_when_scrape_returns_none() -> None:
    async def search(*_a, **_k):
        return [{"url": "https://retailer.example/start"}]

    async def map_urls(*_a, **_k):
        return ["https://retailer.example/products/broken"]

    async def scrape(_key, _u, **_k):
        return None

    with (
        patch.object(resolver, "fc_search", side_effect=search),
        patch.object(resolver, "fc_map", side_effect=map_urls),
        patch.object(resolver, "fc_scrape_extract", side_effect=scrape),
    ):
        url, url_type, _extract = await resolver.resolve_product_url(
            item_name="thing",
            brand="",
            source="retailer.example",
            current_url="",
            department="",
            fc_key="fake-key",
        )

    assert url_type == "plp"
    assert url.endswith("start") or "retailer.example" in url
