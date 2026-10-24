"""Framework-independent helpers for VoxsBridge.

Kept free of FastAPI imports so they can be unit-tested on their own:
  * Twilio access tokens, webhook signatures and TwiML building
  * Estimated call-cost arithmetic
  * Supabase Storage client (private bucket) for recordings
  * Pure-ASGI security middleware (security headers + CSRF protection)
"""
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from html import escape as _html_escape
from http.cookies import SimpleCookie


# --------------------------------------------------------------------------
# Twilio
# --------------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def twilio_access_token(account_sid, api_key_sid, api_key_secret, identity,
                        twiml_app_sid, ttl=3600, now=None):
    """Voice SDK access token (HS256 JWT signed with an API key secret)."""
    iat = int(now if now is not None else time.time())
    header = {"typ": "JWT", "alg": "HS256", "cty": "twilio-fpa;v=1"}
    payload = {
        "jti": f"{api_key_sid}-{iat}",
        "iss": api_key_sid,
        "sub": account_sid,
        "iat": iat,
        "nbf": iat,
        "exp": iat + int(ttl),
        "grants": {
            "identity": identity,
            "voice": {
                "incoming": {"allow": True},
                "outgoing": {"application_sid": twiml_app_sid},
            },
        },
    }
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "." +
                     _b64url(json.dumps(payload, separators=(",", ":")).encode()))
    signature = hmac.new(api_key_secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64url(signature)


def twilio_expected_signature(auth_token, url, params):
    """X-Twilio-Signature: base64(HMAC-SHA1(url + sorted key/value pairs))."""
    data = url
    for key in sorted(params.keys()):
        values = params[key]
        if not isinstance(values, (list, tuple)):
            values = [values]
        for value in values:
            data += key + str(value)
    digest = hmac.new(auth_token.encode(), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def twilio_signature_valid(auth_token, urls, params, signature):
    if not auth_token or not signature:
        return False
    for url in urls:
        if url and hmac.compare_digest(twilio_expected_signature(auth_token, url, params), signature):
            return True
    return False


def xml_attr(value) -> str:
    return _html_escape(str(value), quote=True)


def twiml(*verbs: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(verbs) + "</Response>"


def identity_for_user(user_id) -> str:
    """Twilio client identity: letters, digits and underscore only."""
    return "dn_" + str(user_id).replace("-", "")


def user_id_from_identity(identity: str):
    raw = str(identity or "")
    if raw.startswith("client:"):
        raw = raw[len("client:"):]
    if not raw.startswith("dn_"):
        return None
    hex_part = raw[3:]
    if len(hex_part) != 32 or any(c not in "0123456789abcdef" for c in hex_part.lower()):
        return None
    h = hex_part.lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


class ProviderError(Exception):
    pass


def http_json(method, url, *, headers=None, form=None, body=None, basic_auth=None, timeout=25):
    """Small urllib JSON client. Returns (status, parsed_json_or_text)."""
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form, doseq=True).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    if basic_auth:
        token = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
        headers["Authorization"] = "Basic " + token
    headers.setdefault("Accept", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"Network error: {exc}") from exc
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text) if text.strip() else {}
    except ValueError:
        return status, text


def http_bytes(url, *, headers=None, basic_auth=None, timeout=30, max_bytes=50 * 1024 * 1024):
    headers = dict(headers or {})
    if basic_auth:
        token = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
        headers["Authorization"] = "Basic " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
            ctype = resp.headers.get("Content-Type", "application/octet-stream")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"Download failed: {exc}") from exc
    if len(data) > max_bytes:
        raise ProviderError("Download too large")
    return data, ctype


# --------------------------------------------------------------------------
# Estimated cost
# --------------------------------------------------------------------------
DEFAULT_RATES = {
    # USD per billable minute. Editable by the admin in Settings.
    "telnyx_out": 0.0070,
    "telnyx_in": 0.0055,
    "twilio_out": 0.0140,
    "twilio_in": 0.0085,
    "usd_to_pkr": 280.0,
}


def billable_minutes(seconds) -> int:
    seconds = int(seconds or 0)
    return math.ceil(seconds / 60) if seconds > 0 else 0


def call_cost(seconds, provider, direction, rates) -> float:
    provider = provider if provider in ("telnyx", "twilio") else "telnyx"
    leg = "in" if direction == "incoming" else "out"
    rate = float(rates.get(f"{provider}_{leg}", DEFAULT_RATES[f"{provider}_{leg}"]))
    return round(billable_minutes(seconds) * rate, 6)


# --------------------------------------------------------------------------
# Supabase Storage (private bucket)
# --------------------------------------------------------------------------
class SupabaseStorage:
    def __init__(self, url, service_key, bucket="voxsbridge-recordings"):
        self.url = (url or "").rstrip("/")
        self.key = service_key or ""
        self.bucket = bucket
        self._bucket_ready = False

    @property
    def enabled(self):
        return bool(self.url and self.key)

    def _headers(self, extra=None):
        h = {"Authorization": f"Bearer {self.key}", "apikey": self.key}
        h.update(extra or {})
        return h

    def _ensure_bucket(self):
        if self._bucket_ready:
            return
        status, _ = http_json("POST", f"{self.url}/storage/v1/bucket", headers=self._headers(),
                              body={"id": self.bucket, "name": self.bucket, "public": False})
        # 200 created; 400/409 when it already exists.
        if status in (200, 201, 400, 409):
            self._bucket_ready = True

    def upload(self, path, data: bytes, content_type: str):
        self._ensure_bucket()
        quoted = urllib.parse.quote(path)
        status, resp = http_json("POST", f"{self.url}/storage/v1/object/{self.bucket}/{quoted}",
                                 headers=self._headers({"Content-Type": content_type, "x-upsert": "true"}),
                                 body=data, timeout=60)
        if status not in (200, 201):
            raise ProviderError(f"Storage upload failed ({status})")

    def download(self, path):
        quoted = urllib.parse.quote(path)
        return http_bytes(f"{self.url}/storage/v1/object/{self.bucket}/{quoted}", headers=self._headers())

    def delete(self, path):
        status, _ = http_json("DELETE", f"{self.url}/storage/v1/object/{self.bucket}",
                              headers=self._headers(), body={"prefixes": [path]})
        return status in (200, 204)


# --------------------------------------------------------------------------
# Security middleware (pure ASGI so request bodies stay readable downstream)
# --------------------------------------------------------------------------
CSRF_COOKIE = "dn_csrf"
CSRF_HEADER = b"x-csrf-token"
CSRF_FIELD = "csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def build_csp():
    return "; ".join([
        "default-src 'self'",
        # Existing templates use inline scripts and onclick handlers.
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com data:",
        "img-src 'self' data: blob:",
        "media-src 'self' blob: https://sdk.twilio.com https://media.twiliocdn.com",
        "connect-src 'self' wss://*.telnyx.com https://*.telnyx.com wss://*.twilio.com https://*.twilio.com",
        "worker-src 'self' blob:",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
    ])


class SecurityMiddleware:
    def __init__(self, app, exempt_prefixes=("/webhooks/", "/twilio/", "/static/"),
                 trusted_hosts=None, report_only=False, max_form_bytes=2 * 1024 * 1024):
        self.app = app
        self.exempt = tuple(exempt_prefixes)
        self.trusted_hosts = {h.strip().lower() for h in (trusted_hosts or []) if h.strip()}
        self.report_only = report_only
        self.max_form_bytes = max_form_bytes
        self.csp = build_csp()

    @staticmethod
    def _headers(scope):
        out = {}
        for k, v in scope.get("headers", []):
            out.setdefault(k.lower(), v.decode("latin-1"))
        return out

    @staticmethod
    def _cookie(headers, name):
        raw = headers.get(b"cookie")
        if not raw:
            return ""
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return ""
        return jar[name].value if name in jar else ""

    def _host_ok(self, value, host):
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError:
            return False
        netloc = (parsed.netloc or "").lower()
        if not netloc:
            return False
        return netloc == (host or "").lower() or netloc in self.trusted_hosts

    async def _reject(self, send, reason):
        body = json.dumps({"detail": "Security check failed. Please refresh the page and try again.",
                           "reason": reason}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = self._headers(scope)
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        cookie_token = self._cookie(headers, CSRF_COOKIE)
        new_token = None if cookie_token else secrets.token_urlsafe(32)

        if method not in SAFE_METHODS and not path.startswith(self.exempt):
            host = headers.get(b"host", "")
            origin = headers.get(b"origin", "")
            referer = headers.get(b"referer", "")
            if origin and origin != "null":
                if not self._host_ok(origin, host):
                    return await self._reject(send, "origin")
            elif referer and not self._host_ok(referer, host):
                return await self._reject(send, "referer")

            supplied = headers.get(CSRF_HEADER, "")
            ctype = headers.get(b"content-type", "").lower()
            if not supplied and ctype.startswith("application/x-www-form-urlencoded"):
                chunks, size = [], 0
                more = True
                while more:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > self.max_form_bytes:
                        return await self._reject(send, "form-too-large")
                    chunks.append(chunk)
                    more = message.get("more_body", False)
                body = b"".join(chunks)
                values = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
                supplied = (values.get(CSRF_FIELD) or [""])[0]
                replayed = False

                async def replay():
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await receive()
                receive = replay
            if not cookie_token or not supplied or not hmac.compare_digest(cookie_token, supplied):
                return await self._reject(send, "token")

        csp_header = b"content-security-policy-report-only" if self.report_only else b"content-security-policy"
        security = [
            (csp_header, self.csp.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"permissions-policy", b"microphone=(self), camera=(), geolocation=(), payment=()"),
            (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
            (b"cross-origin-opener-policy", b"same-origin"),
        ]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in security if k not in existing]
                if new_token:
                    cookie = (f"{CSRF_COOKIE}={new_token}; Path=/; Max-Age=31536000; "
                              f"SameSite=Lax; Secure").encode()
                    extra.append((b"set-cookie", cookie))
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        return await self.app(scope, receive, send_wrapper)


def env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Database connection pool
# --------------------------------------------------------------------------
class ConnectionPool:
    """Re-use open PostgreSQL connections instead of opening one per query.

    `with pool.connection() as conn:` behaves like `with psycopg.connect() as conn:`
    (commit on success, rollback on error) but hands the connection back to the
    pool instead of closing it. Stale or broken connections are replaced.
    """

    def __init__(self, factory, max_idle=8, max_idle_age=240.0, ping_after=20.0):
        import threading
        self._factory = factory
        self._idle = []            # list of (connection, last_used)
        self._lock = threading.Lock()
        self.max_idle = max_idle
        self.max_idle_age = max_idle_age
        self.ping_after = ping_after

    @staticmethod
    def _close(conn):
        try:
            conn.close()
        except Exception:
            pass

    @staticmethod
    def _unusable(conn):
        return bool(getattr(conn, "closed", False) or getattr(conn, "broken", False))

    def _acquire(self):
        while True:
            with self._lock:
                item = self._idle.pop() if self._idle else None
            if item is None:
                return self._factory()
            conn, last_used = item
            age = time.monotonic() - last_used
            if self._unusable(conn) or age > self.max_idle_age:
                self._close(conn)
                continue
            if age > self.ping_after:
                try:
                    conn.execute("SELECT 1")
                    conn.rollback()
                except Exception:
                    self._close(conn)
                    continue
            return conn

    def _release(self, conn):
        status = getattr(getattr(conn, "info", None), "transaction_status", 0)
        if self._unusable(conn) or int(status) != 0:   # 0 = IDLE (no open transaction)
            self._close(conn)
            return
        with self._lock:
            if len(self._idle) < self.max_idle:
                self._idle.append((conn, time.monotonic()))
                return
        self._close(conn)

    def connection(self):
        return _PoolLease(self)

    def close_all(self):
        with self._lock:
            idle, self._idle = self._idle, []
        for conn, _ in idle:
            self._close(conn)


class _PoolLease:
    def __init__(self, pool):
        self.pool = pool
        self.conn = None

    def __enter__(self):
        self.conn = self.pool._acquire()
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        conn, self.conn = self.conn, None
        try:
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
        except Exception:
            self.pool._close(conn)
            if exc_type is None:
                raise
            return False
        self.pool._release(conn)
        return False
