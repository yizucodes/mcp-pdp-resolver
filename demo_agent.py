"""
demo_agent.py — End-to-end demo: LLM (GPT-4o) → MCPAgent → MCPClient → mcp_server.py → Firecrawl

Usage:
    python demo_agent.py "find me a black blazer under $300"
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from mcp_use import MCPAgent, MCPClient

from resolver import StripNullToolArgumentsMiddleware

load_dotenv()

MCP_CONFIG = Path(__file__).parent / "mcp.json"

_SYSTEM_PROMPT = (
    "You are a product research assistant. "
    "When given a product query, call the resolve_product tool with the query "
    "and return ONLY the raw JSON object from the tool result — no explanation, "
    "no markdown, just the JSON."
)


def _preflight() -> None:
    """Fail fast with actionable messages before touching any network."""
    errors: list[str] = []
    if not (os.getenv("OPENAI_API_KEY") or "").strip():
        errors.append("OPENAI_API_KEY is not set. Add it to your .env file.")
    if not (os.getenv("FIRECRAWL_API_KEY") or "").strip():
        errors.append("FIRECRAWL_API_KEY is not set. Add it to your .env file.")
    if not MCP_CONFIG.exists():
        errors.append(f"mcp.json not found at {MCP_CONFIG}")
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _build_client() -> MCPClient:
    """
    Build MCPClient with real API keys and StripNullToolArgumentsMiddleware.

    1. Middleware is applied at the MCPClient level only — not duplicated on MCPAgent —
       so it fires once per tool call regardless of which agent drives the session.
    2. mcp's stdio transport only inherits a small safe-list of env vars (HOME, PATH,
       SHELL, etc.) and then merges server.env on top. Any placeholder values in
       mcp.json would reach the subprocess unchanged, breaking authentication.
       We therefore inject the real keys from os.environ here at runtime.
    """
    config = json.loads(MCP_CONFIG.read_text())
    for server in config.get("mcpServers", {}).values():
        # Always use the same Python interpreter that launched demo_agent.py,
        # so the subprocess inherits the venv where mcp and resolver live.
        server["command"] = sys.executable
        # Resolve the script path relative to this file so it works regardless
        # of the working directory the caller used.
        if server.get("args") == ["mcp_server.py"]:
            server["args"] = [str(Path(__file__).parent / "mcp_server.py")]

        env = server.setdefault("env", {})
        for key in ("FIRECRAWL_API_KEY", "OPENAI_API_KEY"):
            real_value = (os.getenv(key) or "").strip()
            if real_value:
                env[key] = real_value

    return MCPClient(
        config=config,
        middleware=[StripNullToolArgumentsMiddleware()],
    )


def _print_result(raw: str) -> None:
    """Pretty-print the agent result. Falls back to raw text if it's not JSON."""
    try:
        data = json.loads(raw)
        print(json.dumps(data, indent=2))
        print()
        print(f"  canonical_url : {data.get('canonical_url') or '(none)'}")
        print(f"  price_usd     : {data.get('price_usd')}")
        print(f"  in_stock      : {data.get('in_stock')}")
        print(f"  confidence    : {data.get('confidence', 'unknown')}")
    except (json.JSONDecodeError, TypeError):
        print(raw)


async def main() -> None:
    _preflight()

    query = " ".join(sys.argv[1:]).strip() or "navy chinos from Bonobos"
    print(f"Query: {query!r}\n")

    client = _build_client()
    llm = ChatOpenAI(model="gpt-4o", temperature=0)
    agent = MCPAgent(
        llm=llm,
        client=client,
        max_steps=5,
        system_prompt=_SYSTEM_PROMPT,
        memory_enabled=False,
    )

    prompt = f'Use resolve_product to find: "{query}"'

    try:
        result = await agent.run(prompt)
        _print_result(result)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        msg = str(exc)
        if "timeout" in msg.lower():
            print(f"ERROR: Firecrawl request timed out. ({exc})", file=sys.stderr)
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
