# 六大技术主线：工程整改与真实对照实验交付

日期：2026-10-10  
分支：`codex/agent-protocol-audit-20261009`  
最近已知历史 HEAD：`e1ad4c0165090902e83c65a7f1dcc751c31d51c1`

## 结论先行

本轮完成了六主线所需的最小生产协议增强和一个七目录实验包，但没有把已有 10 条真实 Qwen 回放外推成“整体智能提升”。已有公平可引用的真实 Qwen 对照仍然只有：Baseline `209fda8`、Optimized `8e908ac`、No-Skill `8e908ac` 的 10 条合成任务；严格成功率为 0.60 / 0.70 / 0.70，Provider 调用 55 / 58 / 58，P95 为 21.21s / 22.93s / 17.62s。它说明成功数增加，但调用数和延迟增加，且 No-Skill 与 Optimized 持平，因此 Skill/Prompt/Context 的因果收益仍未证实。

本轮不再追加真实模型调用。账本仍以此前核实的 189/250 Provider calls、477,425/1,500,000 保守计费 Token、约 USD 0.08534 为准；没有通过进程或工作树重置预算。

## 生产代码改动

- `app/services/skills.py`：从唯一 `SKILL.md` 派生 `id/version/input_types/capabilities/required_resources/negative_intents/tool_capabilities/prerequisites/references/content_hash/enabled/deprecated`；增加只含 L0 元数据的 `catalog()` 和选中后才读取正文的 `detail()`。
- `prompts/`、`app/services/prompt_catalog.py`：新增生产加载的 core/node Prompt Catalog；长节点提示仍由现有代码持有，Catalog 只补稳定政策和节点职责，不建立第二 Planner。Decision、Skill Router、Text2SQL、GroundedResponse 都实际加载对应片段。
- `app/services/context_projection.py`：按工具调用和 Evidence ID 去重，保留 SQLCandidate lineage，显式输出关键事实并在发送前校验 GoalContract、QueryScope、权限和预算字段。压缩只影响模型视图，原始 State/Event/ToolResult 不删除。
- `app/models/schemas.py`、`app/services/user_profile.py`：增加结构化 `UserProfile` 和显式确认的记忆写入边界；Dataset Version、失败状态和原始科研数据不进入长期 Profile。
- `app/services/query_decomposition.py`、`app/services/text2sql.py`：只审计、不生成固定 SQL 模板；在误差+训练覆盖目标上核实 `predictions→model_runs→experiments→dataset_versions` 和 `training_memberships→dataset_versions` 两条 lineage，建议拆成独立 scope 查询。Scope 验证仍然安全拒绝。
- `app/services/artifacts.py`：生成 Artifact 记录 SHA-256、字节数、CSV/XLSX 列清单和行数，便于下载后验证；不改变 MinIO 权限。

## 六主线证据与边界

详细机器可读资产位于 `evaluation/experiments/01_*` 至 `07_*`。每个目录都有独立 manifest、case/gold hash、baseline/optimized snapshot、paired CSV、ablation 和 failure analysis。

| 主线 | 当前最强证据 | 可宣称的优化 | 尚不能宣称 |
|---|---|---|---|
| Skill/Prompt/Context | 既有 Skill 100-case Qwen runs、Schema 70-case retrieval runs、当前 catalog/prompt/context 单测 | 元数据渐进加载、Prompt policy 生产加载、上下文去重/关键事实校验已实现 | 当前版本 paired Qwen accuracy/cost/latency 改善 |
| Goal/Plan | GoalContract/PlanStep/负例测试与 A/B/C 审计 | 目标冻结、可选工具与真实 Observation 完成谓词保持边界 | 新独立语义 holdout 的 False Success/Fallthrough 率 |
| Recovery/Executor | C focused trace、`targeted_sql_repair`、No-progress limit=2、deterministic executor | 错误类型有界恢复，失败候选只作诊断 | 全 fault-injection 矩阵的成功率分布 |
| Text2SQL/Scope | 70-case 历史 Qwen SQL runs、C 三次 focused live trace | 保留 `UNVERIFIED_SCOPE` 安全拒绝；新增 lineage/decomposition 检查 | C 的复杂 JOIN 还不能安全执行；Scope false reject 仍需更多 Gold |
| Evidence/Artifact | GroundedResponse gate、isolated MinIO/Artifact tests | Evidence ID gate、Artifact hash/列 manifest | 新 34-case claim adjudication |
| HITL/Checkpoint/SSE | 13-case isolated backend、21 frontend tests、1 browser pass | 持久 checkpoint、cursor replay、五阶段展示保留真实事件 | 断线/重复事件 fault-injection 率 |

### 已知历史量化结果

- Skill：`skill_dev_s0_all9` 100 cases，Top-1 1.0、Macro-F1 0.8936、总 Token 273,820；BM25-k3 100 cases，Top-1 0.87、Recall@3 0.795、总 Token 199,333。BM25 降低输入但损失召回，不能直接上线为普适最优。
- Schema：BM25 70 cases Table Recall@5 0.8476；BGE-M3 0.9190；Hybrid RRF 0.8976，且存在启动开销。当前未引入新向量库。
- Text2SQL：Gold-schema 70 cases result accuracy 0.9286、安全通过率 1.0；历史 repair-failure 6 cases repair success 0。C focused live 三个版本均 `UNVERIFIED_SCOPE` 安全停止，严格成功率 0，不将生成 SQL 当作已验证候选。
- E2E：已有 20-case isolated reference 的 success 0.85、Evidence coverage 0.77、Artifact correctness 0.5；这些是历史/隔离结果，不是本轮新测量。

## UI 与审计

用户视图仍分五阶段：任务理解、规划、数据分析、证据校验、最终回答（`web/src/executionStages.js`）。Decision、ToolCall、Observation、Recovery 等原始事件继续持久化并在开发者轨迹中展开；阶段归类不删除审计事件。

## 测试

- 后端离线：`414 passed, 42 skipped`（`SCIENTIFIC_AGENT_TEST_PROFILE=offline`）。
- 前端：`npm test` 21 passed；`npm run build` 通过。
- 本轮新增协议测试：`tests/test_six_track_protocols.py` 4 passed。
- 既有隔离基础设施的代表性运行仍为此前 `13 passed`；本轮扩大到需要真实模型或 product DB 的组合时，未配置 Qwen 的场景按设计失败/跳过，不把它们算作优化指标。

## 交付边界与下一批

未推送或合并 `main`，未写生产数据库/MinIO，未覆盖历史任务、冻结 gold 或既有 runs。要完成“六主线公平智能对照”，下一批必须先获得独立 Gold 审核和追加预算，然后用相同 Qwen 参数分别跑当前 50–100/30–50 case holdout；在此之前，所有未知指标在实验资产中都保持 `not_measured`。
