"""
Step 6a-3 (docs/phase6-plan.md 7.2, 7.7, R14): the pinned models.

models.json has no field for a source, so every entry must also be listed in the build record of
docs/phase6-plan.md with the official page it came from and the date. An entry with no source line fails
here. The ids and limits come from the providers' own documentation (see the build record); nothing here is
a guess.
"""

import re
from datetime import date
from pathlib import Path

from app.core.config import Settings
from app.llm.models import load_registry
from app.llm.policy import check_llm_policy
from app.llm.providers import ALLOWED_PROVIDERS

PLAN = Path(__file__).resolve().parents[2] / "docs" / "phase6-plan.md"
SECTION = re.compile(r"^### 6a-3 .*?(?=^### |\Z)", re.M | re.S)


def build_record_lines() -> list[str]:
    text = PLAN.read_text(encoding="utf-8")
    section = SECTION.search(text[text.index("## Build record") :])
    assert section is not None, "the build record has no 6a-3 entry"
    return section.group(0).splitlines()


def test_the_file_loads_and_every_entry_is_pinned_with_a_past_date() -> None:
    registry = load_registry()
    assert registry.models, "6a-3 pins the models whose id and limits were verified"
    for model in registry.models:
        assert date.fromisoformat(model.pinned_on) <= date.today()
        assert model.provider in ALLOWED_PROVIDERS
        assert model.rpm > 0 and model.rpd > 0


def test_every_pinned_model_has_a_source_line_with_a_url_the_date_and_the_same_limits() -> None:
    lines = build_record_lines()
    for model in load_registry().models:
        rows = [line for line in lines if f"`{model.key}`" in line]
        assert rows, f"{model.key}: no source line in the 6a-3 build record"
        row = " ".join(rows)
        assert "https://" in row, f"{model.key}: the source line has no URL"
        assert model.pinned_on in row, f"{model.key}: the source line does not carry the pin date"
        assert f"RPM {model.rpm}" in row and f"RPD {model.rpd}" in row, f"{model.key}: limits differ from the record"


def test_a_chain_of_the_pinned_answer_models_passes_the_start_up_policy() -> None:
    registry = load_registry()
    keys = [m.key for m in registry.models if "answer" in m.purposes and not m.trains_on_inputs]
    assert keys
    settings = Settings(_env_file=None, ENVIRONMENT="development", LLM_ANSWER_CHAIN=",".join(keys))  # type: ignore[call-arg]
    check_llm_policy(settings, registry)


def test_a_training_provider_is_never_marked_as_not_training() -> None:
    assert all(m.provider != "gemini" or m.trains_on_inputs for m in load_registry().models)
