from __future__ import annotations

import pytest

from cobos_apple_mail_mcp.config import load_config
from cobos_apple_mail_mcp.core.errors import ConfirmationRequired, ReadOnlyMode
from cobos_apple_mail_mcp.write import rules
from tests.helpers import FakeJXAExecutor


def _cfg(**overrides):
    # config_path to a nonexistent file so tests use pure built-in defaults,
    # not whatever config.toml happens to exist on the machine running them.
    return load_config(cli_overrides=overrides, config_path="/nonexistent/config.toml", environ={})


_RAW_RULES = {
    "rules": [
        {
            "name": "Work filter",
            "enabled": True,
            "allConditionsMustBeMet": False,
            "conditions": [
                {
                    "ruleType": "from header",
                    "qualifier": "does contain value",
                    "expression": "boss@work.com",
                    "header": "",
                }
            ],
            "actions": {
                "shouldMoveMessage": True,
                "moveMessage": "Work",
                "markFlagged": False,
                "markFlagIndex": -1,
                "colorMessage": None,
                "markRead": None,
            },
        }
    ]
}


def _jxa_with_rules():
    jxa = FakeJXAExecutor()
    jxa.on("listRules", lambda args: _RAW_RULES)
    jxa.on("setRuleEnabled", lambda args: {"name": args["name"], "enabled": args["enabled"]})
    jxa.on("deleteRule", lambda args: {"deleted": True, "name": args["name"]})
    return jxa


def test_list_rules_parses_conditions_and_actions():
    result = rules.list_rules(_jxa_with_rules())
    assert len(result) == 1
    rule = result[0]
    assert rule.name == "Work filter"
    assert rule.enabled is True
    assert rule.conditions[0].rule_type == "from header"
    assert rule.conditions[0].expression == "boss@work.com"
    assert rule.conditions[0].header is None  # empty string normalized to None
    # Null-valued action keys are dropped; set ones remain.
    assert rule.actions["moveMessage"] == "Work"
    assert "colorMessage" not in rule.actions
    assert "markRead" not in rule.actions


def test_enable_disable_rule():
    jxa = _jxa_with_rules()
    r = rules.set_rule_enabled(jxa, _cfg(), "Work filter", False)
    assert r["enabled"] is False
    calls = [args for name, args in jxa.calls if name == "setRuleEnabled"]
    assert calls[-1] == {"name": "Work filter", "enabled": False}


def test_enable_rule_dry_run_makes_no_call():
    jxa = _jxa_with_rules()
    r = rules.set_rule_enabled(jxa, _cfg(), "Work filter", True, dry_run=True)
    assert r["dry_run"] is True
    assert not any(name == "setRuleEnabled" for name, _ in jxa.calls)


def test_enable_rule_blocked_read_only():
    jxa = FakeJXAExecutor()  # no handlers -> any call raises
    with pytest.raises(ReadOnlyMode):
        rules.set_rule_enabled(jxa, _cfg(server={"read_only": True}), "Work filter", True)
    assert jxa.calls == []


def test_delete_rule_requires_confirm():
    jxa = _jxa_with_rules()
    with pytest.raises(ConfirmationRequired):
        rules.delete_rule(jxa, _cfg(), "Work filter", confirm=False)
    assert not any(name == "deleteRule" for name, _ in jxa.calls)

    r = rules.delete_rule(jxa, _cfg(), "Work filter", confirm=True)
    assert r["deleted"] is True


def test_delete_rule_dry_run_previews_without_deleting():
    jxa = _jxa_with_rules()
    r = rules.delete_rule(jxa, _cfg(), "Work filter", dry_run=True)
    assert r["dry_run"] is True
    assert r["reversible"] is False
    assert not any(name == "deleteRule" for name, _ in jxa.calls)


def test_delete_rule_blocked_read_only():
    jxa = FakeJXAExecutor()
    with pytest.raises(ReadOnlyMode):
        rules.delete_rule(jxa, _cfg(server={"read_only": True}), "Work filter", confirm=True)
    assert jxa.calls == []
