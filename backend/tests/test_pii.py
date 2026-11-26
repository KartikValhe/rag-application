"""
Step 6a-10a: PII masking of what goes to the model (docs/phase6-plan.md 9.5 item 1, design B2).

The question is stored as typed; the LLM gets a masked copy: e-mail addresses, +91 and 10-digit phone numbers, 12-digit
ID numbers, and the caller's own name and e-mail. Placeholders are not restored. Pure module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services import pii

MODULE = Path(pii.__file__)


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("mail me at ravi.k+x@example.co.in please", f"mail me at {pii.EMAIL} please"),
        ("A@B.ORG and c_d@e-f.net", f"{pii.EMAIL} and {pii.EMAIL}"),
        ("call +91 98765 43210 now", f"call {pii.PHONE} now"),
        ("call +91-9876543210 now", f"call {pii.PHONE} now"),
        ("call +919876543210 now", f"call {pii.PHONE} now"),
        ("call 9876543210.", f"call {pii.PHONE}."),
        ("call 98765-43210.", f"call {pii.PHONE}."),
        ("फोन ९८७६५४३२१० वर", f"फोन {pii.PHONE} वर"),  # Devanagari digits
        ("aadhaar 1234 5678 9012 ok", f"aadhaar {pii.ID_NUMBER} ok"),
        ("aadhaar 1234-5678-9012 ok", f"aadhaar {pii.ID_NUMBER} ok"),
        ("aadhaar 123456789012 ok", f"aadhaar {pii.ID_NUMBER} ok"),
    ],
)
def test_contact_details_and_id_numbers_are_masked(text: str, masked: str) -> None:
    assert pii.mask(text) == masked


@pytest.mark.parametrize(
    "text",
    [
        "What is a prop in React?",
        "Explain the year 2024 and chapter 12.",
        "Is 123456789 a nine digit number?",  # 9 digits: not a phone
        "Is 12345678901 an eleven digit number?",  # 11 digits: neither a phone nor an ID
        "Is 1234567890123 thirteen digits?",
        "प्रॉप्स म्हणजे काय?",
        "an @ sign alone, a.b@c",
    ],
)
def test_ordinary_text_is_unchanged(text: str) -> None:
    assert pii.mask(text) == text


def test_the_callers_own_name_and_email_are_masked() -> None:
    text = "I am Ravi Kulkarni (RAVI), write to ravi@school.example, not Ravindra."
    out = pii.mask(text, own_name="Ravi Kulkarni", own_email="ravi@school.example")
    assert out == f"I am {pii.NAME} ({pii.NAME}), write to {pii.EMAIL}, not Ravindra."


def test_a_devanagari_name_is_masked_as_a_whole_word_only() -> None:
    out = pii.mask("माझे नाव रवी आहे, रवींद्र नाही", own_name="रवी")
    assert out == f"माझे नाव {pii.NAME} आहे, रवींद्र नाही"


def test_name_parts_shorter_than_three_characters_are_not_masked_alone() -> None:
    out = pii.mask("Al is here, and so is Jo Al", own_name="Jo Al")
    assert out == f"Al is here, and so is {pii.NAME}"


def test_an_own_email_that_the_pattern_would_miss_is_still_masked() -> None:
    assert pii.mask("write to odd!addr@localhost", own_email="odd!addr@localhost") == f"write to {pii.EMAIL}"


def test_no_name_and_no_email_mask_only_the_patterns() -> None:
    assert pii.mask("Ravi", own_name=None, own_email=None) == "Ravi"
    assert pii.mask("Ravi", own_name="", own_email="") == "Ravi"


def test_masking_is_idempotent_and_placeholders_are_not_labels() -> None:
    once = pii.mask("ravi@x.org 9876543210 Ravi", own_name="Ravi")
    assert pii.mask(once, own_name="Ravi") == once
    for placeholder in (pii.EMAIL, pii.PHONE, pii.ID_NUMBER, pii.NAME):
        assert not placeholder.startswith("[S")


def test_masker_binds_the_callers_details() -> None:
    m = pii.masker(own_name="Ravi", own_email="r@x.org")
    assert m("Ravi r@x.org") == f"{pii.NAME} {pii.EMAIL}"


def test_the_module_is_pure() -> None:
    allowed = {"__future__", "re", "collections.abc", "typing"}
    found: set[str] = set()
    for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    assert found <= allowed, sorted(found - allowed)
