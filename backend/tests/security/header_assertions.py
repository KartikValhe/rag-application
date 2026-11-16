"""
tests/security/header_assertions.py
───────────────────────────────────
Shared assertion for the security-headers layer (finding 10 of the review of 5b-1/5b-2). Every
response of the real app, whatever its status and whoever produced it (a route, the body-limit
middleware or the unhandled-error path), must carry the defensive headers. The helper is used by the
real-app tests and by the prove-it-fails meta-test, which switches the layer off and shows that the
helper then raises.
"""

from collections.abc import Mapping

DEFAULT_CSP = "default-src 'none'; frame-ancestors 'none'"


def _csp_directives(value: str) -> list[str]:
    return [part.strip().split()[0].lower() for part in value.split(";") if part.strip()]


def assert_security_headers(
    headers: Mapping[str, str], *, label: str, sandboxed: bool = False, hsts: bool = False
) -> None:
    """
    nosniff, frame denial, referrer policy and a CSP on every response. The CSP is the strict default
    unless the response is a sandboxed download (its own CSP has the `sandbox` directive).
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered.get("x-content-type-options") == "nosniff", f"{label}: no X-Content-Type-Options: nosniff"
    assert lowered.get("x-frame-options") == "DENY", f"{label}: no X-Frame-Options: DENY"
    assert lowered.get("referrer-policy") == "strict-origin-when-cross-origin", f"{label}: no Referrer-Policy"
    csp = lowered.get("content-security-policy")
    assert csp is not None, f"{label}: no Content-Security-Policy"
    if sandboxed:
        assert "sandbox" in _csp_directives(csp), f"{label}: a download lost its sandbox CSP: {csp!r}"
    else:
        assert csp == DEFAULT_CSP, f"{label}: weak or unexpected Content-Security-Policy: {csp!r}"
    assert ("strict-transport-security" in lowered) == hsts, f"{label}: HSTS {'missing' if hsts else 'present'}"
