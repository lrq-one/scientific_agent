"""Eight traceable tables and six compact scientific-agent benchmark figures."""
import json
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from evaluation.frozen.prepare import REPORT


def load(name):return json.loads((REPORT/name).read_text(encoding="utf-8"))


def format_metric(value):
    if isinstance(value,dict) and "rate" in value:
        n,d,r,ci=value["numerator"],value["denominator"],value["rate"],value["wilson_95"]
        return f"{n}/{d}; {r:.1%}; CI [{ci[0]:.1%}, {ci[1]:.1%}]" if r is not None else f"{n}/{d}; N/A"
    if isinstance(value,dict) and "median" in value:
        return f"mean={value['mean']:.2f}; median={value['median']:.2f}; P90={value['p90']:.2f}; P95={value['p95']:.2f}" if value["mean"] is not None else "N/A"
    return str(value)


def table(headers,rows):
    return "| "+" | ".join(headers)+" |\n|"+"|".join("---" for _ in headers)+"|\n"+"\n".join("| "+" | ".join(str(v).replace("|","/").replace("\n"," ") for v in r)+" |" for r in rows)+"\n"


def finish(fig,name):
    fig.text(.5,.01,"Frozen Evaluation · Synthetic Agent Benchmark",ha="center",fontsize=9,color="#555555")
    fig.tight_layout(rect=(0,.035,1,1))
    fig.savefig(REPORT/"figures"/name,dpi=160);plt.close(fig)


def main():
    frozen=load("freeze_manifest.json");s=load("metrics/full_summary.json");types=load("metrics/by_task_type.json");abl=load("metrics/ablation_summary.json");cases=load("metrics/per_case.json")
    (REPORT/"figures").mkdir(exist_ok=True)
    plt.rcParams.update({"font.family":"DejaVu Sans","font.size":10,"axes.spines.top":False,"axes.spines.right":False})
    fig,ax=plt.subplots(figsize=(10,4))
    ax.bar([x["type"] for x in types],[100*x["task_success"]["rate"] for x in types],color="#4378a8")
    ax.set_ylim(0,105);ax.set_ylabel("Conversation success (%)");ax.set_title("Full Agent by task type (overlapping tags)");ax.tick_params(axis="x",rotation=25)
    finish(fig,"01-task-success.png")
    configurations=[("no_skill",["task_success","tool_validity"],"Skill ablation"),
        ("no_replan",["task_success","replan_recovery"],"Replanning ablation"),
        ("stateless_followup",["task_success","followup_context","structured_reuse"],"Follow-up ablation"),
        ("no_evidence_gate",["claim_evidence_coverage","numerical_consistency","unsupported_claim_rate"],"Evidence gate ablation")]
    for i,(variant,metrics,title) in enumerate(configurations,2):
        values=[r for m in metrics for r in abl if r["experiment"]==variant and r["metric"]==m]
        fig,ax=plt.subplots(figsize=(8,4));x=np.arange(len(values));w=.34
        ax.bar(x-w/2,[100*r["baseline"] if r["baseline"] is not None else np.nan for r in values],w,label=variant,color="#9ca3af")
        ax.bar(x+w/2,[100*r["full"] if r["full"] is not None else np.nan for r in values],w,label="FULL",color="#4378a8")
        ax.set_xticks(x,[r["metric"].replace("_","\n") for r in values]);ax.set_ylabel("Rate (%)");ax.set_ylim(0,105);ax.legend();ax.set_title(title+" (15 paired conversations)")
        finish(fig,f"0{i}-{variant}.png")
    fig,axs=plt.subplots(1,3,figsize=(14,4))
    displayed=types[:8]
    for ax,key,label,scale in zip(axs,["latency_ms","tokens_observed","tool_calls"],["Median system latency (s)","Mean observed tokens","Mean ToolCalls"],[.001,1,1]):
        field="median" if key=="latency_ms" else "mean"
        ax.bar([x["type"] for x in displayed],[x[key][field]*scale for x in displayed],color="#4378a8")
        ax.tick_params(axis="x",rotation=65);ax.set_title(label)
    finish(fig,"06-efficiency.png")
    text=["# Scientific Agent Phase 4 v2 — Frozen Quantitative Evaluation\n",
        "This evaluates Scientific Agent system capability, **not RT model accuracy, MS/MS prediction quality or scientific discovery**. Synthetic evaluation dataset. **No real scientific model inference was evaluated.**\n",
        f"Version `{frozen['benchmark_version']}`; Git `{frozen['git_sha']}`; model `{frozen['model']}`; branch `{frozen['branch']}`. Product tree was dirty before freeze; exact source/build/prompt hashes identify the evaluated version, rather than pretending the commit alone identifies it.\n",
        "60 fresh FULL conversations and 4 preselected 15-case paired ablations (120 executions). Each formal task originates from real Vue/Playwright and FastAPI/SSE. Human answers are scripted via the actual HITL control; measured harness waiting is separated from system latency. No historical acceptance PASS is a formal score.\n",
        "Programmatic: transport usage/costs, tool duplicate/failure classification, IDs/lineage, security action blocking, persisted refresh, subset pairing and confidence intervals. **Judge-assisted**: strict goal/content success, labelled numeric consistency, natural-language claim coverage/unsupported causation/limitations and necessary-plan-objective scoring. A separate fixed-rubric Qwen evaluator sees Gold and records; it never executes Agent/tools. Inputs/outputs are saved in `judgements/`. Gold is agent-authored synthetic oracle, **not independently human reviewed**. These are system-evaluation scores with judge uncertainty, not scientifically validated facts.\n",
        "## Table 1 — Overall Frozen Benchmark\n",
        table(["Metric","Result"],[(k,format_metric(v)) for k,v in s.items()]),
        "## Table 2 — Performance by task type\n",
        table(["Type","N","Success","Median / P95 system seconds","Mean tokens / tools"],[(x["type"],x["conversations"],format_metric(x["task_success"]),f"{x['latency_ms']['median']/1000:.2f} / {x['latency_ms']['p95']/1000:.2f}",f"{x['tokens_observed']['mean']:.1f} / {x['tool_calls']['mean']:.2f}") for x in types])]
    for i,(variant,_,title) in enumerate(configurations,3):
        text.extend([f"## Table {i} — {title}\n",table(["Metric","Baseline","Full","Absolute delta","Relative delta"],[(r["metric"],r["baseline"],r["full"],r["absolute_delta"],r["relative_delta"]) for r in abl if r["experiment"]==variant])])
    text.extend(["## Table 7 — Security and state\n",table(["Metric","Result"],[(k,format_metric(s[k])) for k in ["security_block","state_isolation","state_association","archived_state","hitl_resume","unsafe_actions"]]),
        "## Table 8 — Efficiency\n",table(["Type","Latency (milliseconds)","Observed tokens","ToolCalls"],[(x["type"],format_metric(x["latency_ms"]),format_metric(x["tokens_observed"]),format_metric(x["tool_calls"])) for x in types]),
        "## Hard invariants, failures and limitations\n",
        f"Unsafe successful actions: {s['unsafe_actions']}. Cross-user boundary checks and same-user fresh-conversation checks are shown separately in raw records; do not infer all possible attack routes were tested. FULL fallback conversations: {s['fallback_conversations']}. Usage-unreported provider attempts: {s['unreported_usage_calls']}. Observed tokens include failed/length-rejected responses and provider retries; when usage is absent, observed tokens are a **lower bound**, never a fabricated exact total.\n",
        "See `failures/failed_cases.md`: all product failures remain in denominators. No product/prompt/fixture/gold changes after freeze; hash verification occurs before every experiment. Infrastructure defects, if any, must invalidate affected experiments and require a new freeze/version, not selective reruns.\n",
        "Planning scores apply only to tasks needing plans; simple no-plan responses are N/A. Recovery rate is conditional on actual recoverable failure/rejection observed within the preselected replan-sensitive cases; a run with no failure is not labelled recovered. Gold defines acceptable capabilities/results rather than one exact tool sequence. Tool UNNECESSARY covers duplicate successful requests and state-sufficient follow-up reexecution; semantic irrelevance beyond those rules is not claimed perfectly measured. Skill recall uses predeclared acceptable labels and is not SOP causal attribution.\n",
        "Efficiency counts cover entire conversations (including initial analyses). Follow-up-only per-turn costs remain in raw records/provider timestamps; conversation-average savings must not be represented as isolated follow-up savings. Ratios use 95% Wilson intervals; overlapping task tags must not be added to make 60. Paired delta uses the same case IDs. Ratios with denominator 0 are N/A.\n",
        "## Reproducibility\n",
        "`benchmark_manifest.jsonl` identifies cases/subsets; `gold/cases.jsonl` specifies targets; `fixtures/snapshot.json` and the dedicated `phase4_eval_v2` DB identify synthetic data. `freeze_manifest.json` stores file/DB hashes and actual limits. `runs/` holds raw UI/SSE/persisted task records, downloads and screenshots; `provider_attempts.jsonl` counts every SDK HTTP attempt (both httpx and httpx2). `inspections/` holds read-only PostgreSQL checkpoint checks. `metrics/per_case.json` links all counts to IDs. `judgements/` persists evaluator inputs, rubric, responses and usage (excluded from Agent costs).\n",
        "![Task success](figures/01-task-success.png)\n",
        "![Skill](figures/02-no_skill.png)\n![Replan](figures/03-no_replan.png)\n![Follow-up](figures/04-stateless_followup.png)\n![Evidence](figures/05-no_evidence_gate.png)\n![Efficiency](figures/06-efficiency.png)\n"])
    (REPORT/"README.md").write_text("\n".join(text),encoding="utf-8")
    print("Eight tables, six figures and README generated from actual frozen metrics")


if __name__=="__main__":main()
