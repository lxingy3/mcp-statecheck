"""Load package-owned M5 application acceptance recipes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

RECIPE_VERSION = 2
RECIPE_KIND = "application-state"
_RECIPE_FIELDS = {"kind", "recipe_id", "version"}
FILESYSTEM_RECIPE_ID = "filesystem-2026.7.10-stdio-write-edit-boundary"
GIT_RECIPE_ID = "git-2026.8.18-stdio-stage-commit-boundary"
_RECIPES = {
    FILESYSTEM_RECIPE_ID: ("filesystem", "2026.7.10"),
    GIT_RECIPE_ID: ("git", "2026.8.18"),
}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate object key")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


@dataclass(frozen=True)
class ApplicationRecipe:
    """A validated identifier for one package-owned acceptance plan."""

    target: str
    recipe_id: str
    sha256: str

    @property
    def target_recipe(self) -> dict[str, object]:
        return {
            "kind": RECIPE_KIND,
            "recipe_id": self.recipe_id,
            "version": RECIPE_VERSION,
        }


def load_application_recipe(
    path: Path,
    *,
    target: str,
    target_version: str,
) -> ApplicationRecipe:
    """Load a strict recipe identifier without accepting executable input."""

    try:
        payload = path.read_bytes()
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("application recipe is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != _RECIPE_FIELDS:
        raise ValueError(
            "application recipe fields must be exactly kind, recipe_id, and version"
        )
    if type(value["version"]) is not int or value["version"] != RECIPE_VERSION:
        raise ValueError(
            f"unsupported application recipe version; expected {RECIPE_VERSION}"
        )
    if value["kind"] != RECIPE_KIND:
        raise ValueError(f"unsupported application recipe kind; expected {RECIPE_KIND}")
    recipe_id = value["recipe_id"]
    if not isinstance(recipe_id, str) or _RECIPES.get(recipe_id) != (
        target,
        target_version,
    ):
        raise ValueError(f"application recipe is not allowlisted for {target}")
    return ApplicationRecipe(
        target=target,
        recipe_id=recipe_id,
        sha256=hashlib.sha256(payload).hexdigest(),
    )
