"""Commands must remain available while a persona's schedule is busy."""
from __future__ import annotations

import copy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_persona_schedule.commands import is_command_event


class CommandFilter:
    __module__ = "astrbot.core.star.filter.command"

    def __init__(self, name="plugin", alias=(), parents=None):
        self.command_name = name
        self.alias = set(alias)
        self.parent_command_names = parents or [""]
        self._cmpl_cmd_names = None

    def filter(self, *args):
        raise AssertionError("A schedule check must not run command filters")

    def get_complete_command_names(self):
        raise AssertionError("Only command metadata fields should be read")


class CommandGroupFilter:
    __module__ = "astrbot.core.star.filter.command_group"

    def __init__(self, name="manage", alias=(), parent=None):
        self.group_name = name
        self.alias = set(alias)
        self.parent_group = parent


class Event:
    def __init__(self, message="你好", **extras):
        self.message_str = message
        self._extras = extras

    def get_extra(self, name, default=None):
        return self._extras.get(name, default)


def handler(*filters, enabled=True):
    return types.SimpleNamespace(event_filters=list(filters), enabled=enabled)


class CommandBypassTests(unittest.TestCase):
    def check(self, text, expected, config=None, handlers=()):
        self.assertEqual(is_command_event(Event(text), config, handlers), expected)

    def test_builtin_reset_with_and_without_pipeline_prefix(self):
        for text in ("reset", "/reset", " /reset ", "reset current"):
            with self.subTest(text=text):
                self.check(text, True)

    def test_configured_wake_prefixes(self):
        for text in ("!reset", "bot reset", "小助手 reset"):
            self.check(text, True, {"wake_prefix": ["!", "bot ", "小助手 "]})

    def test_parsed_empty_command_arguments_are_authoritative(self):
        event = Event("rewritten input", handlers_parsed_params={"plug_reset": {}})
        self.assertTrue(is_command_event(event, handlers=()))

    def test_activated_command_handler_is_authoritative(self):
        event = Event("rewritten input", activated_handlers=[handler(CommandFilter())])
        self.assertTrue(is_command_event(event, handlers=()))

    def test_generic_listener_does_not_make_all_messages_commands(self):
        event = Event("今天过得怎么样", activated_handlers=[handler(object())])
        self.assertFalse(is_command_event(event, handlers=()))

    def test_plugin_alias_and_arguments(self):
        handlers = [handler(CommandFilter("画卷", ["canvas"]))]
        for text in ("画卷", "/canvas", "canvas photo", "画卷 相册"):
            self.check(text, True, handlers=handlers)
        for text in ("画卷好看", "canvaslike", "想看看画卷"):
            self.check(text, False, handlers=handlers)

    def test_parent_command_names_include_aliases(self):
        handlers = [handler(CommandFilter("list", ["ls"], ["admin", "管理"]))]
        self.check("admin list", True, handlers=handlers)
        self.check("管理 ls arg", True, handlers=handlers)
        self.check("list", False, handlers=handlers)

    def test_nested_command_groups(self):
        parent = CommandGroupFilter("admin", ["管理"])
        child = CommandGroupFilter("role", ["人设"], parent)
        handlers = [handler(child)]
        self.check("管理 人设 list", True, handlers=handlers)
        self.check("admin role", True, handlers=handlers)
        self.check("admin roleplay", False, handlers=handlers)

    def test_cached_names_and_official_filter_subclass(self):
        class CustomCommand(CommandFilter):
            pass

        custom = CustomCommand()
        custom._cmpl_cmd_names = ["plugins show"]
        self.check("plugins show list", True, handlers=[handler(custom)])

    def test_custom_filter_is_not_called_or_guessed_as_command(self):
        class CustomFilter:
            def filter(self, *args):
                raise AssertionError("Side effect")

        self.check("普通聊天", False, handlers=[handler(CustomFilter())])

    def test_disabled_plugin_commands_are_not_matched_from_metadata(self):
        self.check("plugin arg", False, handlers=[handler(CommandFilter(), enabled=False)])

    def test_event_and_metadata_are_unchanged(self):
        cmd = CommandFilter("画卷", ["canvas"])
        event = Event(" /canvas photo ", handlers_parsed_params={})
        before_event = copy.deepcopy(event.__dict__)
        before_filter = copy.deepcopy(cmd.__dict__)
        self.assertTrue(is_command_event(event, handlers=[handler(cmd)]))
        self.assertEqual(event.__dict__, before_event)
        self.assertEqual(cmd.__dict__, before_filter)

    def test_command_boundary_and_empty_text(self):
        for text in ("", "  ", "/", "resetting", "/resetting", "please reset"):
            self.check(text, False)
        self.check("reset\ncurrent", True)

    def test_malformed_extras_and_group_cycles_are_safe(self):
        event = Event("你好", activated_handlers=True, handlers_parsed_params="bad")
        group = CommandGroupFilter()
        group.parent_group = group
        self.assertFalse(is_command_event(event, handlers=[handler(group)]))

    def test_registry_uses_session_enabled_plugins_without_running_filters(self):
        registry = Mock()
        registry.get_handlers_by_event_type.return_value = [handler(CommandFilter("抽卡"))]
        event = Event("抽卡 单抽")
        event.plugins_name = ["gacha"]
        metadata_module = types.ModuleType("astrbot.core.star.star_handler")
        metadata_module.EventType = types.SimpleNamespace(AdapterMessageEvent="message")
        metadata_module.star_handlers_registry = registry
        with patch.dict(sys.modules, {metadata_module.__name__: metadata_module}):
            self.assertTrue(is_command_event(event))
        registry.get_handlers_by_event_type.assert_called_once_with(
            "message", plugins_name=["gacha"])


if __name__ == "__main__":
    unittest.main()
