"""Read-only command detection for AstrBot's message and LLM hooks.

AstrBot 4.28 resolves command filters in its waking stage, before adapter
handlers run.  Reuse those results instead of re-running filters: filters can
change event extras, validate arguments, send messages, or raise errors.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


# Compatibility for calls outside the normal waking stage (older adapters and
# direct plugin LLM requests). Registered metadata is authoritative when present.
_BUILTIN_COMMANDS = frozenset({
    "help", "sid", "name", "reset", "stop", "new", "stats", "provider",
    "dashboard_update", "set", "unset", "日程", "日程重建", "日程会话",
})
_COMMAND_TYPES = {
    ("astrbot.core.star.filter.command", "CommandFilter"),
    ("astrbot.core.star.filter.command_group", "CommandGroupFilter"),
}


def _extra(event, name):
    getter = getattr(event, "get_extra", None)
    if callable(getter):
        try:
            return getter(name)
        except (AttributeError, TypeError):
            pass
    extras = getattr(event, "_extras", getattr(event, "extras", {}))
    return extras.get(name) if isinstance(extras, Mapping) else None


def _is_command_filter(value) -> bool:
    # MRO keeps official subclasses working without importing the SDK into pure
    # helper tests or executing a filter's custom behavior.
    return any((base.__module__, base.__name__) in _COMMAND_TYPES
               for base in type(value).__mro__)


def _filters(handler):
    values = getattr(handler, "event_filters", ())
    return values if isinstance(values, (list, tuple)) else ()


def _names(value):
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [name for name in value if isinstance(name, str) and name.strip()]
    return []


def _complete_names(command_filter, seen=None) -> list[str]:
    """Read only fields owned by official command filters; never call filter()."""
    if not _is_command_filter(command_filter):
        return []
    seen = set() if seen is None else seen
    if id(command_filter) in seen or len(seen) >= 12:
        return []
    seen.add(id(command_filter))
    cached = _names(getattr(command_filter, "_cmpl_cmd_names", None))
    if cached:
        return cached
    command_name = getattr(command_filter, "command_name", None)
    aliases = _names(getattr(command_filter, "alias", ()))
    if isinstance(command_name, str):
        parents = _names(getattr(command_filter, "parent_command_names", ())) or [""]
        return [f"{parent} {name}".strip()
                for parent in parents for name in [command_name, *aliases] if name]
    group_name = getattr(command_filter, "group_name", None)
    if not isinstance(group_name, str):
        return []
    parent = getattr(command_filter, "parent_group", None)
    parents = _complete_names(parent, seen) if parent is not None else [""]
    return [f"{parent_name} {name}".strip()
            for parent_name in parents for name in [group_name, *aliases] if name]


def _registered_handlers(event):
    try:
        from astrbot.core.star.star_handler import EventType, star_handlers_registry

        return star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent,
            plugins_name=getattr(event, "plugins_name", None),
        )
    except (ImportError, AttributeError, TypeError):
        return ()


def _text_candidates(event, config) -> set[str]:
    text = getattr(event, "message_str", "")
    if not isinstance(text, str):
        return set()
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return set()
    candidates = {text}
    prefixes = _names(config.get("wake_prefix", ())) if isinstance(config, Mapping) else []
    # In the standard pipeline wake_prefix is already removed. Slash stripping
    # also handles a direct hook or test event that bypassed that stage.
    for prefix in [*prefixes, "/"]:
        if prefix and text.startswith(prefix):
            candidate = text[len(prefix):].strip()
            if candidate:
                candidates.add(candidate)
    return candidates


def is_command_event(event, config=None, handlers: Iterable | None = None) -> bool:
    """True for a parsed or registered command, without mutating the event.

    ``config`` should be the session's AstrBot config, so configured wake
    prefixes also work in nonstandard pipelines. ``handlers`` is an optional
    metadata iterable for compatibility adapters and isolated tests.
    """
    parsed = _extra(event, "handlers_parsed_params")
    if isinstance(parsed, Mapping) and parsed:
        return True
    activated = _extra(event, "activated_handlers")
    if isinstance(activated, (list, tuple)):
        if any(_is_command_filter(value)
               for handler in activated for value in _filters(handler)):
            return True

    candidates = _text_candidates(event, config)
    if not candidates:
        return False
    command_names = set(_BUILTIN_COMMANDS)
    for handler in _registered_handlers(event) if handlers is None else handlers:
        if not getattr(handler, "enabled", True):
            continue
        for command_filter in _filters(handler):
            command_names.update(_complete_names(command_filter))
    for name in command_names:
        name = re.sub(r"\s+", " ", name.strip())
        if name and any(text == name or text.startswith(name + " ") for text in candidates):
            return True
    return False
