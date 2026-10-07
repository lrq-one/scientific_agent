import pytest

from app.services.mcp_client import ScientificMCPClient


@pytest.mark.asyncio
async def test_real_mcp_stdio_list_and_call():
    client = ScientificMCPClient()
    assert "get_molecule_features" in await client.list_tools()
    result = await client.call("get_molecule_features", {"molecule_id": "M004"})
    assert result.success is True
    assert result.data["structure_type"] == "fused_ring"
    assert result.metadata["transport"] == "stdio"

