"""
Prove-it-fails for the fixes of the second review of step 5a-3 (findings 5, 6 and 10).

Each protection is switched off with monkeypatch (or replaced by the code it replaced) and the
shared helper in tests/security/upload_assertions.py must RAISE. That shows the helper checks the
protection and does not pass by accident.

Test database only: nothing here touches Postgres or Redis. Files go to pytest's tmp_path.
"""

import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core import upload_security
from app.services import document_store as ds
from app.services.document_store import LocalPrivateStore
from tests.security.upload_assertions import (
    assert_failed_write_leaves_nothing,
    assert_slide_cap_counts_slide_list_entries,
    assert_zip_member_names_rejected,
)

TENANT = uuid.uuid4()


# ─── Zip member names (finding 5) ────────────────────────────────────────────

def test_baseline_zip_name_helper_passes() -> None:
    assert_zip_member_names_rejected()


def test_zip_name_helper_fails_when_the_unsafe_name_check_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upload_security, "_unsafe_member_name", lambda name: False)
    with pytest.raises(AssertionError, match="member names were accepted"):
        assert_zip_member_names_rejected()


def test_zip_name_helper_fails_when_collisions_are_no_longer_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The collision check is switched off: the second of two entries silently replaces the first."""
    monkeypatch.setattr(upload_security, "_reject_collision", lambda members, key: None)
    with pytest.raises(AssertionError, match="collision|wrong code"):
        assert_zip_member_names_rejected()


def test_zip_name_helper_fails_when_only_ascii_lowercasing_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """The previous behaviour (name.lower()) catches case variants but not NFC/NFD pairs."""
    monkeypatch.setattr(upload_security, "_name_key", lambda name: name.lower())
    with pytest.raises(AssertionError, match="collision 2"):
        assert_zip_member_names_rejected()


# ─── Slide count (finding 6) ─────────────────────────────────────────────────

def test_baseline_slide_helper_passes() -> None:
    assert_slide_cap_counts_slide_list_entries()


def test_slide_helper_fails_when_sld_id_entries_are_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upload_security, "PRESENTATION_NS", "urn:not-the-presentation-namespace")
    with pytest.raises(AssertionError, match="101 slides was accepted"):
        assert_slide_cap_counts_slide_list_entries()


# ─── Atomic write (finding 10) ───────────────────────────────────────────────

async def test_baseline_atomic_write_helper_passes(tmp_path: Path) -> None:
    await assert_failed_write_leaves_nothing(LocalPrivateStore(tmp_path / "p"), tmp_path / "p", TENANT)


def _old_write_new(target: Path, data: Any) -> None:
    """The non-atomic write the store used before the review: create the final file, then write into it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "xb") as fh:
        fh.write(data)


async def test_atomic_write_helper_fails_for_the_old_direct_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(LocalPrivateStore, "_write_new", staticmethod(_old_write_new))
    with pytest.raises(AssertionError, match="left files behind"):
        await assert_failed_write_leaves_nothing(LocalPrivateStore(tmp_path / "p"), tmp_path / "p", TENANT)


async def test_atomic_write_helper_fails_when_the_temp_file_is_not_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ds.os, "unlink", lambda path: None)
    with pytest.raises(AssertionError, match="left files behind"):
        await assert_failed_write_leaves_nothing(LocalPrivateStore(tmp_path / "p"), tmp_path / "p", TENANT)
    # The leftover really is the temp file of the failed write
    left = [p.name for p in (tmp_path / "p").rglob("*") if p.is_file()]
    assert left and all(n.endswith(".tmp") for n in left), left
