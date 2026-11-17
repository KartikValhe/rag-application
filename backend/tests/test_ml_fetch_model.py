"""
Step 5c-1: the operator download script (`python -m app.scripts.fetch_model`). Its logic (URL, redirect and host
allow-list, size limit, sha256 and size verification, atomic move, mismatch handling, --print-hashes) is tested
with httpx.MockTransport: no real network, no local server, and `block_sockets` proves it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.scripts import fetch_model as fm

pytestmark = pytest.mark.usefixtures("block_sockets")

REPO = "test/synthetic"
REV = "0123456789abcdef0123456789abcdef01234567"
MODEL_BYTES = b"model-bytes-" * 100
TOK_BYTES = b'{"tokenizer": true}'
URL_MODEL = f"https://huggingface.co/{REPO}/resolve/{REV}/onnx/model.onnx"
URL_TOK = f"https://huggingface.co/{REPO}/resolve/{REV}/onnx/tokenizer.json"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _entry(files: dict[str, bytes], **over: Any) -> dict[str, Any]:
    e = {
        "repo": REPO, "revision": REV, "embedder": "OnnxE5Embedder",
        "model_file": "onnx/model.onnx", "tokenizer_file": "onnx/tokenizer.json",
        "files": [{"path": p, "sha256": _sha(b), "size": len(b)} for p, b in files.items()],
        "dim": 8, "max_tokens": 512, "pooling": "mean", "pad_token": "<pad>",
        "prefixes": {"query": "query: ", "passage": "passage: "}, "license": "MIT", "status": "pinned",
    }
    e.update(over)
    return e


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    files = {"onnx/model.onnx": MODEL_BYTES, "onnx/tokenizer.json": TOK_BYTES}
    p = tmp_path / "lock.json"
    p.write_text(json.dumps({"version": 1, "models": {"tiny": _entry(files)}}), encoding="utf-8")
    return p


class Server:
    """A fake Hugging Face: routes by URL, records every request."""

    def __init__(self, routes: dict[str, Callable[[httpx.Request], httpx.Response]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(str(request.url))
        if handler is None:
            return httpx.Response(404, content=b"not found")
        return handler(request)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def ok(body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(200, content=body)


def good_server() -> Server:
    return Server({URL_MODEL: ok(MODEL_BYTES), URL_TOK: ok(TOK_BYTES)})


def listing(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def run(lock: Path, out: Path, server: Server, *extra: str) -> int:
    return fm.main(["tiny", "--out", str(out), "--lock", str(lock), *extra], client=server.client())


# ── Download and verification ─────────────────────────────────────────────────


def test_a_good_download_lands_in_the_model_directory_with_the_exact_url(lock_path: Path, tmp_path: Path) -> None:
    out, server = tmp_path / "models", good_server()
    assert run(lock_path, out, server) == 0
    assert (out / "tiny" / "onnx" / "model.onnx").read_bytes() == MODEL_BYTES
    assert (out / "tiny" / "onnx" / "tokenizer.json").read_bytes() == TOK_BYTES
    assert sorted(str(r.url) for r in server.requests) == sorted([URL_MODEL, URL_TOK])
    assert listing(out) == {"tiny/onnx/model.onnx", "tiny/onnx/tokenizer.json"}  # no temp files left


def test_no_credentials_or_identifying_headers_are_sent(lock_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_secret_should_never_be_sent")
    server = good_server()
    run(lock_path, tmp_path / "m", server)
    for r in server.requests:
        assert "authorization" not in r.headers and "cookie" not in r.headers
        assert "hf_secret" not in str(r.headers)
        assert r.method == "GET"
        assert "@" not in r.headers.get("user-agent", "")


def test_an_already_verified_file_is_not_downloaded_again(lock_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "m"
    assert run(lock_path, out, good_server()) == 0
    again = good_server()
    assert run(lock_path, out, again) == 0
    assert again.requests == []


def test_redirects_to_the_cdn_are_followed_and_the_file_is_verified(lock_path: Path, tmp_path: Path) -> None:
    cdn = "https://cdn-lfs.hf.co/repos/ab/cd/blob?sig=1"
    server = Server({
        URL_MODEL: lambda _r: httpx.Response(302, headers={"location": cdn}),
        cdn: ok(MODEL_BYTES),
        URL_TOK: lambda _r: httpx.Response(302, headers={"location": "/test/synthetic/cdn/tok"}),
        "https://huggingface.co/test/synthetic/cdn/tok": ok(TOK_BYTES),
    })
    assert run(lock_path, tmp_path / "m", server) == 0
    # the redirect hops carry no credentials either
    assert all("authorization" not in r.headers for r in server.requests)


@pytest.mark.parametrize("target", [
    "https://evil.example.com/x",
    "http://huggingface.co/x",  # plain http
    "https://huggingface.co.evil.com/x",  # look-alike suffix
    "https://evilhuggingface.co/x",
    "https://nothf.co/x",
    "https://user:pw@huggingface.co/x",  # user info
    "https://huggingface.co:8443/x",  # unusual port
    "ftp://huggingface.co/x",
    "file:///etc/passwd",
    "https://127.0.0.1/x",
    "https://[::1]/x",
])
def test_a_redirect_to_a_disallowed_target_is_refused_and_nothing_is_written(
    lock_path: Path, tmp_path: Path, target: str
) -> None:
    out = tmp_path / "m"
    server = Server({URL_MODEL: lambda _r: httpx.Response(302, headers={"location": target}),
                     URL_TOK: ok(TOK_BYTES)})
    assert run(lock_path, out, server) != 0
    assert not any(str(r.url) == target for r in server.requests)  # never even requested
    assert "tiny/onnx/model.onnx" not in listing(out) if out.exists() else True


def test_too_many_redirects_are_refused(lock_path: Path, tmp_path: Path) -> None:
    loop = "https://huggingface.co/loop"
    server = Server({URL_MODEL: lambda _r: httpx.Response(302, headers={"location": loop}),
                     loop: lambda _r: httpx.Response(302, headers={"location": loop}),
                     URL_TOK: ok(TOK_BYTES)})
    assert run(lock_path, tmp_path / "m", server) != 0
    assert len(server.requests) < 15


@pytest.mark.parametrize("host,allowed", [
    ("huggingface.co", True), ("cdn-lfs.huggingface.co", True), ("cdn-lfs.hf.co", True),
    ("cas-bridge.xethub.hf.co", True), ("HuggingFace.co", True),
    ("huggingface.co.evil.com", False), ("evilhuggingface.co", False), ("hf.co.evil.org", False),
    ("evil.com", False), ("", False), ("localhost", False), ("127.0.0.1", False),
])
def test_host_allow_list(host: str, allowed: bool) -> None:
    assert fm.is_allowed_host(host) is allowed


def test_a_hash_mismatch_deletes_the_temp_file_and_exits_non_zero(lock_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "m"
    bad = bytearray(MODEL_BYTES)
    bad[5] ^= 0x01  # same size, different content
    server = Server({URL_MODEL: ok(bytes(bad)), URL_TOK: ok(TOK_BYTES)})
    assert run(lock_path, out, server) == 1
    assert "tiny/onnx/model.onnx" not in listing(out)
    assert not [p for p in listing(out) if p.endswith(".part") or ".tmp" in p or p.startswith(".")]


def test_a_mismatch_leaves_an_existing_good_file_untouched(lock_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "m"
    dest = out / "tiny" / "onnx"
    dest.mkdir(parents=True)
    (dest / "model.onnx").write_bytes(b"old, different content")  # fails verification, so it is re-downloaded
    bad = Server({URL_MODEL: ok(b"x" * len(MODEL_BYTES)), URL_TOK: ok(TOK_BYTES)})
    assert run(lock_path, out, bad) == 1
    assert (dest / "model.onnx").read_bytes() == b"old, different content"  # not replaced by bad bytes
    assert listing(out) == {"tiny/onnx/model.onnx"}


def test_a_short_download_is_a_size_mismatch(lock_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "m"
    server = Server({URL_MODEL: ok(MODEL_BYTES[:-1]), URL_TOK: ok(TOK_BYTES)})
    assert run(lock_path, out, server) == 1
    assert "tiny/onnx/model.onnx" not in listing(out)


def test_a_body_larger_than_the_locked_size_stops_early_and_is_discarded(lock_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "m"
    consumed = {"chunks": 0}

    def endless() -> Iterator[bytes]:
        for _ in range(10_000):
            consumed["chunks"] += 1
            yield b"A" * 1024

    server = Server({URL_MODEL: lambda _r: httpx.Response(200, content=endless()), URL_TOK: ok(TOK_BYTES)})
    assert run(lock_path, out, server) == 1
    assert consumed["chunks"] < 100  # stopped near the limit (1200 bytes), not after 10 MB
    assert "tiny/onnx/model.onnx" not in listing(out)


def test_a_declared_content_length_over_the_limit_is_refused_before_reading_the_body(
    lock_path: Path, tmp_path: Path
) -> None:
    consumed = {"chunks": 0}

    def body() -> Iterator[bytes]:
        consumed["chunks"] += 1
        yield MODEL_BYTES

    server = Server({
        URL_MODEL: lambda _r: httpx.Response(200, headers={"content-length": str(10 * len(MODEL_BYTES))},
                                             content=body()),
        URL_TOK: ok(TOK_BYTES),
    })
    assert run(lock_path, tmp_path / "m", server) == 1
    assert consumed["chunks"] == 0


@pytest.mark.parametrize("status", [403, 404, 500, 503])
def test_an_http_error_fails_without_writing(lock_path: Path, tmp_path: Path, status: int) -> None:
    out = tmp_path / "m"
    server = Server({URL_MODEL: lambda _r: httpx.Response(status, content=b"nope"), URL_TOK: ok(TOK_BYTES)})
    assert run(lock_path, out, server) == 1
    assert "tiny/onnx/model.onnx" not in (listing(out) if out.exists() else set())


def test_a_network_error_fails_cleanly(lock_path: Path, tmp_path: Path) -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert run(lock_path, tmp_path / "m", Server({URL_MODEL: boom, URL_TOK: ok(TOK_BYTES)})) == 1


def test_the_final_move_is_atomic_the_destination_never_holds_partial_bytes(
    lock_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "m"
    dest = out / "tiny" / "onnx" / "model.onnx"
    seen: list[bool] = []
    real_replace = fm.os.replace

    def spy(src: Any, dst: Any) -> None:
        # at the moment of the move the destination does not exist yet and the source is complete
        seen.append(Path(dst) == dest and not dest.exists() and Path(src).read_bytes() == MODEL_BYTES)
        real_replace(src, dst)

    monkeypatch.setattr(fm.os, "replace", spy)
    assert run(lock_path, out, good_server()) == 0
    assert True in seen


def test_an_unverified_entry_is_never_downloaded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {"onnx/model.onnx": MODEL_BYTES, "onnx/tokenizer.json": TOK_BYTES}
    p = tmp_path / "lock.json"
    p.write_text(json.dumps({"version": 1, "models": {"tiny": _entry(files, status="UNVERIFIED")}}), encoding="utf-8")
    server = good_server()
    assert run(p, tmp_path / "m", server) == 2
    assert server.requests == []
    assert "--print-hashes" in capsys.readouterr().err


def test_an_unknown_model_name_exits_with_a_usage_error(lock_path: Path, tmp_path: Path) -> None:
    assert fm.main(["nope", "--lock", str(lock_path), "--out", str(tmp_path)], client=good_server().client()) == 2


# ── --print-hashes (trust on first use: computes, prints, writes nothing) ─────


def test_print_hashes_prints_size_and_sha256_and_writes_nothing(
    lock_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "m"
    d = out / "tiny" / "onnx"
    d.mkdir(parents=True)
    (d / "model.onnx").write_bytes(MODEL_BYTES)
    (d / "tokenizer.json").write_bytes(TOK_BYTES + b" ")  # differs from the lock
    before, lock_before = listing(out), lock_path.read_bytes()
    server = good_server()
    assert run(lock_path, out, server, "--print-hashes") == 0
    text = capsys.readouterr().out
    assert _sha(MODEL_BYTES) in text and str(len(MODEL_BYTES)) in text
    assert _sha(TOK_BYTES + b" ") in text
    assert "matches the lock" in text and "DIFFERS from the lock" in text
    assert listing(out) == before and lock_path.read_bytes() == lock_before
    assert server.requests == []  # no network in this mode


def test_print_hashes_reports_a_missing_file_and_exits_non_zero(
    lock_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(lock_path, tmp_path / "m", good_server(), "--print-hashes") == 1
    assert "MISSING" in capsys.readouterr().out


def test_print_hashes_works_for_an_unverified_entry_with_explicit_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = tmp_path / "lock.json"
    p.write_text(json.dumps({"version": 1, "models": {"tiny": _entry({}, status="UNVERIFIED", revision=None,
                                                                      model_file=None, tokenizer_file=None)}}),
                 encoding="utf-8")
    d = tmp_path / "m" / "tiny" / "onnx"
    d.mkdir(parents=True)
    (d / "model.onnx").write_bytes(MODEL_BYTES)
    assert fm.main(["tiny", "--out", str(tmp_path / "m"), "--lock", str(p), "--print-hashes",
                    "--file", "onnx/model.onnx"], client=good_server().client()) == 0
    assert _sha(MODEL_BYTES) in capsys.readouterr().out


def test_print_hashes_refuses_unsafe_file_arguments(tmp_path: Path) -> None:
    p = tmp_path / "lock.json"
    p.write_text(json.dumps({"version": 1, "models": {"tiny": _entry({}, status="UNVERIFIED", revision=None,
                                                                      model_file=None, tokenizer_file=None)}}),
                 encoding="utf-8")
    for bad in ("../x", "/etc/passwd", "a\\b"):
        assert fm.main(["tiny", "--out", str(tmp_path), "--lock", str(p), "--print-hashes", "--file", bad],
                       client=good_server().client()) == 2
