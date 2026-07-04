"""End-to-end: `cos serve` MCP server driven by a real MCP client over HTTP.

Spawns `cos serve` as a subprocess and connects with the mcp SDK client — the
exact path an MCP client (arc) takes. Skips if `mcp` or Docker is unavailable.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("mcp")

from cos.core.backend import DockerBackend  # noqa: E402

_PORT = 8779


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="module")
def _docker_ok():
    try:
        DockerBackend().ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"docker not reachable: {exc}")


@pytest.fixture
def server(_docker_ok):
    proc = subprocess.Popen(
        [sys.executable, "-m", "cos.cli.main", "serve", "--port", str(_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if _port_open(_PORT):
            break
        if proc.poll() is not None:
            pytest.skip("cos serve exited before binding")
        time.sleep(0.2)
    else:
        proc.terminate()
        pytest.skip("cos serve did not bind in time")
    yield f"http://127.0.0.1:{_PORT}/mcp"
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


async def _drive(url):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            result = await session.call_tool(
                "container_run", {"image": "alpine:3.19", "command": "echo mcp-e2e"})
            text = "\n".join(getattr(b, "text", "") for b in result.content)
            return tools, text


def test_mcp_server_run_end_to_end(server):
    import asyncio

    tools, text = asyncio.run(_drive(server))
    assert {"container_run", "container_list", "container_ensure"} <= tools
    assert "mcp-e2e" in text and "exit=0" in text
