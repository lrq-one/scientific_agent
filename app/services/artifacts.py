from __future__ import annotations

from io import BytesIO
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from app.services.object_storage import ObjectStorageService


class ArtifactService:
    def __init__(self, storage: ObjectStorageService):
        self.storage = storage

    def plot_metric_comparison(
        self,
        owner_id: str,
        thread_id: str,
        metrics: dict[str, dict[str, float]],
        filename: str = "metric_comparison.png",
    ) -> dict[str, Any]:
        names = list(metrics)
        mae = [metrics[name]["mae"] for name in names]
        rmse = [metrics[name]["rmse"] for name in names]
        figure, axis = plt.subplots(figsize=(7, 4.2))
        x = range(len(names))
        axis.bar([index - 0.18 for index in x], mae, width=0.36, label="MAE")
        axis.bar([index + 0.18 for index in x], rmse, width=0.36, label="RMSE")
        axis.set_xticks(list(x), names, rotation=12, ha="right")
        axis.set_ylabel("Error")
        axis.set_title("Model metric comparison")
        axis.legend()
        axis.grid(axis="y", alpha=0.2)
        figure.tight_layout()
        output = BytesIO()
        figure.savefig(output, format="png", dpi=160)
        plt.close(figure)
        return self.storage.upload_artifact(
            owner_id,
            thread_id,
            filename,
            "image/png",
            output.getvalue(),
            "chart",
            {"generator": "plot_metric_comparison", "models": names},
        )

    def save_result_table(
        self,
        owner_id: str,
        thread_id: str,
        rows: list[dict[str, Any]],
        filename: str = "result_table.csv",
        output_format: str = "csv",
        required_columns: set[str] | None = None,
    ) -> dict[str, Any]:
        if not rows:
            raise ValueError("cannot create a scientific result table without rows")
        columns = set(rows[0])
        missing = (required_columns or set()) - columns
        if missing:
            raise ValueError(f"result table missing required columns: {sorted(missing)}")
        if any(set(row) != columns for row in rows):
            raise ValueError("result table rows have inconsistent columns")
        frame = pd.DataFrame(rows)
        output = BytesIO()
        if output_format == "xlsx":
            frame.to_excel(output, index=False)
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            artifact_type = "xlsx"
        elif output_format == "csv":
            output.write(frame.to_csv(index=False).encode("utf-8-sig"))
            content_type = "text/csv"
            artifact_type = "csv"
        else:
            raise ValueError("output_format must be csv or xlsx")
        return self.storage.upload_artifact(
            owner_id,
            thread_id,
            filename,
            content_type,
            output.getvalue(),
            artifact_type,
            {"generator": "save_result_table", "rows": len(frame)},
        )

    def save_chart(self, owner_id: str, thread_id: str, content: bytes, filename: str) -> dict[str, Any]:
        return self.storage.upload_artifact(
            owner_id, thread_id, filename, "image/png", content, "chart", {"generator": "save_chart"}
        )
