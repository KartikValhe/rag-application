"""
python -m app.scripts.fetch_model <name> [--out DIR] [--lock FILE]
python -m app.scripts.fetch_model <name> --print-hashes [--file PATH ...]

The ONE operator command that downloads an embedding model (design A1). The API, the worker and the tests never
do: at runtime the loader only reads the verified local files. Run it on a build or admin machine, review the
output, and bake the files into the image (README, "Embedding model").

Download mode (the default), for a model whose lock entry is pinned:
  * for each file of the lock entry: GET https://huggingface.co/{repo}/resolve/{revision}/{path} with httpx;
  * HTTPS only. Redirects are followed by hand (at most 5) and every hop must stay on huggingface.co or its CDN
    hosts (see ALLOWED_HOST_SUFFIXES): another host, plain http, user info or an unusual port is refused before
    any request is made. No credentials, cookies or tokens are ever sent (no HF_TOKEN, no netrc, no proxy
    settings: `trust_env` is off);
  * the body streams into a temporary file next to the destination and is cut off at the locked size (a larger
    Content-Length is refused before the body is read);
  * size and sha256 are verified against the lock; only then is the file moved into place atomically
    (`os.replace`). On any mismatch the temp file is deleted, an existing file is left untouched and the
    command exits non-zero. A file that already verifies is not downloaded again.

--print-hashes ("trust on first use"): computes and prints the size and sha256 of the files that are ALREADY in
`<dir>/<name>/` (the lock's files, or the ones named with --file), says whether each matches the lock, and
writes nothing: no download, no change to the lock file. This is how an UNVERIFIED entry is reviewed before
a person pins it by hand.

Exit codes: 0 success, 1 download or verification failure (or a missing file with --print-hashes), 2 usage,
lock or unverified-entry error.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO

import httpx

from app.ml.lock import (
    DEFAULT_LOCK_PATH,
    STATUS_PINNED,
    LockedFile,
    ModelLoadError,
    ModelSpec,
    hash_file,
    is_safe_relative_path,
    load_lock,
)

# Hugging Face's own host and its CDN domains. UNVERIFIED which exact CDN host names Hugging Face serves large
# files from today: a redirect to a host outside this list is refused and printed, so the operator can review
# the host and extend this tuple on purpose instead of the script trusting an arbitrary redirect.
ALLOWED_HOST_SUFFIXES = (".huggingface.co", ".hf.co")
ALLOWED_HOSTS_EXACT = ("huggingface.co", "hf.co")
MAX_REDIRECTS = 5
FILE_DEADLINE_SECONDS = 3600
USER_AGENT = "rag-platform-fetch-model/1"
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class FetchError(Exception):
    """A download or verification problem; the command exits with code 1."""


def is_allowed_host(host: str) -> bool:
    h = host.lower().rstrip(".")
    return bool(h) and (h in ALLOWED_HOSTS_EXACT or h.endswith(ALLOWED_HOST_SUFFIXES))


def check_url(url: str) -> httpx.URL:
    """Refuse anything but plain https to an allowed host (no user info, default port only)."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise FetchError(f"refusing an invalid URL: {exc}") from exc
    if parsed.scheme != "https":
        raise FetchError(f"refusing a non-https URL: {parsed.scheme}://{parsed.host}")
    if parsed.userinfo:
        raise FetchError(f"refusing a URL with user info (host {parsed.host})")
    if parsed.port not in (None, 443):
        raise FetchError(f"refusing a non-standard port: {parsed.host}:{parsed.port}")
    if not is_allowed_host(parsed.host):
        raise FetchError(
            f"refusing host {parsed.host!r}: only huggingface.co and its CDN hosts are allowed "
            f"(ALLOWED_HOST_SUFFIXES in app/scripts/fetch_model.py)"
        )
    return parsed


def file_url(spec: ModelSpec, path: str) -> str:
    return f"https://huggingface.co/{spec.repo}/resolve/{spec.revision}/{path}"


def _verified(path: Path, locked: LockedFile) -> bool:
    try:
        if path.stat().st_size != locked.size:
            return False
        return hash_file(path)[1] == locked.sha256
    except OSError:
        return False


def download_file(client: httpx.Client, url: str, dest: Path, locked: LockedFile) -> None:
    """Download `url` to `dest`, verified against `locked`. `dest` appears only when it verifies."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".fetch-", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            digest = hashlib.sha256()
            total = _stream_to(client, url, locked, out, digest)
            out.flush()
            os.fsync(out.fileno())
        if total != locked.size:
            raise FetchError(f"{locked.path}: downloaded {total} bytes, the lock says {locked.size}")
        if digest.hexdigest() != locked.sha256:
            raise FetchError(
                f"{locked.path}: sha256 {digest.hexdigest()} differs from the locked sha256 {locked.sha256}"
            )
        os.chmod(tmp, 0o644)  # mkstemp makes it 0600; the service user may differ from the operator
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _stream_to(client: httpx.Client, url: str, locked: LockedFile, out: BinaryIO, digest: Any) -> int:
    deadline = time.monotonic() + FILE_DEADLINE_SECONDS
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        check_url(current)
        try:
            with client.stream(
                "GET", current, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
                follow_redirects=False,
            ) as response:
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError(f"{locked.path}: redirect without a Location header")
                    current = str(httpx.URL(current).join(location))
                    continue
                if response.status_code != 200:
                    raise FetchError(f"{locked.path}: HTTP {response.status_code} from {response.url.host}")
                declared = response.headers.get("content-length")
                if declared is not None and declared.isdigit() and int(declared) > locked.size:
                    raise FetchError(f"{locked.path}: the server announces {declared} bytes, the lock says {locked.size}")
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > locked.size:
                        raise FetchError(f"{locked.path}: more than the locked {locked.size} bytes arrived")
                    if time.monotonic() > deadline:
                        raise FetchError(f"{locked.path}: download exceeded {FILE_DEADLINE_SECONDS} seconds")
                    digest.update(chunk)
                    out.write(chunk)
                return total
        except httpx.HTTPError as exc:
            raise FetchError(f"{locked.path}: network error: {type(exc).__name__}: {exc}") from exc
    raise FetchError(f"{locked.path}: more than {MAX_REDIRECTS} redirects")


def fetch_model(spec: ModelSpec, out_dir: Path, client: httpx.Client) -> list[Path]:
    """Download every locked file of a pinned model into `<out_dir>/<name>/`. Stops at the first failure."""
    if spec.status != STATUS_PINNED:
        raise ModelLoadError(
            f"model {spec.name!r} is marked {spec.status}: nothing to download. Review the files you "
            "have with --print-hashes, then pin the values in app/ml/models.lock.json by hand"
        )
    written: list[Path] = []
    for locked in spec.files:
        dest = out_dir / spec.directory_name / locked.path
        if _verified(dest, locked):
            print(f"ok       {locked.path} (already present and verified)")
            continue
        print(f"fetching {locked.path} ({locked.size} bytes)")
        download_file(client, file_url(spec, locked.path), dest, locked)
        print(f"ok       {locked.path} sha256 {locked.sha256}")
        written.append(dest)
    return written


def print_hashes(spec: ModelSpec, out_dir: Path, files: list[str]) -> int:
    """Print size and sha256 of the local files; never writes and never uses the network."""
    locked_by_path = {f.path: f for f in spec.files}
    paths = files or [f.path for f in spec.files]
    if not paths:
        print("nothing to hash: the lock entry lists no files; name them with --file", file=sys.stderr)
        return 2
    missing = 0
    for path in paths:
        target = out_dir / spec.directory_name / path
        if not target.is_file():
            print(f"MISSING  {path} (expected at {target})")
            missing += 1
            continue
        size, digest = hash_file(target)
        locked = locked_by_path.get(path)
        if locked is None:
            verdict = "not in the lock"
        elif (locked.size, locked.sha256) == (size, digest):
            verdict = "matches the lock"
        else:
            verdict = "DIFFERS from the lock"
        print(f"{path}\n  size   {size}\n  sha256 {digest}\n  {verdict}")
    print("Nothing was written. Review the values, then pin them in app/ml/models.lock.json by hand.")
    return 1 if missing else 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.scripts.fetch_model",
        description="Download and verify an embedding model listed in app/ml/models.lock.json.",
    )
    p.add_argument("name", help="model name in the lock file, for example e5-small")
    p.add_argument("--out", help="model root directory (default: EMBEDDING_MODEL_DIR); files go to <out>/<name>/")
    p.add_argument("--lock", help="lock file (default: the packaged app/ml/models.lock.json)")
    p.add_argument("--print-hashes", action="store_true",
                   help="only compute and print size and sha256 of the existing local files; write nothing")
    p.add_argument("--file", action="append", default=[], metavar="PATH",
                   help="with --print-hashes: a file under <out>/<name>/ to hash (repeatable)")
    return p


def main(argv: list[str] | None = None, *, client: httpx.Client | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        spec = load_lock(Path(args.lock) if args.lock else DEFAULT_LOCK_PATH).get(args.name)
    except ModelLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out = args.out
    if not out:
        from app.core.config import settings

        out = settings.EMBEDDING_MODEL_DIR
    if not out:
        print("error: give --out DIR or set EMBEDDING_MODEL_DIR", file=sys.stderr)
        return 2
    out_dir = Path(out)

    if args.print_hashes:
        if bad := [f for f in args.file if not is_safe_relative_path(f)]:
            print(f"error: unsafe --file path {bad[0]!r}: use a plain relative path", file=sys.stderr)
            return 2
        return print_hashes(spec, out_dir, args.file)

    if args.file:
        print("error: --file only works together with --print-hashes", file=sys.stderr)
        return 2
    try:
        spec.model_key  # noqa: B018  (raises when the entry has no pinned revision)
        owned = client is None
        http = client or httpx.Client(
            timeout=httpx.Timeout(30.0, read=60.0), follow_redirects=False, trust_env=False
        )
        try:
            fetch_model(spec, out_dir, http)
        finally:
            if owned:
                http.close()
    except ModelLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FetchError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"model {spec.name!r} is verified in {out_dir / spec.directory_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
