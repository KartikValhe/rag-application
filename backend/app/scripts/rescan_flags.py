"""
app/scripts/rescan_flags.py
───────────────────────────
Operator command (review of 5d-2, finding 4): scan the STORED chunks of one tenant's ready and published
versions with the current injection rules, and, only with --apply, write chunks.injection_flags and
document_versions.flagged_chunk_count.

    python -m app.scripts.rescan_flags <tenant slug>            dry run: prints what would change
    python -m app.scripts.rescan_flags <tenant slug> --apply    writes the changes

Who needs it:
  - versions indexed before 5d-2 have flagged_chunk_count NULL ("never scanned"). Publishing them needs
    acknowledge_flags until this command has scanned them;
  - versions indexed before a rule change (5d-2b narrowed and widened rules) carry the codes of the old rules.

How it works: the tenant is found by slug through the platform session; everything else runs in tenant
sessions as app_tenant, so RLS applies. One short transaction per version: the version row is locked, its
chunks are read and scanned in a worker thread, and changed codes are written with one UPDATE ... FROM unnest
(the index stage's comma-joined form). A version that changed is audited as document:flags_rescanned (system
actor, counts only). Archived, failed and in-flight versions have no chunks to scan and are skipped.

It prints counts and ids only: never chunk text, a title or a file name (they are untrusted and may be private).
A published version that gains flags is reported: it was published without acknowledging them, so a teacher
should review it with GET .../versions/{version_id}/flags.

Run it against the database you mean: it uses DATABASE_URL and PLATFORM_DATABASE_URL like the API.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text

from app.core.tenant_db import tenant_session_context
from app.db.models.document import VERSION_STATUS_PUBLISHED, VERSION_STATUS_READY
from app.db.models.tenant import Tenant
from app.services import injection_scan
from app.services.audit import record_audit_event

SCANNED_STATUSES = (VERSION_STATUS_READY, VERSION_STATUS_PUBLISHED)

_UPDATE_CHUNKS_SQL = """
UPDATE app.chunks c SET injection_flags = string_to_array(u.flags, ',')
FROM unnest(CAST(:ids AS uuid[]), CAST(:flags AS text[])) AS u(id, flags)
WHERE c.tenant_id = :t AND c.document_version_id = :v AND c.id = u.id
"""


@dataclass(frozen=True)
class RescanSummary:
    versions_scanned: int = 0
    versions_changed: int = 0
    chunks_changed: int = 0
    applied: bool = False


class UnknownTenant(LookupError):
    pass


async def _tenant_id(slug: str) -> uuid.UUID:
    from app.db.engine import PlatformSessionLocal

    async with PlatformSessionLocal() as session, session.begin():
        found = (await session.execute(select(Tenant.id).where(Tenant.slug == slug))).scalar_one_or_none()
    if found is None:
        raise UnknownTenant(slug)
    return found


async def _version_ids(tenant_id: uuid.UUID) -> list[uuid.UUID]:
    async with tenant_session_context(str(tenant_id)) as db:
        rows = await db.execute(
            text(
                "SELECT id FROM app.document_versions WHERE tenant_id = :t AND status = ANY(CAST(:s AS text[])) "
                "ORDER BY created_at, id"
            ),
            {"t": tenant_id, "s": list(SCANNED_STATUSES)},
        )
        return [r[0] for r in rows.all()]


def _scan(rows: list[Any]) -> list[tuple[str, ...]]:
    return [injection_scan.scan_chunk(r.heading_path, r.content) for r in rows]


async def _rescan_version(
    tenant_id: uuid.UUID, version_id: uuid.UUID, *, apply: bool, out: Any
) -> tuple[bool, int]:
    """(changed, chunks changed) for one version, in one transaction."""
    async with tenant_session_context(str(tenant_id)) as db:
        version = (
            await db.execute(
                text(
                    "SELECT document_id, version_no, status, flagged_chunk_count FROM app.document_versions "
                    "WHERE tenant_id = :t AND id = :v" + (" FOR UPDATE" if apply else "")
                ),
                {"t": tenant_id, "v": version_id},
            )
        ).first()
        if version is None or version.status not in SCANNED_STATUSES:
            return False, 0  # deleted, archived or replaced since the list was read
        rows = (
            await db.execute(
                text(
                    "SELECT id, heading_path, content, injection_flags FROM app.chunks "
                    "WHERE tenant_id = :t AND document_version_id = :v ORDER BY ordinal"
                ),
                {"t": tenant_id, "v": version_id},
            )
        ).all()
        flags = await asyncio.to_thread(_scan, list(rows))
        changed = [(r.id, codes) for r, codes in zip(rows, flags, strict=True) if list(codes) != list(r.injection_flags or [])]
        new_count = sum(1 for codes in flags if codes)
        old_count = version.flagged_chunk_count
        if not changed and old_count == new_count:
            return False, 0

        out(
            f"version {version_id} (document {version.document_id}, v{version.version_no}, {version.status}): "
            f"flagged_chunk_count {old_count if old_count is not None else 'NULL'} -> {new_count}, "
            f"{len(changed)} chunk(s) with new codes"
        )
        if version.status == VERSION_STATUS_PUBLISHED and new_count > (old_count or 0):
            out(f"  review: version {version_id} is published and now has flagged chunks that were never acknowledged")
        if apply:
            if changed:
                await db.execute(
                    text(_UPDATE_CHUNKS_SQL),
                    {
                        "t": tenant_id, "v": version_id,
                        "ids": [c[0] for c in changed], "flags": [",".join(c[1]) for c in changed],
                    },
                )
            await db.execute(
                text(
                    "UPDATE app.document_versions SET flagged_chunk_count = :n, updated_at = now() "
                    "WHERE tenant_id = :t AND id = :v"
                ),
                {"n": new_count, "t": tenant_id, "v": version_id},
            )
            await record_audit_event(
                tenant_id=tenant_id, action="document:flags_rescanned", target_type="document",
                target_id=str(version.document_id),
                details={
                    "version_id": str(version_id), "version_no": version.version_no,
                    "flagged_chunk_count_before": old_count, "flagged_chunk_count": new_count,
                    "chunks_changed": len(changed),
                },
                actor_id=None, db=db,
            )
        return True, len(changed)


async def rescan(slug: str, *, apply: bool, out: Any = print) -> RescanSummary:
    """Rescan one tenant (see the module docstring). Raises UnknownTenant for a slug that does not exist."""
    tenant_id = await _tenant_id(slug)
    out(f"{'APPLY' if apply else 'DRY RUN'}: rescanning tenant {slug} ({tenant_id})")
    scanned = changed_versions = changed_chunks = 0
    for version_id in await _version_ids(tenant_id):
        changed, chunks = await _rescan_version(tenant_id, version_id, apply=apply, out=out)
        scanned += 1
        changed_versions += int(changed)
        changed_chunks += chunks
    out(
        f"{scanned} version(s) scanned, {changed_versions} {'changed' if apply else 'would change'}, "
        f"{changed_chunks} chunk(s) with new codes" + ("" if apply else " (dry run: nothing written; add --apply)")
    )
    return RescanSummary(scanned, changed_versions, changed_chunks, apply)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.rescan_flags",
        description="Rescan the stored chunks of one tenant with the current injection rules (dry run by default).",
    )
    parser.add_argument("tenant", help="the tenant slug")
    parser.add_argument("--apply", action="store_true", help="write the new codes and counts (default: dry run)")
    return parser


async def run(argv: list[str] | None = None) -> int:
    """The command, for an event loop that already runs (main() runs it with asyncio.run). Exit code."""
    args = _parser().parse_args(argv)
    try:
        await rescan(args.tenant, apply=args.apply)
    except UnknownTenant:
        print(f"unknown tenant: {args.tenant}", file=sys.stderr)
        return 2
    return 0


async def _main(argv: list[str] | None) -> int:
    from app.db.engine import engine, platform_engine

    try:
        return await run(argv)
    finally:
        await engine.dispose()
        await platform_engine.dispose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
