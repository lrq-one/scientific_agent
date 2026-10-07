from __future__ import annotations

import random

from evaluation.phase4_common import PROTOCOL_ROOT, SEED, SPLITS_ROOT, load_suite, sha256, write_json


SIZES = {
    "intent": (80, 40),
    "skill_routing": (100, 50),
    "tool_routing": (100, 50),
    "schema_retrieval": (70, 30),
    "text2sql": (70, 30),
}


STEP_CANDIDATES = {
    "discover file resources": ["list_workspace_files", "inspect_table"],
    "inspect uploaded table": ["inspect_table", "profile_dataset"],
    "read bounded records": ["read_csv", "read_excel", "preview_table"],
    "profile input": ["inspect_table", "profile_dataset"],
    "compute overall metrics": ["calculate_metrics", "compare_models"],
    "group by structure": ["group_metrics", "compare_structure_groups"],
    "compare models": ["compare_models", "calculate_metrics"],
    "rank residuals": ["find_high_error_samples", "filter_samples"],
    "join on molecule": ["join_tables", "get_molecule_features"],
    "filter rows": ["filter_samples", "find_high_error_samples"],
    "create chart": ["plot_metric_comparison", "save_chart"],
    "list authorized sources": ["list_datasources", "search_schema"],
    "retrieve schema": ["search_schema", "get_table_schema"],
    "inspect table schema": ["get_table_schema", "preview_table"],
    "inspect relationships": ["get_table_relationships", "search_schema"],
    "preview rows": ["preview_table", "get_table_schema"],
    "execute approved SQL": ["execute_readonly_sql", "preview_table"],
    "retrieve scientific features": ["get_molecule_features", "compare_structure_groups"],
    "invoke configured model": ["predict_rt", "get_molecule_features"],
    "summarize groups": ["compare_structure_groups", "group_metrics"],
    "save table": ["save_result_table", "save_chart"],
    "save figure": ["save_chart", "plot_metric_comparison"],
}


def main() -> None:
    for index, (stage, (dev_count, test_count)) in enumerate(SIZES.items()):
        rows = load_suite(stage)
        if len(rows) != dev_count + test_count:
            raise ValueError(f"{stage}: expected {dev_count + test_count}, got {len(rows)}")
        ids = [row["case_id"] for row in rows]
        random.Random(SEED + index).shuffle(ids)
        manifest = {
            "stage": stage,
            "seed": SEED + index,
            "source_sha256": sha256(rows),
            "dev": ids[:dev_count],
            "test": ids[dev_count:],
        }
        write_json(SPLITS_ROOT / f"{stage}.json", manifest)

    e2e = load_suite("e2e")
    write_json(SPLITS_ROOT / "e2e.json", {
        "stage": "e2e",
        "seed": SEED,
        "source_sha256": sha256(e2e),
        "dev": [row["case_id"] for row in e2e],
        "test": [],
        "note": "All current E2E cases are Dev/runtime evaluation only.",
    })
    write_json(PROTOCOL_ROOT / "tool_step_candidates.json", {
        "version": "tool-context-v1",
        "created_before_any_dev_result": True,
        "mapping": STEP_CANDIDATES,
        "sha256": sha256(STEP_CANDIDATES),
    })


if __name__ == "__main__":
    main()
