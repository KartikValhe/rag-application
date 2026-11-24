"""
Step 6a-2 (docs/phase6-plan.md 7.2, R14): app/llm/models.json and its schema validation.

Strict on purpose: unknown keys, a missing pin (model id, date it was pinned), a missing or non-boolean
trains_on_inputs, a provider outside the allow-list, a duplicate and a training provider whose flag was
flipped to false are all refused with a ModelRegistryError.
"""

import copy
import json
from pathlib import Path

import pytest

from app.llm.errors import ModelRegistryError
from app.llm.models import DEFAULT_MODELS_PATH, ModelRegistry, load_registry, parse_registry
from app.llm.providers import ALLOWED_PROVIDERS

pytestmark = pytest.mark.usefixtures("no_network_at_all")

GOOD_ENTRY: dict[str, object] = {
    "provider": "groq",
    "model_id": "test-model-1",
    "purposes": ["answer"],
    "trains_on_inputs": False,
    "rpm": 30,
    "rpd": 1000,
    "pinned_on": "2026-11-24",
}


def doc(*entries: dict[str, object]) -> dict[str, object]:
    return {"schema_version": 1, "models": [copy.deepcopy(e) for e in entries]}


def entry(**overrides: object) -> dict[str, object]:
    e = copy.deepcopy(GOOD_ENTRY)
    e.update(overrides)
    return e


# ── the shipped file ─────────────────────────────────────────────────────────────────────────────────


def test_the_shipped_file_loads_and_every_entry_obeys_the_rules() -> None:
    assert DEFAULT_MODELS_PATH.name == "models.json" and DEFAULT_MODELS_PATH.parent.name == "llm"
    registry = load_registry()
    for model in registry.models:
        assert model.provider in ALLOWED_PROVIDERS
        assert model.key == f"{model.provider}:{model.model_id}"


def test_a_good_entry_is_accepted_and_found_by_key() -> None:
    registry = parse_registry(doc(GOOD_ENTRY))
    assert isinstance(registry, ModelRegistry)
    model = registry.get("groq:test-model-1")
    assert model is not None
    assert (model.rpm, model.rpd, model.trains_on_inputs) == (30, 1000, False)
    assert model.purposes == ("answer",)
    assert registry.get("groq:other") is None
    assert registry.get("test-model-1") is None


def test_a_model_id_with_slashes_and_colons_is_one_key() -> None:
    # OpenRouter ids look like "vendor/model:free": the key splits on the FIRST colon only
    e = entry(provider="openrouter", model_id="vendor/some-model:free")
    assert parse_registry(doc(e)).get("openrouter:vendor/some-model:free") is not None


# ── schema validation ────────────────────────────────────────────────────────────────────────────────


def test_an_unknown_top_level_key_is_refused() -> None:
    bad = doc(GOOD_ENTRY)
    bad["tools"] = []
    with pytest.raises(ModelRegistryError, match="tools"):
        parse_registry(bad)


def test_an_unknown_key_in_an_entry_is_refused() -> None:
    with pytest.raises(ModelRegistryError, match="tools"):
        parse_registry(doc(entry(tools=["x"])))
    with pytest.raises(ModelRegistryError, match="zdr"):
        parse_registry(doc(entry(zdr=True)))


@pytest.mark.parametrize("missing", ["provider", "model_id", "purposes", "trains_on_inputs", "rpm", "rpd", "pinned_on"])
def test_a_missing_field_is_refused_so_nothing_is_unpinned(missing: str) -> None:
    e = entry()
    del e[missing]
    with pytest.raises(ModelRegistryError, match=missing):
        parse_registry(doc(e))


@pytest.mark.parametrize("missing", ["schema_version", "models"])
def test_a_missing_top_level_field_is_refused(missing: str) -> None:
    bad = doc(GOOD_ENTRY)
    del bad[missing]
    with pytest.raises(ModelRegistryError):
        parse_registry(bad)


def test_the_schema_version_must_be_the_known_one() -> None:
    for version in (0, 2, "1", None):
        bad = doc(GOOD_ENTRY)
        bad["schema_version"] = version
        with pytest.raises(ModelRegistryError):
            parse_registry(bad)


@pytest.mark.parametrize(
    "bad",
    [
        {"model_id": ""},
        {"model_id": "has space"},
        {"model_id": "x" * 200},
        {"model_id": 5},
        {"purposes": []},
        {"purposes": ["translate"]},
        {"purposes": "answer"},
        {"purposes": ["answer", "answer"]},
        {"trains_on_inputs": "false"},
        {"trains_on_inputs": 0},
        {"trains_on_inputs": None},
        {"rpm": 0},
        {"rpm": -1},
        {"rpm": True},
        {"rpm": 1.5},
        {"rpm": "30"},
        {"rpd": 0},
        {"pinned_on": "yesterday"},
        {"pinned_on": "2026-13-40"},
        {"pinned_on": ""},
        {"pinned_on": None},
    ],
)
def test_wrong_values_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ModelRegistryError):
        parse_registry(doc(entry(**bad)))


@pytest.mark.parametrize("provider", ["evilcorp", "fake", "Groq", "", "groq:", 7])
def test_a_provider_outside_the_allow_list_is_refused(provider: object) -> None:
    with pytest.raises(ModelRegistryError, match="provider"):
        parse_registry(doc(entry(provider=provider)))


def test_a_duplicate_key_is_refused() -> None:
    with pytest.raises(ModelRegistryError, match="duplicate"):
        parse_registry(doc(entry(), entry(rpm=5)))


def test_a_training_provider_cannot_be_marked_as_not_training() -> None:
    # the flag is what the start-up policy reads: Gemini's free tier trains on inputs, so the file may not say otherwise
    with pytest.raises(ModelRegistryError, match="trains_on_inputs"):
        parse_registry(doc(entry(provider="gemini", model_id="flash-x", trains_on_inputs=False)))
    ok = parse_registry(doc(entry(provider="gemini", model_id="flash-x", trains_on_inputs=True)))
    assert ok.get("gemini:flash-x") is not None and ok.get("gemini:flash-x").trains_on_inputs is True  # type: ignore[union-attr]


def test_the_error_names_the_place_and_never_echoes_the_value() -> None:
    canary = "VALUE-CANARY-6642"
    with pytest.raises(ModelRegistryError) as caught:
        parse_registry(doc(entry(rpm=canary)))
    assert "rpm" in str(caught.value)
    assert canary not in str(caught.value)


@pytest.mark.parametrize("not_a_document", [[], "text", 5, None, []])
def test_the_file_must_be_an_object(not_a_document: object) -> None:
    with pytest.raises(ModelRegistryError):
        parse_registry(not_a_document)


# ── step 6a-4: optional token limits tpm / tpd (data only, not enforced yet) ──────────────────────────


def test_tpm_and_tpd_are_optional_and_none_when_absent() -> None:
    model = parse_registry(doc(GOOD_ENTRY)).get("groq:test-model-1")
    assert model is not None
    assert (model.tpm, model.tpd) == (None, None)


def test_tpm_and_tpd_may_be_null() -> None:
    model = parse_registry(doc(entry(tpm=None, tpd=None))).get("groq:test-model-1")
    assert model is not None
    assert (model.tpm, model.tpd) == (None, None)


def test_tpm_and_tpd_are_read_when_given() -> None:
    model = parse_registry(doc(entry(tpm=8000, tpd=200_000))).get("groq:test-model-1")
    assert model is not None
    assert (model.tpm, model.tpd) == (8000, 200_000)
    one = parse_registry(doc(entry(tpm=8000))).get("groq:test-model-1")
    assert one is not None and (one.tpm, one.tpd) == (8000, None)


@pytest.mark.parametrize("field", ["tpm", "tpd"])
@pytest.mark.parametrize("bad", [0, -1, True, False, 1.5, "8000", [8000], 10**10])
def test_tpm_and_tpd_must_be_positive_integers(field: str, bad: object) -> None:
    with pytest.raises(ModelRegistryError, match=field):
        parse_registry(doc(entry(**{field: bad})))


def test_the_shipped_file_still_loads_with_the_new_optional_fields() -> None:
    for model in load_registry().models:
        assert model.tpm is None or model.tpm > 0
        assert model.tpd is None or model.tpd > 0


# ── reading the file ─────────────────────────────────────────────────────────────────────────────────


def test_load_reads_a_file_and_refuses_bad_json_and_missing_files(tmp_path: Path) -> None:
    good = tmp_path / "models.json"
    good.write_text(json.dumps(doc(GOOD_ENTRY)), encoding="utf-8")
    assert load_registry(good).get("groq:test-model-1") is not None

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ModelRegistryError, match="JSON"):
        load_registry(broken)

    with pytest.raises(ModelRegistryError, match="read"):
        load_registry(tmp_path / "missing.json")


def test_duplicate_keys_inside_the_json_text_are_refused(tmp_path: Path) -> None:
    # json.loads keeps the last duplicate silently; a repeated "trains_on_inputs" could hide the first value
    path = tmp_path / "models.json"
    path.write_text(
        '{"schema_version": 1, "models": [{"provider": "groq", "model_id": "m", "purposes": ["answer"], '
        '"trains_on_inputs": true, "trains_on_inputs": false, "rpm": 1, "rpd": 1, "pinned_on": "2026-11-24"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ModelRegistryError, match="duplicate"):
        load_registry(path)
