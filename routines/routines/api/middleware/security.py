"""Browser-CSRF + DNS-rebinding guard for the loopback bridge (F-1, F-3).

The bridge binds to ``127.0.0.1:8765`` and trusts the loopback boundary for
authentication (there is no token/cookie auth — any process on the box may
call it). That trust model has two browser-shaped holes this middleware
closes:

F-1 — **browser-origin CSRF.** FastAPI/Starlette JSON-parse a POST body even
when ``Content-Type`` is *absent*, so a cross-origin page can fire a
``fetch(url, {method:'POST', body: new Blob([json])})`` with NO custom headers
— which means NO CORS preflight — and the request reaches every
state-changing route. CORS only protects the *response* from being read; it
does nothing to stop the *request* from executing its side effect. We close
this by requiring, for every state-changing method:

  (a) an ``Origin`` header in a strict loopback allowlist; OR, when ``Origin``
      is absent (non-browser clients, health probes, curl), a
      ``Sec-Fetch-Site`` of ``same-origin`` / ``none``. We **fail closed**:
      browsers always send ``Sec-Fetch-*`` on modern engines, so a cross-site
      browser request that omits ``Origin`` still carries
      ``Sec-Fetch-Site: cross-site`` and is rejected — but a request with
      NEITHER ``Origin`` NOR ``Sec-Fetch-Site`` is ALSO rejected (an absent
      fetch-metadata signal is no longer treated as proof of a same-origin
      caller). A genuine non-browser loopback client that needs to mutate
      state must self-identify with ``Sec-Fetch-Site: none`` (or a loopback
      ``Origin``); silent neither-header callers no longer slip through
      (codex round, FINDING 1).
  (b) a ``Content-Type`` beginning ``application/json``, enforced
      UNCONDITIONALLY on every state-changing method (no body-present
      relaxation). This single-handedly kills the absent-/``text/plain``
      Content-Type CSRF vector AND closes the chunked/HTTP2/dechunked
      body-detection gap (a real body can present with no positive
      ``Content-Length`` → header-only body detection would have skipped the
      gate). A simple cross-origin ``fetch`` cannot set
      ``Content-Type: application/json`` without tripping a CORS preflight
      (it's not a CORS-safelisted value), and our CORS policy never answers a
      preflight for a foreign origin. This is safe for legitimate callers:
      the dashboard's ``lib/api.ts`` ``request()`` helper sends
      ``Content-Type: application/json`` on EVERY call (including bodyless
      mutations like ``archiveSession``/``deleteCredential``), so no real
      endpoint breaks (codex round, FINDING 2).

F-3 — **DNS rebinding.** Without Host validation, an attacker page on
``evil.test`` whose DNS is rebound to ``127.0.0.1`` becomes "same-origin"
from the browser's view and can read the entire GET/SSE surface. We reject
any request whose ``Host`` authority is not a loopback authority BEFORE
routing.

Design choices
--------------
* **Raw ASGI, not ``BaseHTTPMiddleware``.** We read the ``host`` header and
  the method straight off the ASGI ``scope`` and reject by sending the
  response ourselves — the rejected request never constructs a ``Request``,
  never touches ``request.url``, and never reaches a route handler. This is
  *CVE-immune by construction* w.r.t. CVE-2026-48710 (the Starlette "BadHost"
  class where a crafted ``Host`` poisons ``request.url.hostname``): we never
  consult ``request.url`` for the security decision. (Starlette is a transitive
  dependency via FastAPI — not directly pinned here — and the resolved version
  in this environment carries the BadHost fix; but self-implementing keeps the
  guard correct regardless of which Starlette version resolves, and keeps the
  F-1 + F-3 allowlists in one place.)

* **Order in the stack.** Registered LAST in ``create_app`` so it wraps
  OUTERMOST (Starlette middleware is LIFO) — the guard runs BEFORE CORS,
  RunId, routing, and any handler. A rejected request does zero work.

* **Status codes.** Foreign/missing-and-unattested Origin → ``403``
  (forbidden — the caller is not an allowed origin, or did not attest a
  same-origin/non-browser fetch context). Non-JSON / absent Content-Type →
  ``415`` (Unsupported Media Type — semantically precise: the request's media
  type is not one we accept for a mutation). Bad Host → ``400`` (the request
  is malformed for this server — it was not addressed to a known authority).

* **No production env escape-hatch.** The Host/Origin allowlists are fixed
  loopback sets. They can be overridden ONLY via constructor arguments
  (``allowed_hosts`` / ``allowed_origins``) — there is deliberately NO
  environment variable that can extend them at runtime, so a misconfigured
  production deployment cannot be coaxed into trusting ``evil.test`` (codex
  round, FINDING 3). The test suite passes the gate by pinning its
  ``TestClient`` ``base_url``/headers to loopback values, not by widening the
  production default.

* **Read methods are exempt.** ``GET/HEAD/OPTIONS`` skip the Origin +
  Content-Type checks (reads have no side effect; ``OPTIONS`` is the CORS
  preflight). The Host check applies to ALL methods (DNS-rebind targets the
  read surface too).

Allowlists are module constants so adding a port/origin is a one-line change.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

# ── Configurable allowlists ──────────────────────────────────────────────────
# The dashboard's same-origin (production, :8765) + dev (Vite, :5173) origins.
# A future port/origin is a one-line addition here.
ALLOWED_ORIGINS: frozenset[str] = frozenset(
    {
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    }
)

# Host header authorities we answer to. Includes the bare hosts (a client may
# send ``Host: 127.0.0.1`` with no port when the default is implied by the
# scheme, and some probes do) plus the explicit loopback authorities. Any
# other Host is a rebinding attempt → 400.
ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "127.0.0.1:8765",
        "localhost:8765",
        "127.0.0.1:5173",
        "localhost:5173",
        "127.0.0.1",
        "localhost",
    }
)

# When Origin is absent, accept only these Sec-Fetch-Site values (same-origin
# browser requests + non-browser clients that send the header). A cross-site
# browser request that strips Origin still sends ``Sec-Fetch-Site: cross-site``
# and is rejected.
SAFE_FETCH_SITES: frozenset[str] = frozenset({"same-origin", "none"})

# Methods that mutate state — these get the full Origin + Content-Type gate.
STATE_CHANGING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Required media-type prefix for any state-changing request. Enforced
# UNCONDITIONALLY (codex round, FINDING 2) — we deliberately do NOT try to
# detect whether a body is present (Content-Length / Transfer-Encoding are not
# airtight: a chunked / HTTP2 / dechunked body can carry real bytes with no
# positive Content-Length, which a header-only body check would miss and so
# skip this gate, reopening the no-Content-Type JSON-parse CSRF vector).
REQUIRED_CONTENT_TYPE_PREFIX = "application/json"

# ── Multipart upload exception (#chat-attachments) ────────────────────────────
# A file upload MUST use multipart/form-data — it cannot send application/json.
# We permit multipart ONLY on the explicit upload route below, and ONLY for
# multipart/form-data. The anti-CSRF protection for these paths is carried
# ENTIRELY by the F-1(a) Origin / Sec-Fetch-Site gate above, which still runs
# UNCHANGED: a cross-site browser request carries ``Sec-Fetch-Site: cross-site``
# (or a foreign Origin) and is rejected before this content-type check, exactly
# as for a JSON request. The F-1(b) require-JSON rule is a *defence-in-depth*
# layer (a simple cross-origin fetch can't set application/json without a
# preflight); multipart/form-data IS a CORS-safelisted content type, so it's a
# weaker layer — but the load-bearing F-1(a) gate is undiminished, and these
# routes are additionally loopback-only at the router level.
#
# The allowance is scoped by an EXACT route-shape match, NOT a suffix/substring:
# only ``/api/sessions/<id>/attachments`` (no extra path segments, no trailing
# slash) is granted the exception. A regex on the RAW ASGI ``scope['path']``
# (never ``request.url``) means a dot-segment path like
# ``/api/sessions/x/../attachments``, a trailing-slash variant, a substring
# match, or any future unrelated ``…/attachments`` route is NOT granted the
# relaxation and still requires application/json — without relying on ASGI path
# normalisation to have collapsed the dot-segments first.
MULTIPART_CONTENT_TYPE_PREFIX = "multipart/form-data"
# ``[^/]+`` for the session id rejects embedded slashes (and therefore any
# ``..`` segment, which needs slashes around it); anchored ``^…$`` rejects
# prefixes/suffixes and trailing slashes.
MULTIPART_UPLOAD_PATH_RE: re.Pattern[str] = re.compile(
    r"^/api/sessions/[^/]+/attachments$"
)


def _header(scope: Scope, name: bytes) -> str | None:
    """Read a single request header off the raw ASGI scope (case-insensitive).

    We go to the scope rather than building a Starlette ``Request`` so the
    security decision never depends on ``request.url`` (the BadHost class).
    Header names on the wire are lowercase per ASGI spec.
    """
    for k, v in scope.get("headers", ()):
        if k == name:
            try:
                return v.decode("latin-1")
            except Exception:  # pragma: no cover — headers are latin-1 by spec
                return None
    return None


class SecurityHeadersMiddleware:
    """ASGI middleware enforcing Host + Origin + require-JSON (F-1, F-3).

    Pure-ASGI (no ``BaseHTTPMiddleware``) so rejection happens before a
    ``Request``/route is constructed. Non-HTTP scopes (lifespan, websocket)
    pass straight through.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_origins: Iterable[str] = ALLOWED_ORIGINS,
        allowed_hosts: Iterable[str] = ALLOWED_HOSTS,
        safe_fetch_sites: Iterable[str] = SAFE_FETCH_SITES,
    ) -> None:
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)
        # Host comparison is case-insensitive (we lower() the wire value); keep
        # the allowlist lowercased. The ONLY way to extend it is via this
        # constructor argument — there is deliberately no env-var escape hatch
        # (codex round, FINDING 3), so production can't be coaxed into trusting
        # a non-loopback host at runtime.
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self.safe_fetch_sites = frozenset(safe_fetch_sites)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()

        # ── F-3: Host validation (ALL methods, incl. reads — rebind hits GET) ──
        host = _header(scope, b"host")
        # An HTTP/1.1 request always carries Host; absence is non-conforming.
        # We reject a missing or non-loopback Host as a malformed/rebound
        # request. Comparison is on the RAW header, never ``request.url``.
        if host is None or host.lower() not in self.allowed_hosts:
            await self._reject(
                send,
                status=400,
                error="bad_host",
                message="Invalid Host header — request not addressed to a known loopback authority.",
            )
            return

        # ── Reads are exempt from the Origin + Content-Type gate ──────────────
        # GET/HEAD have no side effect; OPTIONS is the CORS preflight (must
        # reach CORSMiddleware to be answered).
        if method not in STATE_CHANGING_METHODS:
            await self.app(scope, receive, send)
            return

        # ── F-1 (a): Origin allowlist (or absent-Origin same-site fallback) ───
        origin = _header(scope, b"origin")
        if origin is not None:
            if origin not in self.allowed_origins:
                await self._reject(
                    send,
                    status=403,
                    error="forbidden_origin",
                    message="Cross-origin state-changing request rejected.",
                )
                return
        else:
            # No Origin header — FAIL CLOSED (codex round, FINDING 1). We accept
            # ONLY when ``Sec-Fetch-Site`` is present AND in the safe set
            # ({same-origin, none}). Modern browsers ALWAYS send Sec-Fetch-Site,
            # so a cross-site browser request that omits Origin still
            # self-identifies as ``cross-site`` and is rejected. A request with
            # NEITHER Origin NOR Sec-Fetch-Site is no longer treated as a
            # trusted same-origin caller: an absent metadata signal is not proof
            # of intent, so we reject it. A genuine non-browser loopback client
            # that needs to mutate state must attest with ``Sec-Fetch-Site:
            # none`` (or send a loopback ``Origin``).
            sec_fetch_site = _header(scope, b"sec-fetch-site")
            if sec_fetch_site is None or sec_fetch_site not in self.safe_fetch_sites:
                await self._reject(
                    send,
                    status=403,
                    error="forbidden_origin",
                    message=(
                        "State-changing request without an allowed Origin or a "
                        "same-origin/non-browser Sec-Fetch-Site attestation rejected."
                    ),
                )
                return

        # ── F-1 (b): require application/json — UNCONDITIONALLY (codex FINDING 2)
        # Kills the absent-/text-plain Content-Type CSRF vector: a simple
        # cross-origin fetch cannot set application/json without a preflight,
        # and FastAPI would otherwise JSON-parse a body with no Content-Type.
        #
        # We enforce this on EVERY state-changing method regardless of whether a
        # body appears present. Header-only body detection (Content-Length /
        # Transfer-Encoding) is NOT airtight — a chunked / HTTP2 / dechunked
        # body can carry real bytes with no positive Content-Length, which a
        # "has body?" check would miss and so skip this gate, reopening the
        # no-Content-Type JSON-parse vector. Enforcing JSON unconditionally is
        # robust by construction. It's safe for legitimate callers: the
        # dashboard's ``lib/api.ts`` ``request()`` helper sends
        # ``Content-Type: application/json`` on EVERY call, including bodyless
        # mutations (archiveSession / deleteCredential / scheduler pause/resume).
        # A non-browser loopback client making a bodyless mutation must likewise
        # send the header (it's a one-line addition and is already required of
        # the in-process/external HTTP tool clients — e.g. the Synapse tool
        # definitions in ``routines/composite/install/install_synapse.py``).
        content_type = _header(scope, b"content-type") or ""
        ct_lower = content_type.lower()
        # #chat-attachments: the file-upload route may send multipart/form-data
        # instead of application/json. The F-1(a) Origin/Sec-Fetch-Site gate
        # above already ran (and would have rejected a cross-site request), so
        # this only relaxes the defence-in-depth content-type layer, and ONLY for
        # the EXACT upload route shape + the multipart media type. The path is
        # matched with an anchored regex on the raw ASGI scope (never
        # ``request.url``), so a dot-segment / trailing-slash / substring /
        # unrelated ``…/attachments`` path is NOT granted the exception, without
        # relying on ASGI path normalisation.
        raw_path = scope.get("path", "") or ""
        multipart_allowed = (
            MULTIPART_UPLOAD_PATH_RE.match(raw_path) is not None
            and ct_lower.startswith(MULTIPART_CONTENT_TYPE_PREFIX)
        )
        if not multipart_allowed and not ct_lower.startswith(REQUIRED_CONTENT_TYPE_PREFIX):
            await self._reject(
                send,
                status=415,
                error="unsupported_media_type",
                message="State-changing requests must send Content-Type: application/json.",
            )
            return

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Send, *, status: int, error: str, message: str) -> None:
        """Send a small JSON error response and short-circuit the request.

        The body mirrors the bridge's ``{error, human_message, detail}`` shape
        (see the SkillPreconditions handler in ``app.py``) so existing dashboard
        error parsers (which read ``detail``) surface it unchanged.
        """
        body = json.dumps(
            {"error": error, "human_message": message, "detail": message}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = [
    "ALLOWED_ORIGINS",
    "ALLOWED_HOSTS",
    "SAFE_FETCH_SITES",
    "STATE_CHANGING_METHODS",
    "REQUIRED_CONTENT_TYPE_PREFIX",
    "MULTIPART_CONTENT_TYPE_PREFIX",
    "MULTIPART_UPLOAD_PATH_RE",
    "SecurityHeadersMiddleware",
]
