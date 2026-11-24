"""
app/llm/models.py
─────────────────
Reads and validates app/llm/models.json, the list of pinned models (docs/phase6-plan.md 7.2, R14).

The file is strict on purpose: unknown keys, a missing field (model id, pin date, `trains_on_inputs`, limits),
a wrong type, a provider outside the allow-list, a duplicate, or a training provider marked as not training are
all refused with a ModelRegistryError. The error names the place and the rule, never the value found.

The file ships EMPTY in step 6a-2. Step 6a-3 verifies the real model ids and free-tier limits against the
provider consoles and pins them here; a guessed id would read like a pin.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from app.llm.errors import ModelRegistryError
from app.llm.providers import ALLOWED_PROVIDERS, is_allowed_provider

DEFAULT_MODELS_PATH: Final = Path(__file__).with_name("models.json")

Purpose = Literal["answer", "condense"]
ModelId = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}$")]
Limit = Annotated[StrictInt, Field(gt=0, le=1_000_000_000)]


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    provider: StrictStr
    model_id: ModelId
    purposes: Annotated[list[Purpose], Field(min_length=1)]
    trains_on_inputs: StrictBool
    rpm: Limit
    rpd: Limit
    # Step 6a-4: token limits per minute and per day. Optional (null or absent) and data only: nothing enforces
    # them yet, a 429 cooldown handles an overrun.
    tpm: Limit | None = None
    tpd: Limit | None = None
    pinned_on: Annotated[StrictStr, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]

    @field_validator("provider")
    @classmethod
    def _provider_on_allow_list(cls, value: str) -> str:
        if not is_allowed_provider(value):
            raise ValueError("provider is not on the allow-list")
        return value

    @field_validator("purposes")
    @classmethod
    def _purposes_unique(cls, value: list[Purpose]) -> list[Purpose]:
        if len(set(value)) != len(value):
            raise ValueError("purposes must not repeat")
        return value

    @field_validator("pinned_on")
    @classmethod
    def _pin_is_a_real_date(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("pinned_on must be a real date (YYYY-MM-DD)") from None
        return value

    @model_validator(mode="after")
    def _training_flag_matches_the_provider(self) -> _Entry:
        if ALLOWED_PROVIDERS[self.provider].always_trains_on_inputs and not self.trains_on_inputs:
            raise ValueError("trains_on_inputs must be true for a provider that trains on inputs")
        return self


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    models: list[_Entry]


@dataclass(frozen=True, slots=True)
class RegisteredModel:
    provider: str
    model_id: str
    purposes: tuple[str, ...]
    trains_on_inputs: bool
    rpm: int
    rpd: int
    pinned_on: str
    tpm: int | None = None  # data only (step 6a-4), not enforced
    tpd: int | None = None

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model_id}"


@dataclass(frozen=True, slots=True)
class ModelRegistry:
    models: tuple[RegisteredModel, ...]

    def get(self, key: str) -> RegisteredModel | None:
        for model in self.models:
            if model.key == key:
                return model
        return None


def _describe(error: ValidationError) -> list[str]:
    """One line per problem: where it is and which rule broke. The value found is never included."""
    lines = []
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        where = ".".join(str(part) for part in item["loc"]) or "file"
        lines.append(f"models.json {where}: {item['msg']}")
    return lines


def parse_registry(data: object) -> ModelRegistry:
    try:
        parsed = _File.model_validate(data)
    except ValidationError as exc:
        raise ModelRegistryError(_describe(exc)) from None
    seen: set[str] = set()
    problems: list[str] = []
    models: list[RegisteredModel] = []
    for index, entry in enumerate(parsed.models):
        model = RegisteredModel(
            provider=entry.provider,
            model_id=entry.model_id,
            purposes=tuple(entry.purposes),
            trains_on_inputs=entry.trains_on_inputs,
            rpm=entry.rpm,
            rpd=entry.rpd,
            pinned_on=entry.pinned_on,
            tpm=entry.tpm,
            tpd=entry.tpd,
        )
        if model.key in seen:
            problems.append(f"models.json models.{index}: duplicate model key {model.key}")
        seen.add(model.key)
        models.append(model)
    if problems:
        raise ModelRegistryError(problems)
    return ModelRegistry(models=tuple(models))


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModelRegistryError([f"models.json: duplicate key {key!r} in the JSON text"])
        result[key] = value
    return result


def load_registry(path: Path = DEFAULT_MODELS_PATH) -> ModelRegistry:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ModelRegistryError(["models.json: cannot read the file"]) from None
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except ValueError:  # a duplicate key raises ModelRegistryError itself and passes through
        raise ModelRegistryError(["models.json: not valid JSON"]) from None
    return parse_registry(data)
