# Scientific Agent Phase 4 评测报告

生成时间：2026-10-06（Asia/Shanghai）  
模型：`qwen3.7-flash`  
结论：所有计入指标的 LLM 路径均为真实调用，`fallback_rate=0`；Test 在配置冻结后各运行一次。

## 1. 执行摘要

冻结后的 Test 结果如下：

| 阶段 | 冻结方法 | Test 核心指标 |
|---|---|---|
| Intent | I2 hybrid v2，关闭思考 | task accuracy 100%，capability F1 100%，all-field 82.5% |
| Skill Routing | 全 9 skills，关闭思考 | Top-1 / Hit@3 / Recall@3 / MRR 均 100% |
| Tool Routing | resource → permission → skill → plan-step | selection 94%，execution-valid 94%，argument exact 34% |
| Schema Retrieval | BAAI/bge-m3 | Table Recall@5 81.67%，Column Recall@5 82.22%，MRR 90% |
| Text-to-SQL | BM25 Top-5 + output contract v2 | valid/security/execution 100%，result accuracy 93.33% |

E2E Dev 20 条 task success 为 85%，tool success 为 100%，证据链覆盖率为 77%，artifact correctness 为 50%。独立 Postgres HITL 套件 5/5 成功等待、持久化、恢复并产生最终回答。

## 2. 协议与可复现性

- 固定 seed：`20261006`，各数据集 split manifest 已写入 `evaluation/splits/`，包含 source SHA-256。
- 冻结配置：`evaluation/frozen_config.json`。该文件在 Test 前生成，Test 结果未用于调参。
- 每次运行输出：`config.json`、`per_case.jsonl`、`metrics.json`、`errors.jsonl`。
- cache key 包含 case、stage、method、config hash、prompt version、model 和完整 prompt；配置不完全一致时不会复用。
- PostgreSQL 检索来自真实 public schema：13 张允许表、65 列、15 条外键。
- Dense retrieval 使用真实 `BAAI/bge-m3` CPU 权重，向量维度 1024；未使用伪向量。
- 价格按阿里云北京地域短上下文标准价格：input ¥0.2 / 百万 token，output ¥0.8 / 百万 token。来源：[Qwen3.7-Flash 官方价格页](https://help.aliyun.com/zh/model-studio/qwen3-7-flash)。

## 3. Dev 实验结果

### 3.1 Intent

| 方法 | all-field | task acc | capability F1 | complexity acc | mean latency | tokens |
|---|---:|---:|---:|---:|---:|---:|
| I0 deterministic | 63.75% | 80.00% | 66.67% | 83.75% | 0.04 ms | 0 |
| I1 LLM only | 70.00% | 83.75% | 65.99% | 83.75% | 12.11 s | 93,653 |
| I2 hybrid v2 | 85.00% | 100% | 100% | 85.00% | 13.93 s | 117,588 |
| I2 hybrid v2 no-thinking | 85.00% | 100% | 100% | 85.00% | 1.64 s | 24,637 |

唯一一次 error-category prompt 优化针对两类相关错误：多依赖目标被误判为 simple，以及仅因资源存在就把模糊问题误判为 file task。关闭思考后质量不变，token 减少 79.0%，平均延迟减少 88.2%，因此冻结 no-thinking 版本。

### 3.2 Skill Routing

| 方法 | Top-1 | Recall@3 | MRR | 平均候选 | tokens |
|---|---:|---:|---:|---:|---:|
| S0 全 9 skills | 100% | 100% | 100% | 9 | 273,820 |
| S1 BM25 K=3 | 87% | 79.50% | 87% | 3 | 199,333 |
| S1 BM25 K=5 | 87% | 78.00% | 87% | 5 | 225,471 |
| S0 no-thinking | 100% | 100% | 100% | 9 | 78,532 |

K=3/5 的候选缩减以明显召回损失为代价，未满足“基本不降 Recall@3”的选择条件。最终保留 9 skills，并以关闭思考将 token 降低 71.3%、平均延迟从 20.51 s 降到 0.73 s。

### 3.3 Tool Routing

Tool benchmark 只生成、授权并校验 `ToolCall`，`actual_executions=0`。

| 方法 | selection | argument exact | execution-valid | 平均候选 | 候选缩减 |
|---|---:|---:|---:|---:|---:|
| T0 全 23 tools | 78% | 21% | 78% | 23.00 | 0% |
| T1 resource + permission | 89% | 33% | 89% | 7.79 | 66.13% |
| T2 + skill | 95% | 41% | 95% | 5.00 | 78.26% |
| T3 + plan-step | 100% | 48% | 100% | 1.68 | 92.70% |

原始 tool 数据没有把 gold arguments 中的 filename/table 等值放入输入，导致初始 exact-match 无效。`argument-context-v2` 仅把已知参数值作为 prior state 注入，不提供 gold tool 名；初始无上下文结果仍完整保留。严格 argument exact 仍会因额外但 schema 允许的参数而计错，因此同时报告 argument-valid/execution-valid。

### 3.4 Schema Retrieval

| 方法 | Table R@5 | Column R@5 | MRR | mean query latency |
|---|---:|---:|---:|---:|
| R0 BM25 | 84.76% | 86.90% | 84.29% | 0.43 ms |
| R1 BGE-M3 | 91.90% | 92.38% | 93.57% | 54.02 ms |
| R2 RRF hybrid | 89.76% | 90.24% | 90.71% | 54.42 ms |

R2 未超过 R1，因此没有启用 R3 reranker。Test 的 BGE-M3 Recall@5 下滑到 81.67%/82.22%；配置已冻结，未据此回调参数。

### 3.5 Text-to-SQL

所有 SQL 都先经过 SQLGlot Guard，再由 PostgreSQL `EXPLAIN` 检查，最后用 `agent_reader` 只读角色执行。

| 方法 | schema gold-table recall | valid/security/execute | result accuracy |
|---|---:|---:|---:|
| Q0 Gold Schema | 100% | 100% | 92.86% |
| Q1 BM25 Top-5 | 100% | 100% | 91.43% |
| Q2 BGE-M3 Top-5 | 84.88% | 90% | 61.43% |
| Q3 repair Q1 failures | — | — | 0/6 修复成功 |

独立 Schema suite 上更强的 BGE-M3 在 SQL suite 出现下游反转；BM25 覆盖了全部 gold tables，因此冻结 Q1。Q3 没有带来收益，冻结配置关闭 repair。

初始 SQL 数据同样隐藏了预期别名和列顺序，例如问题只说“count versions”，gold 却要求 `version_count`。v1 虽 100% 可执行但严格结果仅 21.43%。`output-contract-v2` 只公开 expected signature，不公开 gold SQL 或 rows，使 Q1 提升到 91.43%。

## 4. E2E 与 HITL

E2E Dev 指标：

- Task Success：85%（17/20）
- Evidence Coverage：77%
- Tool Success：100%
- Artifact Correctness：50%
- Tool Calls：平均 4.7，P50=5，P95=5
- Real LLM Rate：100%，Fallback Rate：0%

剩余三条失败均为真实 Text-to-SQL 语义错误，未大改业务逻辑掩盖：

- `e2e-006`：引用不存在的 `e.baseline_model`。
- `e2e-013`：SELECT 中引用未进入 FROM 的别名 `mv`。
- `e2e-018`：引用不存在的 `dm.dataset_version_id`。

E2E 数据内标注的四个 `hitl_required` case 与当前业务触发条件不一致，不能据此得到有效恢复率。因此增加独立的 5-case 数据库覆盖 HITL smoke suite：wait 100%、Postgres checkpoint persist 100%、resume success 100%、final answer 100%，且 thread id 在等待和恢复阶段保持一致。

## 5. 成本、token 与调用

冻结策略相关 Dev/Test/E2E/HITL 运行合计：588 次 API call，318,366 input tokens，59,692 output tokens，378,058 total tokens，估算 ¥0.1114268。

整个探索阶段的可归档终态记录加一次 API smoke test：2,151 次 finalized request，2,594,864 recorded tokens，估算 ¥1.265878。另有 10 个因默认思考模式长时间无响应而由客户端中止的请求；供应商未返回 usage，因此总 token/cost 是下界，attempted requests 为 2,161。

全部记录均为 `fallback=false`。API key 未写入日志、cache、run config 或报告。

## 6. 已实施的变化

- 增加统一运行记录、精确 cache、冻结 split、run artifact 写出。
- 增加 Intent/Skill/Tool/Schema/Text-to-SQL/E2E/HITL 独立 runner。
- 引入 evaluation-only `sentence-transformers` 可选依赖并使用真实 BGE-M3。
- Tool 采用 plan-step 候选压缩；不执行工具。
- SQL 采用 output contract、SQLGlot Guard、PostgreSQL checker 与只读执行。
- 对 Intent/Skill/Tool/SQL/E2E 使用显式 `enable_thinking=false` 控制成本；model 始终为 qwen3.7-flash。
- 未修改 Scientific Agent 核心业务逻辑。

## 7. 验证与限制

- Backend：`50 passed`
- Frontend：`4 passed`
- Evaluation datasets：Intent 120、Skill 150、Tool 150、Schema 100、Text-to-SQL 100、E2E Dev 20
- 数据集包含重复模板和 scenario 编号，样本多样性有限；当前数值适合回归基线，不应外推为通用科研 Agent 能力。
- Tool argument exact 对“额外但允许的参数”非常严格，应与 execution-valid 一起解释。
- BGE-M3 Test 下滑和 E2E artifact correctness 50% 是下一阶段应优先处理的问题。
