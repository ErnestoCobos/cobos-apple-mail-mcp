"""Mail rule read + lifecycle management (JXA-backed; rules aren't in the
on-disk index).

**Scope is deliberately read + lifecycle only.** Apple Mail's scripting
dictionary cannot create or modify rule *conditions* — verified live against a
real Mail.app: `make` on a `rule condition` raises "Can't make or move that
element into that container", and passing conditions via a rule's
`withProperties`/array assignment silently produces a rule with no conditions.
A conditionless rule is useless (or dangerous), so `create_rule`/`update_rule`
are not offered rather than shipped broken. `list_rules` reads everything;
`enable_rule`/`disable_rule`/`delete_rule` manage the lifecycle.

Lifecycle mutations pass through `core.safety.gate_nonbatch()` (read-only,
dry_run, confirm) — not `guard()`, which is built around message batches.
`delete_rule` is irreversible (Mail's scripting can't recreate a rule), so it
requires `confirm=true` by default (`config.confirmation.require_confirm`) and
is not journaled/undoable.
"""

from __future__ import annotations

from cobos_apple_mail_mcp.config import Config
from cobos_apple_mail_mcp.core.models import Rule, RuleCondition
from cobos_apple_mail_mcp.core.safety import gate_nonbatch
from cobos_apple_mail_mcp.write.jxa_executor import JXAExecutor


def _to_rule(raw: dict) -> Rule:
    conditions = [
        RuleCondition(
            rule_type=c.get("ruleType"),
            qualifier=c.get("qualifier"),
            expression=c.get("expression"),
            header=c.get("header") or None,
        )
        for c in raw.get("conditions", [])
    ]
    # Drop null-valued action keys so the output shows only actions the rule
    # actually sets, not the full nullable surface.
    actions = {k: v for k, v in (raw.get("actions") or {}).items() if v is not None}
    return Rule(
        name=raw.get("name", ""),
        enabled=bool(raw.get("enabled", False)),
        all_conditions_must_be_met=bool(raw.get("allConditionsMustBeMet", False)),
        conditions=conditions,
        actions=actions,
    )


def list_rules(jxa: JXAExecutor) -> list[Rule]:
    """Read every Mail rule (name, enabled, conditions, action properties).
    Read-only — no gating."""
    result = jxa.call("listRules", {})
    return [_to_rule(r) for r in (result or {}).get("rules", [])]


def set_rule_enabled(
    jxa: JXAExecutor, config: Config, name: str, enabled: bool, *, dry_run: bool = False
) -> dict:
    operation = "enable_rule" if enabled else "disable_rule"
    preview = {
        "dry_run": True,
        "operation": operation,
        "rule": name,
        "would_set_enabled": enabled,
    }
    gated = gate_nonbatch(config, operation, dry_run=dry_run, preview=preview)
    if gated is not None:
        return gated
    return jxa.call("setRuleEnabled", {"name": name, "enabled": enabled})


def delete_rule(
    jxa: JXAExecutor, config: Config, name: str, *, dry_run: bool = False, confirm: bool = False
) -> dict:
    # Always require confirm — a deleted rule cannot be recreated through Mail's
    # scripting at all (conditions aren't scriptable), so this is strictly more
    # destructive than a message delete. Enforced unconditionally rather than
    # via config.confirmation so an older config.toml (generated before this
    # feature existed) can't silently drop the confirmation gate. It's still
    # listed in the default require_confirm for discoverability.
    requires_confirm = True
    preview = {
        "dry_run": True,
        "operation": "delete_rule",
        "rule": name,
        "reversible": False,
        "note": "Mail's scripting cannot recreate a deleted rule; this is not undoable",
    }
    gated = gate_nonbatch(
        config,
        "delete_rule",
        dry_run=dry_run,
        confirm=confirm,
        requires_confirm=requires_confirm,
        preview=preview,
    )
    if gated is not None:
        return gated
    return jxa.call("deleteRule", {"name": name})
