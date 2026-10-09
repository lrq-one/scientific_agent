# LLM call safety gate — final acceptance addendum

The offline and `integration_isolated` profiles now install a process-level deny-by-default HTTP guard from `app/config.py:45-46`. The guard covers the `httpcore`/`httpcore2` transport boundary and the `httpx`/`httpx2` client boundary, so direct `TextToSQLService` construction cannot bypass it. In-process Mock/ASGI/TestClient transports remain allowed; only the explicitly isolated MinIO ports (9002/9003) and PostgreSQL port (55433) are allowed as local service endpoints.

The focused gate is `tests/test_llm_network_guard.py` (3 passed). It proves: a real model-shaped request is rejected before socket I/O, an in-process fake transport is allowed, and a direct Text2SQL provider is rejected. Formal offline and isolated acceptance commands clear all model variables and use only fixed fake providers; those runs made 0 real model calls. The guard-negative tests intentionally produce blocked events, which are not provider calls.

One earlier manual diagnostic run, before the gate was installed, made one accidental real Text2SQL request; it is excluded from acceptance results and remains recorded in `production-validation-readiness.md`. A later negative test attempted a fake endpoint and was stopped by the new guard before socket I/O. Neither incident is concealed.

`live_llm_manual` remains a separate, explicit, human-authorized profile and was not run.
