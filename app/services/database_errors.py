"""Classify PostgreSQL physical storage faults without weakening SQL validation.

A failed database page/index cannot be fixed by changing an Agent plan or
regenerating SQL. Classification requires an actual psycopg exception.
"""
from __future__ import annotations

import psycopg


def is_database_storage_corruption(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, psycopg.Error):
            sqlstate = getattr(current, "sqlstate", None)
            if sqlstate in {"XX001", "XX002"}:
                return True
            description = str(current).lower()
            if any(fragment in description for fragment in (
                "invalid page in block", "invalid page header",
                "could not read block", "unexpected data beyond eof",
            )):
                return True
        current = current.__cause__ or current.__context__
    return False
