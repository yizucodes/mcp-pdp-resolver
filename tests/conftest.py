"""
Pytest configuration.

``mcp_server`` exits on import if ``FIRECRAWL_API_KEY`` is unset. Tests that
import ``mcp_server`` rely on this stub unless the environment already provides
a real key (integration runs).
"""

from __future__ import annotations

import os

# Must run before any test module imports mcp_server.
os.environ.setdefault("FIRECRAWL_API_KEY", "pytest-stub-firecrawl-key")
