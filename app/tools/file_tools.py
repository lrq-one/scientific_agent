from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.models.schemas import ToolResult


class FileAnalysisService:
    REQUIRED = {"molecule_id", "observed_rt", "predicted_rt", "structure_type", "is_cyclic"}

    def read_table(self, path: Path) -> pd.DataFrame:
        suffix = path.suffix.lower()
        if suffix == ".csv":
            return pd.read_csv(path)
        if suffix in {".xlsx", ".xls"}:
            return pd.read_excel(path)
        raise ValueError(f"unsupported file type: {suffix}")

    def inspect_columns(self, path: Path) -> ToolResult:
        frame = self.read_table(path)
        return ToolResult(
            success=True,
            data={
                "columns": list(frame.columns),
                "types": {name: str(dtype) for name, dtype in frame.dtypes.items()},
                "rows": int(len(frame)),
                "row_count": int(len(frame)),
            },
            source=str(path),
        )

    def inspect_table(self, path: Path) -> ToolResult:
        return self.inspect_columns(path)

    def read_csv(self, path: Path, limit: int = 100) -> ToolResult:
        if path.suffix.lower() != ".csv":
            return ToolResult(success=False, source=str(path), error="file is not CSV", outcome="UNSUPPORTED_OPERATION")
        frame = pd.read_csv(path)
        return ToolResult(success=True, data=frame.head(limit).to_dict(orient="records"), source=str(path),
                          metadata={"total_rows": len(frame), "row_limit": limit, "rows_complete": len(frame) <= limit})

    def read_excel(self, path: Path, limit: int = 100) -> ToolResult:
        if path.suffix.lower() not in {".xlsx", ".xls"}:
            return ToolResult(success=False, source=str(path), error="file is not Excel", outcome="UNSUPPORTED_OPERATION")
        frame = pd.read_excel(path)
        return ToolResult(success=True, data=frame.head(limit).to_dict(orient="records"), source=str(path),
                          metadata={"total_rows": len(frame), "row_limit": limit, "rows_complete": len(frame) <= limit})

    def profile_dataset(self, path: Path) -> ToolResult:
        frame = self.read_table(path)
        numeric = frame.select_dtypes(include="number")
        data = {
            "rows": int(len(frame)),
            "columns": int(len(frame.columns)),
            "missing": {name: int(value) for name, value in frame.isna().sum().items()},
            "unique": {name: int(value) for name, value in frame.nunique(dropna=True).items()},
            "numeric_ranges": {
                name: {"min": float(numeric[name].min()), "max": float(numeric[name].max())}
                for name in numeric
                if not numeric[name].dropna().empty
            },
        }
        return ToolResult(success=True, data=data, source=str(path), metadata={"deterministic": True})

    def calculate_metrics(self, path: Path) -> ToolResult:
        frame = self.read_table(path)
        missing = {"observed_rt", "predicted_rt"} - set(frame.columns)
        if missing:
            return ToolResult(success=False, source=str(path), error=f"missing columns: {sorted(missing)}")
        errors = (frame["predicted_rt"] - frame["observed_rt"]).abs()
        data = {
            "count": int(len(frame)),
            "mae": round(float(errors.mean()), 4),
            "rmse": round(float(((errors ** 2).mean()) ** 0.5), 4),
        }
        return ToolResult(success=True, data=data, source=str(path), metadata={"deterministic": True})

    def group_metrics(self, path: Path, group: str = "structure_type") -> ToolResult:
        frame = self.read_table(path).copy()
        if group not in frame.columns:
            return ToolResult(success=False, source=str(path), error=f"unknown group column: {group}")
        frame["absolute_error"] = (frame["predicted_rt"] - frame["observed_rt"]).abs()
        grouped = frame.groupby(group, dropna=False)["absolute_error"].agg(["count", "mean", "max"]).reset_index()
        grouped = grouped.rename(columns={"mean": "mae", "max": "max_error"})
        grouped[["mae", "max_error"]] = grouped[["mae", "max_error"]].round(4)
        return ToolResult(success=True, data=grouped.to_dict(orient="records"), source=str(path), metadata={"deterministic": True})

    def find_high_error_samples(self, path: Path, limit: int = 5) -> ToolResult:
        frame = self.read_table(path).copy()
        frame["absolute_error"] = (frame["predicted_rt"] - frame["observed_rt"]).abs()
        cols = [c for c in ["molecule_id", "smiles", "structure_type", "absolute_error"] if c in frame.columns]
        rows: list[dict[str, Any]] = frame.nlargest(limit, "absolute_error")[cols].round(4).to_dict(orient="records")
        return ToolResult(success=True, data=rows, source=str(path), metadata={"deterministic": True})

    def compare_models(self, paths: list[Path]) -> ToolResult:
        if len(paths) != 2:
            return ToolResult(success=False, source=",".join(map(str, paths)), error="paired comparison requires exactly two model files")
        frames = [self.read_table(path) for path in paths]
        required = {"molecule_id", "observed_rt", "predicted_rt", "structure_type"}
        for path, frame in zip(paths, frames, strict=True):
            missing = required - set(frame)
            if missing:
                return ToolResult(success=False, source=str(path), error=f"missing columns: {sorted(missing)}")
            if frame["molecule_id"].isna().any() or frame["molecule_id"].duplicated().any():
                return ToolResult(success=False, source=str(path), error="null or duplicate molecule_id")
            if not np.isfinite(pd.to_numeric(frame["observed_rt"], errors="coerce")).all() or not np.isfinite(
                pd.to_numeric(frame["predicted_rt"], errors="coerce")
            ).all():
                return ToolResult(success=False, source=str(path), error="non-finite RT value")
        left_ids, right_ids = set(frames[0]["molecule_id"]), set(frames[1]["molecule_id"])
        unmatched = [sorted(left_ids - right_ids), sorted(right_ids - left_ids)]
        if any(unmatched):
            return ToolResult(success=True, source=",".join(map(str, paths)), data={
                "comparable": False,
                "left_only_count": len(unmatched[0]), "right_only_count": len(unmatched[1]),
                "left_only_examples": unmatched[0][:20], "right_only_examples": unmatched[1][:20],
            }, metadata={"deterministic": True, "join_key": "molecule_id"})
        joined = frames[0][["molecule_id", "observed_rt", "predicted_rt", "structure_type"]].merge(
            frames[1][["molecule_id", "observed_rt", "predicted_rt", "structure_type"]],
            on="molecule_id", how="inner", suffixes=("_left", "_right"), validate="one_to_one",
        )
        if not np.allclose(joined["observed_rt_left"], joined["observed_rt_right"], rtol=0, atol=1e-9):
            return ToolResult(success=False, source=",".join(map(str, paths)), error="observed_rt differs for aligned molecules")
        if not joined["structure_type_left"].equals(joined["structure_type_right"]):
            return ToolResult(success=False, source=",".join(map(str, paths)), error="structure_type differs for aligned molecules")
        if joined.empty:
            return ToolResult(success=True, source=",".join(map(str, paths)), data={"comparable": False, "aligned_count": 0},
                              metadata={"deterministic": True, "join_key": "molecule_id"})
        joined["error_left"] = (joined["predicted_rt_left"] - joined["observed_rt_left"]).abs()
        joined["error_right"] = (joined["predicted_rt_right"] - joined["observed_rt_right"]).abs()
        groups = joined.groupby("structure_type_left", sort=True)
        subgroup = [
            {
                "structure_type": str(name), "count": int(len(group)),
                "mae_left": round(float(group["error_left"].mean()), 4),
                "mae_right": round(float(group["error_right"].mean()), 4),
                "delta_right_minus_left": round(float((group["error_right"] - group["error_left"]).mean()), 4),
            }
            for name, group in groups
        ]
        return ToolResult(success=True, source=",".join(map(str, paths)), data={
            "comparable": True, "aligned_count": int(len(joined)),
            "left_model": paths[0].stem, "right_model": paths[1].stem,
            "mae_left": round(float(joined["error_left"].mean()), 4),
            "mae_right": round(float(joined["error_right"].mean()), 4),
            "paired_mae_delta_right_minus_left": round(float((joined["error_right"] - joined["error_left"]).mean()), 4),
            "subgroups": subgroup,
        }, metadata={"deterministic": True, "join_key": "molecule_id"})

    def join_tables(self, left: Path, right: Path, on: str, limit: int = 500) -> ToolResult:
        left_frame, right_frame = self.read_table(left), self.read_table(right)
        if on not in left_frame or on not in right_frame:
            return ToolResult(success=False, source=f"{left},{right}", error=f"join key not found: {on}")
        joined = left_frame.merge(right_frame, on=on, how="inner", suffixes=("_left", "_right"))
        return ToolResult(
            success=True,
            data=joined.head(limit).to_dict(orient="records"),
            source=f"{left},{right}",
            metadata={"rows": int(len(joined)), "join": "inner", "key": on},
        )

    def filter_samples(self, path: Path, filters: dict[str, Any], limit: int = 500) -> ToolResult:
        frame = self.read_table(path)
        selected = frame
        for column, expected in filters.items():
            if column not in selected:
                return ToolResult(success=False, source=str(path), error=f"unknown filter column: {column}")
            if isinstance(expected, (dict, list)):
                return ToolResult(success=False, source=str(path), outcome="UNSUPPORTED_OPERATION",
                                  error="filter_samples supports scalar equality only; operator objects are unsupported")
            selected = selected[selected[column] == expected]
        return ToolResult(
            success=True,
            data=selected.head(limit).to_dict(orient="records"),
            source=str(path),
            metadata={"matched_rows": int(len(selected)), "filters": filters, "rows_complete": len(selected) <= limit},
        )

