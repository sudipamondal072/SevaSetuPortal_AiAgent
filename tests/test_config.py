from mcp_servers.common.config import (
    PORTAL_BASE_URL,
    CITIZEN_MCP_PORT,
    OFFICER_MCP_PORT,
)


def test_default_portal_url():
    assert PORTAL_BASE_URL.startswith("http://")


def test_mcp_ports_are_different():
    assert CITIZEN_MCP_PORT != OFFICER_MCP_PORT