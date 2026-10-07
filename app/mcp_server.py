from __future__ import annotations

from mcp.server.fastmcp import FastMCP


server = FastMCP("scientific-molecule-demo")

MOLECULES = {
    "M004": {
        "molecule_id": "M004",
        "smiles": "c1ccc2ccccc2c1",
        "structure_type": "fused_ring",
        "ring_count": 2,
        "source": "synthetic_demo",
    },
    "M006": {
        "molecule_id": "M006",
        "smiles": "C1=CC2=CC=CC=C2C=C1",
        "structure_type": "fused_ring",
        "ring_count": 2,
        "source": "synthetic_demo",
    },
}


@server.tool()
def get_molecule_features(molecule_id: str) -> dict:
    """Return explicitly synthetic structural metadata for a demo molecule id."""
    return MOLECULES.get(
        molecule_id,
        {
            "molecule_id": molecule_id,
            "available": False,
            "source": "synthetic_demo",
        },
    )


if __name__ == "__main__":
    server.run(transport="stdio")

