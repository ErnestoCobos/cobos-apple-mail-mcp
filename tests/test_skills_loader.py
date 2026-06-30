from __future__ import annotations

from cobos_apple_mail_mcp.skills.loader import discover_recipes, get_recipe, render_recipe


def test_discover_recipes_finds_all_five_bundled():
    names = {r.name for r in discover_recipes()}
    assert names == {
        "daily-triage",
        "inbox-zero",
        "awaiting-reply",
        "weekly-review",
        "thread-catchup",
    }


def test_every_recipe_renders_with_no_unsubstituted_placeholders():
    for recipe in discover_recipes():
        text = render_recipe(recipe, {})
        assert "{{" not in text, f"{recipe.name} left an unsubstituted placeholder"
        assert len(text) > 50


def test_render_recipe_substitutes_given_value():
    recipe = get_recipe("daily-triage")
    text = render_recipe(recipe, {"account": "Personal"})
    assert "Personal" in text


def test_get_recipe_unknown_returns_none():
    assert get_recipe("does-not-exist") is None
