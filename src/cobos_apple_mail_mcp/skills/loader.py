"""Recipe loader: discovers `skills/<name>/recipe.yaml` + `prompt.md` and
registers each as an MCP prompt (CLAUDE.md knowledge map: Resources and
prompts-recipes) — the Spark-CLI-style packaged-workflow model, implemented
natively as MCP prompts rather than a parallel invocation channel, so any
MCP client gets them automatically. Also drives
`apple-mail-mcp recipe list|run <name>`.

Recipe argument names come from our own packaged `recipe.yaml` files (never
from runtime user input), so building a per-recipe function signature via
`__signature__` is safe and lets FastMCP's normal introspection produce the
right prompt argument schema without any exec()-based code generation.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from importlib import resources
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from fastmcp import FastMCP

_SKILLS_PACKAGE = "cobos_apple_mail_mcp.skills"


@dataclass
class RecipeArgument:
    name: str
    required: bool = False
    description: str = ""
    default: str | None = None


@dataclass
class Recipe:
    name: str
    description: str
    arguments: list[RecipeArgument] = field(default_factory=list)
    uses_tools: list[str] = field(default_factory=list)
    uses_resources: list[str] = field(default_factory=list)
    prompt_template: str = "prompt.md"


def _skills_root():
    return resources.files(_SKILLS_PACKAGE)


def discover_recipes() -> list[Recipe]:
    root = _skills_root()
    recipes: list[Recipe] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        recipe_yaml = entry / "recipe.yaml"
        if not recipe_yaml.is_file():
            continue
        data = yaml.safe_load(recipe_yaml.read_text("utf-8"))
        args = [RecipeArgument(**a) for a in data.get("arguments", [])]
        recipes.append(
            Recipe(
                name=data["name"],
                description=data.get("description", ""),
                arguments=args,
                uses_tools=data.get("uses_tools", []),
                uses_resources=data.get("uses_resources", []),
                prompt_template=data.get("prompt_template", "prompt.md"),
            )
        )
    return sorted(recipes, key=lambda r: r.name)


def get_recipe(name: str) -> Recipe | None:
    return next((r for r in discover_recipes() if r.name == name), None)


def render_recipe(recipe: Recipe, values: dict[str, Any]) -> str:
    template_path = _skills_root() / recipe.name / recipe.prompt_template
    text = template_path.read_text("utf-8")
    for arg in recipe.arguments:
        value = values.get(arg.name)
        if value is None:
            value = arg.default or ""
        text = text.replace("{{" + arg.name + "}}", str(value))
    return text


def _build_prompt_function(recipe: Recipe):
    params = []
    for arg in recipe.arguments:
        default = inspect.Parameter.empty if arg.required else (arg.default or None)
        annotation = str if arg.required else (str | None)
        params.append(
            inspect.Parameter(
                arg.name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=annotation,
            )
        )

    def fn(**kwargs: Any) -> str:
        return render_recipe(recipe, kwargs)

    fn.__name__ = recipe.name.replace("-", "_")
    fn.__doc__ = recipe.description
    fn.__signature__ = inspect.Signature(params, return_annotation=str)
    # FastMCP's schema generation resolves parameter types via
    # typing.get_type_hints(), which reads __annotations__ directly rather
    # than inspect.signature() — __signature__ alone isn't enough.
    fn.__annotations__ = {p.name: p.annotation for p in params} | {"return": str}
    return fn


def register_prompts(mcp: FastMCP) -> list[Recipe]:
    recipes = discover_recipes()
    for recipe in recipes:
        fn = _build_prompt_function(recipe)
        mcp.prompt(name=recipe.name, description=recipe.description)(fn)
    return recipes
