"""Bounded user-preference projection; never stores scientific task facts."""
from __future__ import annotations

from app.models.schemas import UserProfile


ALLOWED_PREFERENCE_KEYS = {"locale", "domain", "default_units", "response_detail"}


def task_profile_context(profile: UserProfile | None, *, relevant_fields: set[str] | None = None) -> dict:
    if profile is None:
        return {}
    fields = relevant_fields or ALLOWED_PREFERENCE_KEYS
    result = {"locale": profile.locale, "response_detail": profile.response_detail}
    if "domain" in fields and profile.domain:
        result["domain"] = profile.domain
    if "default_units" in fields and profile.default_units:
        result["default_units"] = dict(profile.default_units)
    return result


def validate_memory_write(key: str, *, explicit_user_confirmation: bool, is_task_fact: bool = False) -> bool:
    """Only explicit stable preferences may enter long-lived memory."""
    if is_task_fact or key not in ALLOWED_PREFERENCE_KEYS:
        return False
    return explicit_user_confirmation
