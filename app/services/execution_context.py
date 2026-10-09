from contextvars import ContextVar

# Identifiers only; all scientific data and tool scopes stay in explicit Agent State.
execution_identity: ContextVar[dict[str, str]] = ContextVar("execution_identity", default={})
