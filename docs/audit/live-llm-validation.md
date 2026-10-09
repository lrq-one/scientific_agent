# Live model validation gate

`live_llm_manual` remains opt-in and default-off. This round made no real Qwen
or OpenAI request and makes no live token/cost/latency claim. Before enabling
it, record model/version, budgets, timeout, retry limit, allowed case IDs and an
isolated data scope; retain failed traces.
