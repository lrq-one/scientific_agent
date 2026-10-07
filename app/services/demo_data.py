from __future__ import annotations

import csv
from pathlib import Path
import sqlite3

from app.config import DEMO_DATA


MODEL_V1 = [
    ["M001", "CCO", 2.0, 2.2, "linear", False],
    ["M002", "C1CCCCC1", 5.0, 5.5, "cyclic", True],
    ["M003", "c1ccccc1", 6.0, 6.4, "aromatic", True],
    ["M004", "c1ccc2ccccc2c1", 8.0, 8.7, "fused_ring", True],
    ["M005", "CC(=O)O", 3.0, 3.2, "linear", False],
    ["M006", "C1=CC2=CC=CC=C2C=C1", 9.0, 9.8, "fused_ring", True],
    ["M007", "CCN", 2.5, 2.7, "linear", False],
    ["M008", "C1CCOC1", 4.4, 4.8, "cyclic", True],
]

MODEL_V2 = [
    ["M001", "CCO", 2.0, 2.1, "linear", False],
    ["M002", "C1CCCCC1", 5.0, 5.3, "cyclic", True],
    ["M003", "c1ccccc1", 6.0, 6.2, "aromatic", True],
    ["M004", "c1ccc2ccccc2c1", 8.0, 10.2, "fused_ring", True],
    ["M005", "CC(=O)O", 3.0, 3.1, "linear", False],
    ["M006", "C1=CC2=CC=CC=C2C=C1", 9.0, 11.6, "fused_ring", True],
    ["M007", "CCN", 2.5, 2.6, "linear", False],
    ["M008", "C1CCOC1", 4.4, 4.6, "cyclic", True],
]

HEADER = ["molecule_id", "smiles", "observed_rt", "predicted_rt", "structure_type", "is_cyclic"]


def ensure_demo_data(root: Path = DEMO_DATA) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, rows in (("model_v1.csv", MODEL_V1), ("model_v2.csv", MODEL_V2)):
        path = root / name
        if not path.exists():
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(HEADER)
                writer.writerows(rows)

    db_path = root / "training_demo.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DROP TABLE IF EXISTS training_molecules;
            DROP TABLE IF EXISTS molecules;
            CREATE TABLE molecules (
                molecule_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                smiles TEXT NOT NULL,
                structure_type TEXT NOT NULL
            );
            CREATE TABLE training_molecules (
                molecule_id TEXT NOT NULL REFERENCES molecules(molecule_id),
                dataset_version TEXT NOT NULL,
                is_cyclic INTEGER NOT NULL,
                PRIMARY KEY (molecule_id, dataset_version)
            );
            """
        )
        molecules = [
            ("T001", "Synthetic T001", "CCO", "linear"),
            ("T002", "Synthetic T002", "CCN", "linear"),
            ("T003", "Synthetic T003", "CCC", "linear"),
            ("T004", "Synthetic T004", "C1CCCCC1", "cyclic"),
            ("T005", "Synthetic T005", "C1CCOC1", "cyclic"),
            ("T006", "Synthetic T006", "c1ccccc1", "aromatic"),
            ("T007", "Synthetic T007", "c1ccc2ccccc2c1", "fused_ring"),
        ]
        training = [(row[0], "train_v3", int(row[3] != "linear")) for row in molecules]
        connection.executemany("INSERT INTO molecules VALUES (?, ?, ?, ?)", molecules)
        connection.executemany("INSERT INTO training_molecules VALUES (?, ?, ?)", training)

