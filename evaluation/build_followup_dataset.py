from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "followup" / "cases.jsonl"
SPLIT_OUT = ROOT / "splits" / "followup.json"


DEV = {
    "EVIDENCE_EXPLANATION": [
        "证据呢？",
        "你怎么得到的这些结果？",
        "这个数字从哪来的？",
        "把上一轮 SQL 和原始返回告诉我。",
        "training_db 的依据是什么？",
        "fused-ring 这个结论有原始证据吗？",
        "请给出支撑刚才结论的 Evidence ID。",
        "刚才查询用了什么数据源和版本？",
        "请展示上一轮的查询逻辑，不要重跑。",
        "这些统计是怎么算出来的？",
    ],
    "RESULT_EXPLANATION": [
        "刚才为什么这么判断？",
        "这个结果是什么意思？",
        "解释一下上一轮的结论。",
        "为什么说 fused-ring 覆盖不足？",
        "前面的数字应该怎么解读？",
        "你为什么认为误差集中在环状结构？",
        "那这个结论呢？",
        "前面那个判断呢？",
    ],
    "CONTINUE_ANALYSIS": [
        "继续分析下一步。",
        "接着检查其他结构类型。",
        "再深入分析误差分布。",
        "继续做分组比较。",
        "下一步分析模型版本差异。",
        "在这个基础上继续分析训练覆盖。",
    ],
    "REFINE_PREVIOUS_TASK": [
        "换成 train_v2 再分析一次。",
        "只看 fused-ring 的覆盖。",
        "把范围限定为 model_v2。",
        "改成按 structure_type 分组。",
        "筛选 train_v3 中误差大于 0.5 的记录。",
        "调整为只统计已进入训练集的分子。",
    ],
    "RERUN_PREVIOUS_TASK": [
        "重新运行上一轮分析。",
        "请重跑刚才的 SQL。",
        "用同样条件再执行一次。",
        "重新查询并验证结果。",
        "把刚才任务完整重做一遍。",
        "再跑一次上一轮统计。",
    ],
    "ERROR_QUESTION": [
        "刚才的查询为什么报错？",
        "上一轮失败在哪里？",
        "这个异常是什么原因？",
        "为什么 SQL 执行失败了？",
        "刚才错误发生在哪一步？",
        "解释一下前面的失败信息。",
    ],
    "NEW_TASK": [
        "统计 training_db 中 train_v3 各结构类型的样本数。",
        "比较 model_v1 与 model_v2 的平均绝对误差。",
        "分析 train_v3 中各结构类型的预测误差。",
        "查询训练集中分子量的分布。",
        "计算各模型版本的 RMSE。",
        "检查 train_v2 的 fused-ring 训练覆盖。",
        "统计不同仪器平台的实验数量。",
        "找出预测误差最高的五个分子。",
    ],
}

TEST = {
    "EVIDENCE_EXPLANATION": [
        "请把这些结果的证据链给我。",
        "上一轮用的 SQL、参数和原始行是什么？",
        "这个样本数的来源在哪里？",
        "把 fused-ring 对应的持久化 Evidence 给我。",
    ],
    "RESULT_EXPLANATION": [
        "为什么会得到上面的结论？",
        "请解释刚才的统计结果。",
        "上一个判断该怎么理解？",
    ],
    "CONTINUE_ANALYSIS": [
        "继续检查覆盖和误差的关系。",
        "接着分析其他模型版本。",
        "再深入一步看看异常结构。",
    ],
    "REFINE_PREVIOUS_TASK": [
        "换成 train_v1 做相同分析。",
        "只保留 fused-ring 再统计。",
        "改为按数据集版本分别汇总。",
    ],
    "RERUN_PREVIOUS_TASK": [
        "重新执行这个任务。",
        "按原条件再跑一遍。",
    ],
    "ERROR_QUESTION": [
        "前面为什么会失败？",
        "解释上一轮的错误，不要重新查询。",
    ],
    "NEW_TASK": [
        "统计 train_v3 中每种 structure_type 的覆盖率。",
        "查询不同 model_version 的平均预测误差。",
        "分析训练样本数与绝对误差之间的关系。",
    ],
}


def build() -> tuple[list[dict], str]:
    cases: list[dict] = []
    counts: dict[str, int] = {}
    for split, source in (("dev", DEV), ("test", TEST)):
        for follow_up_type, queries in source.items():
            for query in queries:
                counts[follow_up_type] = counts.get(follow_up_type, 0) + 1
                reuse = follow_up_type in {
                    "EVIDENCE_EXPLANATION",
                    "RESULT_EXPLANATION",
                    "ERROR_QUESTION",
                }
                cases.append(
                    {
                        "case_id": f"followup-{split}-{len([x for x in cases if x['split'] == split]) + 1:03d}",
                        "split": split,
                        "query": query,
                        "gold_follow_up_type": follow_up_type,
                        "requires_provenance": reuse,
                        "allow_workflow": not reuse,
                        "expected_provenance_fields": (
                            ["datasource", "dataset_version", "sql_candidate", "sql_raw_result", "evidence_ids"]
                            if reuse
                            else []
                        ),
                    }
                )
    assert sum(case["split"] == "dev" for case in cases) == 50
    assert sum(case["split"] == "test" for case in cases) == 20
    payload = "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return cases, digest


def main() -> None:
    cases, digest = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases))
    split = {
        "seed": 20261006,
        "frozen": True,
        "sha256": digest,
        "dev": [case["case_id"] for case in cases if case["split"] == "dev"],
        "test": [case["case_id"] for case in cases if case["split"] == "test"],
    }
    with SPLIT_OUT.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(split, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"cases": len(cases), "dev": len(split["dev"]), "test": len(split["test"]), "sha256": digest}))


if __name__ == "__main__":
    main()
