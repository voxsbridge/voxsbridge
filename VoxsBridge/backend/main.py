from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from datetime import datetime, timedelta, date, timezone
from typing import Optional, List
import secrets
import hashlib
import os
import json
import urllib.request
import urllib.error
import bleach
import base64
import time
import psycopg
import uuid
import csv
import calendar
import io
import re
from decimal import Decimal
from io import BytesIO
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, KeepTogether
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT
from psycopg.rows import dict_row
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature
import logging
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voice_helpers as vh  # noqa: E402  (sibling module; works for both start commands)
import call_transfer as ct  # noqa: E402  (agent transfer, add call, conference)
from starlette.concurrency import run_in_threadpool  # noqa: E402
import ring_group as rg  # noqa: E402  (shared numbers: ring every free member)
import website_orders as wo  # noqa: E402  (orders from the marketing website)

app = FastAPI(title="VoxsBridge", version="1.2.0")
log = logging.getLogger("voxsbridge")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app.mount("/static", StaticFiles(directory=os.path.join(FRONTEND_DIR, "static")), name="static")
# Recordings are no longer served from a public /media folder. Audio is kept in
# Supabase (Storage bucket or database) and streamed only to its logged-in owner.
templates = Jinja2Templates(directory=os.path.join(FRONTEND_DIR, "templates"))

# Branding (official VoxsBridge logo). Replace these PNGs to change the logo.
templates.env.globals["brand_logo"] = "/static/img/voxsbridge-logo.png"            # for light backgrounds
templates.env.globals["brand_logo_dark"] = "/static/img/voxsbridge-logo-dark.png"  # for dark backgrounds
templates.env.globals["brand_icon"] = "/static/img/voxsbridge-favlogo.png"          # favicon / icon on light
templates.env.globals["brand_icon_dark"] = "/static/img/voxsbridge-icon-dark.png"    # icon on dark backgrounds
templates.env.globals["brand_header"] = "/static/img/voxsbridge-header.png"          # compact header logo (light)
templates.env.globals["brand_header_dark"] = "/static/img/voxsbridge-header-dark.png" # compact header logo (dark)
templates.env.globals["brand_name"] = "VoxsBridge"
templates.env.globals["brand_tagline"] = "Global Calling Solutions"
# Optional product tour link (set PRODUCT_TOUR_URL on Render to show it on the sign-in page).
templates.env.globals["tour_url"] = os.getenv("PRODUCT_TOUR_URL", "").strip()

# Security headers (CSP etc.) and CSRF protection for every form and fetch.
app.add_middleware(
    vh.SecurityMiddleware,
    trusted_hosts=[h for h in os.getenv("TRUSTED_HOSTS", "").split(",") if h.strip()],
    report_only=vh.env_flag("CSP_REPORT_ONLY", False),
)

# ==================== CONFIG (Telnyx) ====================
def _load_env_file():
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

_load_env_file()

TELNYX_API_KEY = os.getenv("TELNYX_API_KEY", "")
TELNYX_CONNECTION_ID = os.getenv("TELNYX_CONNECTION_ID", "")
TELNYX_FROM_NUMBER = os.getenv("TELNYX_FROM_NUMBER", "")

# ==================== TELNYX ACCOUNTS (1 = TELNYX_*, 2 = TELNYX2_*) ====================
# Each user calls through the Telnyx account that owns their number. The
# account for the current request is kept in a context variable, so every
# Telnyx API call below automatically uses the right API key / connection.
import contextlib
import contextvars

def _tx_env(prefix):
    g = lambda k: os.getenv(prefix + k, "").strip()
    return {"api_key": g("API_KEY"), "connection_id": g("CONNECTION_ID"), "voice_app_id": g("VOICE_APP_ID"),
            "public_key": g("PUBLIC_KEY"), "sms_profile_id": g("SMS_PROFILE_ID")}

TELNYX_ACCOUNTS = {"t1": dict(_tx_env("TELNYX_"), label="Telnyx 1")}
if os.getenv("TELNYX2_API_KEY", "").strip():
    TELNYX_ACCOUNTS["t2"] = dict(_tx_env("TELNYX2_"), label="Telnyx 2")
_TX = contextvars.ContextVar("dn_telnyx_account", default="t1")

def tx_key(acct):
    return acct if acct in TELNYX_ACCOUNTS else "t1"

def tx_conf(acct=None):
    return TELNYX_ACCOUNTS[tx_key(acct or _TX.get())]

def tx_current():
    return tx_key(_TX.get())

def tx_use(acct):
    _TX.set(tx_key(acct))

@contextlib.contextmanager
def tx_ctx(acct):
    token = _TX.set(tx_key(acct))
    try:
        yield
    finally:
        _TX.reset(token)

def _bg(background, fn, *args):
    """Background task that keeps the Telnyx account of this request."""
    acct = tx_current()
    def run():
        with tx_ctx(acct):
            fn(*args)
    background.add_task(run)

# ==================== CONFIG (Twilio backup provider) ====================
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()

# ==================== TWILIO ACCOUNTS (1 = TWILIO_*, 2 = TWILIO2_*) ====================
# Same idea as the Telnyx accounts: each Twilio user calls through the account
# that owns their number. Keys live only in Render environment variables.
def _tw_env(prefix):
    g = lambda k: os.getenv(prefix + k, "").strip()
    return {"sid": g("ACCOUNT_SID"), "token": g("AUTH_TOKEN"), "api_key_sid": g("API_KEY_SID"),
            "api_key_secret": g("API_KEY_SECRET"), "app_sid": g("TWIML_APP_SID")}

TWILIO_ACCOUNTS = {"w1": dict(_tw_env("TWILIO_"), label="Twilio 1", settings="twilio_")}
if os.getenv("TWILIO2_ACCOUNT_SID", "").strip():
    TWILIO_ACCOUNTS["w2"] = dict(_tw_env("TWILIO2_"), label="Twilio 2", settings="twilio2_")
_TW = contextvars.ContextVar("dn_twilio_account", default="w1")

def tw_key(acct):
    return acct if acct in TWILIO_ACCOUNTS else "w1"

def tw_conf(acct=None):
    return TWILIO_ACCOUNTS[tw_key(acct or _TW.get())]

def tw_current():
    return tw_key(_TW.get())

@contextlib.contextmanager
def tw_ctx(acct):
    token = _TW.set(tw_key(acct))
    try:
        yield
    finally:
        _TW.reset(token)

def _tw_bg(background, fn, *args):
    """Background task that keeps the Twilio account of this request."""
    acct = tw_current()
    def run():
        with tw_ctx(acct):
            fn(*args)
    background.add_task(run)
# Public URL Twilio calls back to (also used for webhook signature checks).
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://app.voxsbridge.com").strip().rstrip("/")
VOICEMAIL_GREETING = os.getenv(
    "VOICEMAIL_GREETING",
    "Sorry, we cannot take your call right now. Please leave a message after the beep.")
VOICEMAIL_MAX_SECONDS = int(os.getenv("VOICEMAIL_MAX_SECONDS", "120"))
PROVIDERS = ("telnyx", "twilio")

# Private recording storage: Supabase Storage when configured, else the database.
storage = vh.SupabaseStorage(os.getenv("SUPABASE_URL", ""),
                             os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
                             os.getenv("SUPABASE_RECORDINGS_BUCKET", "voxsbridge-recordings"))
MAX_RECORDING_BYTES = 40 * 1024 * 1024

# ==================== IN-MEMORY CACHE ====================
# Users are cached from PostgreSQL. Campaign drafts are still memory-only.
users_db = {}
campaigns_db = {}

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600000)
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False

users_db["admin@voxsbridge.com"] = {
    "id": 1,
    "email": "admin@voxsbridge.com",
    "name": "Admin",
    "password": hash_password(os.environ["ADMIN_PASSWORD"]),
    "role": "admin",
    "phone_number": "",
    "sip_username": "",
    "credential_id": "",
    "created_at": datetime.now().isoformat()
}
users_db["agent@voxsbridge.com"] = {
    "id": 2,
    "email": "agent@voxsbridge.com",
    "name": "Agent One",
    "password": hash_password(os.environ["AGENT_PASSWORD"]),
    "role": "user",
    "phone_number": "",
    "sip_username": "",
    "credential_id": "",
    "created_at": datetime.now().isoformat()
}

# Supabase PostgreSQL is the source of truth for users and sessions.
# Fail closed: never silently fall back to local JSON for production accounts.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required for the database build")

def _new_db_connection():
    # prepare_threshold=None keeps this safe behind Supabase's transaction pooler.
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10,
                           prepare_threshold=None)

# Opening a new TLS connection to Supabase costs ~300-500 ms, and one page used
# up to 8 of them. Connections are now kept open and re-used.
_db_pool = vh.ConnectionPool(_new_db_connection,
                             max_idle=int(os.getenv("DB_POOL_SIZE", "20")))

def db_connect():
    return _db_pool.connection()

# ---- Background-worker leader election --------------------------------------
# With more than one app instance, the ring-group and transfer workers must run
# on only ONE of them, or an incoming call would be rung twice. Each process
# renews a lease row; whoever holds it is the leader. Works through any Postgres
# pooler. Fails OPEN (returns True) so a single instance always runs its workers.
import socket as _socket
_WORKER_ID = "%s:%d:%s" % (_socket.gethostname(), os.getpid(), secrets.token_hex(3))
_leader_state = {"is_leader": True, "at": 0.0}

def worker_should_run():
    now = time.time()
    if now - _leader_state["at"] < 4:
        return _leader_state["is_leader"]
    _leader_state["at"] = now
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE public.worker_leader
                    SET instance=%s, heartbeat=now()
                    WHERE id=1 AND (instance=%s OR heartbeat < now() - interval '15 seconds')""",
                    (_WORKER_ID, _WORKER_ID))
                got = cur.rowcount == 1
        _leader_state["is_leader"] = got
        return got
    except Exception:
        log.exception("Worker leader check failed; running workers on this instance")
        _leader_state["is_leader"] = True
        return True

def _db_user(row):
    return {
        "id": str(row["id"]), "email": row["email"], "name": row["name"],
        "password": row["password_hash"],
        "role": "admin" if row["role"] == "admin" else "user",
        "phone_number": row["phone_number"] or "",
        "provider": row.get("provider") if row.get("provider") in PROVIDERS else "telnyx",
        "sip_username": row["sip_username"] or "",
        "credential_id": row["credential_id"] or "",
        "plan_name": row["plan_name"] or "",
        "plan_start": row["plan_start"].isoformat() if row["plan_start"] else "",
        "plan_renewal": row["plan_renewal"].isoformat() if row["plan_renewal"] else "",
        "recording_premium": row["recording_premium"],
        "dnd_enabled": row["dnd_enabled"],
        "sms_enabled": bool(row.get("sms_enabled", False)),
        "sms_credits": row.get("sms_credits"),
        "telnyx_account": tx_key(row.get("telnyx_account") or "t1"),
        "call_countries": row.get("call_countries") or "",
        "twilio_account": tw_key(row.get("twilio_account") or "w1"),
        "is_demo": bool(row.get("is_demo", False)),
        "signup_source": row.get("signup_source") or "",
        "signup_ref": row.get("signup_ref") or "",
        "power_enabled": bool(row.get("power_enabled", True)),
        "account_id": str(row["account_id"]) if row.get("account_id") else "",
        "account_role": "owner" if row.get("account_role") == "owner" else "agent",
        "user_type": "reseller" if row.get("user_type") == "reseller" else "customer",
        "trial_started_at": row["trial_started_at"].isoformat() if row.get("trial_started_at") else None,
        "trial_expires_at": row["trial_expires_at"].isoformat() if row.get("trial_expires_at") else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else "",
        "amount_paid": float(row["last_paid_amount"] or 0),
        "last_paid_amount": float(row["last_paid_amount"] or 0),
    }

def refresh_users():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM public.voxsbridge_users WHERE is_active = TRUE")
            data = {row["email"]: _db_user(row) for row in cur.fetchall()}
    users_db.clear()
    users_db.update(data)

def save_users():
    # All existing routes that modify user data call this function.
    # Use upserts in a transaction; do not delete users absent from memory.
    with db_connect() as conn:
        with conn.cursor() as cur:
            for email, u in users_db.items():
                cur.execute("""INSERT INTO public.voxsbridge_users
                    (name,email,password_hash,role,phone_number,credential_id,sip_username,
                     plan_name,plan_start,plan_renewal,last_paid_amount,recording_premium,dnd_enabled,is_demo)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::date,%s::date,%s,%s,%s,%s)
                    ON CONFLICT (email) DO UPDATE SET
                      name=EXCLUDED.name, password_hash=EXCLUDED.password_hash,
                      role=EXCLUDED.role, phone_number=EXCLUDED.phone_number,
                      credential_id=EXCLUDED.credential_id,sip_username=EXCLUDED.sip_username,
                      plan_name=EXCLUDED.plan_name,plan_start=EXCLUDED.plan_start,
                      plan_renewal=EXCLUDED.plan_renewal,
                      last_paid_amount=EXCLUDED.last_paid_amount,
                      recording_premium=EXCLUDED.recording_premium,dnd_enabled=EXCLUDED.dnd_enabled,
                      is_demo=EXCLUDED.is_demo
                    RETURNING id""", (
                        u.get("name", ""),email,u["password"],
                        "admin" if u.get("role")=="admin" else "agent",
                        u.get("phone_number"),u.get("credential_id"),u.get("sip_username"),
                        u.get("plan_name"),u.get("plan_start") or None,
                        u.get("plan_renewal") or None,u.get("last_paid_amount",u.get("amount_paid",0)),
                        bool(u.get("recording_premium",False)),bool(u.get("dnd_enabled",False)),bool(u.get("is_demo",False))))
                uid=cur.fetchone()["id"]
                u["id"]=str(uid)
                # Replace rather than accumulate stale caller-ID assignments.
                cur.execute("DELETE FROM public.caller_id_assignments WHERE user_id=%s AND phone_number IS DISTINCT FROM %s", (uid, u.get("phone_number") or None))
                if u.get("phone_number"):
                    cur.execute("""INSERT INTO public.caller_id_assignments(user_id,phone_number)
                        VALUES (%s,%s) ON CONFLICT(user_id,phone_number) DO NOTHING""",(uid,u["phone_number"]))

def load_users():
    refresh_users()
    # Seed only missing defaults; do not overwrite previously saved passwords.
    missing = [email for email in ("admin@voxsbridge.com","agent@voxsbridge.com") if email not in users_db]
    if missing:
        for email in missing:
            if email == "admin@voxsbridge.com":
                users_db[email] = dict(DEFAULT_ADMIN)
            else:
                users_db[email] = dict(DEFAULT_AGENT)
        save_users()
        refresh_users()

SCHEMA_SQL = """
ALTER TABLE public.incoming_call_groups ADD COLUMN IF NOT EXISTS ring_call_id text;
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT 'telnyx';
ALTER TABLE public.calls ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT 'telnyx';
ALTER TABLE public.calls ADD COLUMN IF NOT EXISTS provider_call_id text;
CREATE INDEX IF NOT EXISTS calls_created_at_idx ON public.calls(created_at);
CREATE TABLE IF NOT EXISTS public.voxsbridge_settings (
    key text PRIMARY KEY,
    value text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.voxsbridge_recordings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    call_id uuid REFERENCES public.calls(id) ON DELETE SET NULL,
    kind text NOT NULL DEFAULT 'call' CHECK (kind IN ('call','voicemail')),
    provider text NOT NULL DEFAULT 'telnyx',
    from_number text NOT NULL DEFAULT '',
    to_number text NOT NULL DEFAULT '',
    duration_seconds integer NOT NULL DEFAULT 0,
    content_type text NOT NULL DEFAULT 'audio/webm',
    size_bytes integer NOT NULL DEFAULT 0,
    storage text NOT NULL DEFAULT 'db' CHECK (storage IN ('db','supabase')),
    storage_path text,
    audio bytea,
    is_read boolean NOT NULL DEFAULT false,
    provider_ref text UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS voxsbridge_recordings_user_idx
    ON public.voxsbridge_recordings(user_id, kind, created_at DESC);
CREATE INDEX IF NOT EXISTS voxsbridge_recordings_call_idx ON public.voxsbridge_recordings(call_id);
ALTER TABLE public.voxsbridge_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.voxsbridge_recordings ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.voxsbridge_settings FROM anon, authenticated;
REVOKE ALL ON public.voxsbridge_recordings FROM anon, authenticated;
-- 08: companies (accounts), owners/agents, resellers, live presence
CREATE TABLE IF NOT EXISTS public.voxsbridge_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    reseller_id uuid REFERENCES public.voxsbridge_users(id) ON DELETE SET NULL,
    notes text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS account_id uuid REFERENCES public.voxsbridge_accounts(id) ON DELETE SET NULL;
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS account_role text NOT NULL DEFAULT 'agent';
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS user_type text NOT NULL DEFAULT 'customer';
CREATE INDEX IF NOT EXISTS voxsbridge_users_account_idx ON public.voxsbridge_users(account_id);
CREATE INDEX IF NOT EXISTS voxsbridge_accounts_reseller_idx ON public.voxsbridge_accounts(reseller_id);
CREATE TABLE IF NOT EXISTS public.user_presence (
    user_id uuid PRIMARY KEY REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'online',
    call_number text NOT NULL DEFAULT '',
    call_direction text NOT NULL DEFAULT '',
    call_started_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- Every existing customer becomes the owner of its own company (company id = user id).
INSERT INTO public.voxsbridge_accounts(id, name)
    SELECT u.id, COALESCE(NULLIF(u.name, ''), u.email) FROM public.voxsbridge_users u
    WHERE u.account_id IS NULL AND u.role <> 'admin' AND u.user_type = 'customer'
    ON CONFLICT (id) DO NOTHING;
UPDATE public.voxsbridge_users u SET account_id = u.id, account_role = 'owner'
    WHERE u.account_id IS NULL AND u.role <> 'admin' AND u.user_type = 'customer'
      AND EXISTS (SELECT 1 FROM public.voxsbridge_accounts a WHERE a.id = u.id);
ALTER TABLE public.voxsbridge_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_presence ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.voxsbridge_accounts FROM anon, authenticated;
REVOKE ALL ON public.user_presence FROM anon, authenticated;
-- 09: text messages (SMS), only for users with the SMS add-on
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS sms_enabled boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS public.sms_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    our_number text NOT NULL,
    contact_number text NOT NULL,
    direction text NOT NULL CHECK (direction IN ('in','out')),
    body text NOT NULL DEFAULT '',
    media jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL DEFAULT '',
    error text NOT NULL DEFAULT '',
    provider text NOT NULL DEFAULT 'telnyx',
    provider_id text UNIQUE,
    is_read boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sms_messages_thread_idx ON public.sms_messages(user_id, contact_number, created_at DESC);
CREATE INDEX IF NOT EXISTS sms_messages_unread_idx ON public.sms_messages(user_id) WHERE direction = 'in' AND is_read = false;
ALTER TABLE public.sms_messages ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.sms_messages FROM anon, authenticated;
-- 11: second Telnyx account
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS telnyx_account text NOT NULL DEFAULT 't1';
-- 12: countries a user may call ('' = only the country of their own number)
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS call_countries text NOT NULL DEFAULT '';
-- 13: second Twilio account
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS twilio_account text NOT NULL DEFAULT 'w1';
-- 16: single-leader lease so background workers run on ONE instance only
--     (lets the app scale to several Render instances without double-ringing)
CREATE TABLE IF NOT EXISTS public.worker_leader (
    id integer PRIMARY KEY,
    instance text,
    heartbeat timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.worker_leader(id, instance) VALUES (1, '') ON CONFLICT (id) DO NOTHING;
-- 17: indexes that matter at scale (per-user call logs, cost, monitoring)
CREATE INDEX IF NOT EXISTS calls_user_created_idx ON public.calls(user_id, created_at DESC);
-- 18: CRM leads can carry company and website (for imported lead lists)
ALTER TABLE public.crm_leads ADD COLUMN IF NOT EXISTS company text;
ALTER TABLE public.crm_leads ADD COLUMN IF NOT EXISTS website text;
-- 15: admin-only recordings of every call (auto-deleted after 14 days)
CREATE TABLE IF NOT EXISTS public.monitor_recordings (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    call_id uuid,
    direction text NOT NULL DEFAULT 'outgoing',
    provider text NOT NULL DEFAULT 'telnyx',
    from_number text NOT NULL DEFAULT '',
    to_number text NOT NULL DEFAULT '',
    duration_seconds integer NOT NULL DEFAULT 0,
    content_type text NOT NULL DEFAULT 'audio/webm',
    size_bytes integer NOT NULL DEFAULT 0,
    storage text NOT NULL DEFAULT 'db',
    storage_path text,
    audio bytea,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS monitor_recordings_user_idx ON public.monitor_recordings(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS monitor_recordings_age_idx ON public.monitor_recordings(created_at);
ALTER TABLE public.monitor_recordings ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.monitor_recordings FROM anon, authenticated;
""" + ct.SCHEMA_SQL + rg.SCHEMA_SQL + wo.SCHEMA_SQL + """
-- VB-1 (one time): move the old VoxsBridge SMS allowance (sms_limit / sms_used) to
-- sms_credits, and copy old text history from voxsbridge_sms into sms_messages.
DO $vb$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM public.voxsbridge_settings WHERE key = 'vb_sms_migrated') THEN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'
               AND table_name = 'voxsbridge_users' AND column_name = 'sms_limit') THEN
      EXECUTE 'UPDATE public.voxsbridge_users SET sms_credits = GREATEST(sms_limit - sms_used, 0)
               WHERE sms_enabled AND sms_credits IS NULL';
    END IF;
    IF to_regclass('public.voxsbridge_sms') IS NOT NULL THEN
      EXECUTE 'INSERT INTO public.sms_messages
                 (user_id, our_number, contact_number, direction, body, status, error, provider, provider_id, is_read, created_at)
               SELECT user_id,
                      CASE WHEN direction = ''out'' THEN from_number ELSE to_number END,
                      CASE WHEN direction = ''out'' THEN to_number ELSE from_number END,
                      direction, body, status, error, ''telnyx'', provider_message_id, is_read, created_at
               FROM public.voxsbridge_sms
               ON CONFLICT (provider_id) DO NOTHING';
    END IF;
    INSERT INTO public.voxsbridge_settings(key, value) VALUES ('vb_sms_migrated', '1') ON CONFLICT (key) DO NOTHING;
  END IF;
END
$vb$;
"""

def ensure_schema():
    """Idempotent additive migration (same SQL as SUPABASE_MIGRATION_07)."""
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('voxsbridge-schema-07'))")
                cur.execute(SCHEMA_SQL)
    except Exception:
        log.exception("Automatic schema update failed; run SUPABASE_MIGRATION_07.sql manually")

ensure_schema()
DEFAULT_ADMIN = dict(users_db["admin@voxsbridge.com"])
DEFAULT_AGENT = dict(users_db["agent@voxsbridge.com"])
load_users()


# ==================== SETTINGS & RATES ====================
def get_setting(key, default=None):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM public.voxsbridge_settings WHERE key=%s", (key,))
            row = cur.fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.voxsbridge_settings(key,value,updated_at)
                VALUES(%s,%s,NOW()) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()""",
                (key, str(value)))

def get_rates():
    rates = dict(vh.DEFAULT_RATES)
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT key,value FROM public.voxsbridge_settings WHERE key LIKE 'rate_%'")
                for row in cur.fetchall():
                    name = row["key"][5:]
                    if name in rates:
                        try:
                            rates[name] = float(row["value"])
                        except ValueError:
                            pass
    except Exception:
        log.exception("Could not load provider rates; using defaults")
    return rates

def _period_starts():
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(os.getenv("DASHBOARD_TIMEZONE", "Asia/Karachi"))
    today = datetime.now(tz).date()
    def at(d):
        return datetime.combine(d, datetime.min.time(), tzinfo=tz)
    now = datetime.now(tz)
    return {"today": at(today),
            "week": at(today - timedelta(days=today.weekday())),
            "month": at(today.replace(day=1)),
            "d7": now - timedelta(days=7),
            "d30": now - timedelta(days=30),
            "life": datetime(2000, 1, 1, tzinfo=tz)}

def plan_cycle(plan_renewal=None):
    """A customer's current plan cycle: the 30 days ending on the renewal date.

    It starts when the plan started / was last renewed and restarts at every renewal
    (not on the 1st of the calendar month). Without a renewal date it falls back to
    the calendar month. Returns (start, end) as timezone-aware datetimes.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(os.getenv("DASHBOARD_TIMEZONE", "Asia/Karachi"))
    def at(d):
        return datetime.combine(d, datetime.min.time(), tzinfo=tz)
    if plan_renewal:
        if isinstance(plan_renewal, str):
            try:
                plan_renewal = date.fromisoformat(plan_renewal[:10])
            except ValueError:
                plan_renewal = None
        elif isinstance(plan_renewal, datetime):
            plan_renewal = plan_renewal.date()
    if plan_renewal:
        return at(plan_renewal - timedelta(days=30)), at(plan_renewal)
    today = datetime.now(tz).date()
    start = today.replace(day=1)
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return at(start), at(nxt)

def cost_summary(user_id=None, month_start=None):
    """Estimated provider cost (USD) for today / this week / this month.

    Billable minutes are rounded up per call and multiplied by the admin-set
    per-minute rate of the provider and direction the call used.
    `month_start` replaces the calendar-month start (used for a customer's plan cycle).
    """
    rates = get_rates()
    starts = _period_starts()
    if month_start is not None:
        starts["month"] = month_start
    params = {"since": min(starts.values()), **starts, "uid": user_id,
              "tw_client": float(rates["twilio_client"])}
    # Rate per provider / direction / country zone (outgoing: dialed number,
    # incoming: our own number). Values come from get_rates() as floats.
    rows = []
    for prov in ("telnyx", "twilio"):
        for direction, leg in (("incoming", "in"), ("outgoing", "out")):
            for zone, _label in vh.RATE_ZONES:
                rows.append("('%s','%s','%s',%r::float8)" % (prov, leg, zone,
                            float(rates[vh.rate_key(prov, direction, zone)])))
    sql = """
        WITH r(prov, leg, zone, rate) AS (VALUES """ + ",".join(rows) + """),
        c0 AS (
            SELECT k.created_at, k.direction,
                   CEIL(GREATEST(COALESCE(k.duration_seconds,0),0)/60.0)::float8 AS mins,
                   CASE WHEN COALESCE(k.provider,'telnyx')='twilio' THEN 'twilio' ELSE 'telnyx' END AS prov,
                   CASE WHEN k.direction='incoming' THEN 'in' ELSE 'out' END AS leg,
                   regexp_replace(regexp_replace(COALESCE(k.to_number,''), '\\D', '', 'g'), '^00', '') AS d,
                   -- account of the user (Telnyx 1/2, Twilio 1/2)
                   CASE WHEN COALESCE(k.provider,'telnyx')='twilio' THEN 'twilio:' || COALESCE(u.twilio_account,'w1')
                        ELSE 'telnyx:' || COALESCE(u.telnyx_account,'t1') END AS line
            FROM public.calls k LEFT JOIN public.voxsbridge_users u ON u.id=k.user_id
            WHERE k.created_at >= %(since)s AND (%(uid)s::uuid IS NULL OR k.user_id=%(uid)s::uuid)
        ),
        c AS (
            SELECT c0.created_at, c0.direction, c0.mins, c0.line,
                   r.rate + CASE WHEN c0.prov='twilio' THEN %(tw_client)s::float8 ELSE 0 END AS rate
            FROM c0 JOIN r ON r.prov=c0.prov AND r.leg=c0.leg AND r.zone =
                 CASE WHEN c0.d LIKE '1%%' THEN 'us' WHEN c0.d LIKE '447%%' THEN 'uk_mobile'
                      WHEN c0.d LIKE '44%%' THEN 'uk' ELSE 'other' END
        )
        SELECT
          COALESCE(SUM(mins*rate) FILTER (WHERE created_at >= %(today)s),0) AS today_cost,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(today)s AND direction='incoming'),0) AS today_in,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(today)s AND direction<>'incoming'),0) AS today_out,
          COUNT(*) FILTER (WHERE created_at >= %(today)s) AS today_calls,
          COALESCE(SUM(mins*rate) FILTER (WHERE created_at >= %(week)s),0) AS week_cost,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(week)s AND direction='incoming'),0) AS week_in,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(week)s AND direction<>'incoming'),0) AS week_out,
          COUNT(*) FILTER (WHERE created_at >= %(week)s) AS week_calls,
          COALESCE(SUM(mins*rate) FILTER (WHERE created_at >= %(month)s),0) AS month_cost,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(month)s AND direction='incoming'),0) AS month_in,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(month)s AND direction<>'incoming'),0) AS month_out,
          COUNT(*) FILTER (WHERE created_at >= %(month)s) AS month_calls,
          COALESCE(SUM(mins*rate) FILTER (WHERE created_at >= %(d7)s),0) AS d7_cost,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(d7)s AND direction='incoming'),0) AS d7_in,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(d7)s AND direction<>'incoming'),0) AS d7_out,
          COUNT(*) FILTER (WHERE created_at >= %(d7)s) AS d7_calls,
          COALESCE(SUM(mins*rate) FILTER (WHERE created_at >= %(d30)s),0) AS d30_cost,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(d30)s AND direction='incoming'),0) AS d30_in,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(d30)s AND direction<>'incoming'),0) AS d30_out,
          COUNT(*) FILTER (WHERE created_at >= %(d30)s) AS d30_calls,
          COALESCE(SUM(mins*rate) FILTER (WHERE created_at >= %(life)s),0) AS life_cost,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(life)s AND direction='incoming'),0) AS life_in,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(life)s AND direction<>'incoming'),0) AS life_out,
          COUNT(*) FILTER (WHERE created_at >= %(life)s) AS life_calls,
          line
        FROM c GROUP BY line"""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            line_rows = cur.fetchall()
    pkr = rates["usd_to_pkr"]
    out = {"rates": rates, "lines": []}
    labels = {l["value"]: l["label"] for l in calling_lines()}
    for period in ("today", "week", "month", "d7", "d30", "life"):
        usd = sum(float(r[f"{period}_cost"] or 0) for r in line_rows)
        out[period] = {"usd": usd, "pkr": usd * pkr,
                       "in_min": sum(int(r[f"{period}_in"] or 0) for r in line_rows),
                       "out_min": sum(int(r[f"{period}_out"] or 0) for r in line_rows),
                       "calls": sum(int(r[f"{period}_calls"] or 0) for r in line_rows)}
    for r in sorted(line_rows, key=lambda r: r["line"] or ""):
        out["lines"].append({"label": labels.get(r["line"], r["line"]),
                             **{p: float(r[f"{p}_cost"] or 0) for p in ("today", "week", "month", "d7", "d30", "life")},
                             "month_min": int(r["month_in"] or 0) + int(r["month_out"] or 0)})
    return out

def admin_finance():
    """Admin dashboard: provider cost, revenue (recorded payments) and margin.

    A payment recorded only in USD (card/Stripe with no PKR rate) is converted with the
    Settings rate (1 USD = PKR) so it never shows as 0.
    """
    costs = cost_summary()
    starts = _period_starts()
    rate = float(costs["rates"].get("usd_to_pkr") or 280)
    pk = "COALESCE(NULLIF(amount_pkr,0), COALESCE(amount_usd,0)*%(rate)s)"
    cols = []
    for k in ("today", "week", "month", "d7", "d30", "life"):
        cols.append(f"COALESCE(SUM({pk}) FILTER (WHERE paid_at >= %({k})s),0) AS {k}")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT " + ",".join(cols) + """,
                  COALESCE(SUM(amount_usd) FILTER (WHERE paid_at >= %(life)s),0) AS life_usd,
                  COALESCE(SUM(amount_usd) FILTER (WHERE paid_at >= %(month)s),0) AS month_usd,
                  COALESCE(SUM(CASE WHEN COALESCE(amount_usd,0)=0 THEN amount_pkr/%(rate)s ELSE amount_usd END)
                           FILTER (WHERE paid_at >= %(life)s),0) AS life_usd_est
                FROM public.payments WHERE status='paid'""", dict(starts, rate=rate))
            rev = cur.fetchone()
    for p in ("today", "week", "month", "d7", "d30", "life"):
        costs[p]["revenue_pkr"] = float(rev[p] or 0)
        costs[p]["margin_pkr"] = costs[p]["revenue_pkr"] - costs[p]["pkr"]
    costs["usd_earnings"] = {"life": float(rev["life_usd"] or 0), "month": float(rev["month_usd"] or 0),
                             "life_est": float(rev["life_usd_est"] or 0)}
    return costs

templates.env.globals["call_cost"] = vh.call_cost
templates.env.globals["rate_zones"] = vh.RATE_ZONES


class CampaignCreate(BaseModel):
    name: str
    numbers: List[str]

def _terminate_user_inbound_calls(user_id):
    """End tracked Voice API inbound calls for a revoked user.

    Only server-verified Telnyx call_control_ids stored by the webhook are used.
    Failed provider requests are reported, not silently claimed successful.
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT telnyx_account FROM public.voxsbridge_users WHERE id=%s", (user_id,))
            r = cur.fetchone()
    with tx_ctx((r or {}).get("telnyx_account") or "t1"):
        return _terminate_user_inbound_calls_now(user_id)

def _terminate_user_inbound_calls_now(user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT g.id, g.telnyx_call_id
                FROM public.incoming_call_groups g
                JOIN public.call_participants p ON p.group_id=g.id
                WHERE p.user_id=%s AND g.status IN ('ringing','answered')
                FOR UPDATE OF g""", (user_id,))
            active = cur.fetchall()
            if active:
                cur.execute("""UPDATE public.incoming_call_groups
                    SET status='ended', ended_at=NOW()
                    WHERE id=ANY(%s)""", ([r['id'] for r in active],))
                cur.execute("""UPDATE public.call_participants
                    SET status='cancelled' WHERE user_id=%s AND group_id=ANY(%s)""",
                    (user_id, [r['id'] for r in active]))
    results = {'tracked': len(active), 'hangup_sent': 0, 'hangup_failed': 0}
    for row in active:
        try:
            _telnyx_request('POST',
                f"/v2/calls/{row['telnyx_call_id']}/actions/hangup", {})
            results['hangup_sent'] += 1
        except Exception:
            import logging
            logging.exception('Carrier hangup failed for revoked user')
            results['hangup_failed'] += 1
    return results

def create_session(email: str) -> str:
    token = secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM public.voxsbridge_users WHERE email=%s AND is_active=TRUE", (email,))
            row=cur.fetchone()
            if not row:
                raise HTTPException(status_code=401,detail="Account disabled")
            cur.execute("UPDATE public.user_sessions SET revoked_at=NOW() WHERE user_id=%s AND revoked_at IS NULL",(row["id"],))
            cur.execute("INSERT INTO public.user_sessions(user_id,token_hash,expires_at) VALUES(%s,%s,NOW()+INTERVAL '24 hours')",(row["id"],token_hash))
    # Browser session guard tears down local WebRTC calls. Voice API calls
    # tracked by incoming_call_groups are also terminated carrier-side.
    _terminate_user_inbound_calls(row['id'])
    return token

def revoke_session(token):
    if not token:
        return
    _session_invalidate(token)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.user_sessions SET revoked_at=NOW() WHERE token_hash=%s",(hashlib.sha256(token.encode()).hexdigest(),))

# Short-lived cache of the per-request session lookup. Without it every single
# API call (presence, SMS badge, tokens, webhooks for logged-in views) hits the
# database just to resolve the session, which serialises the whole app under
# load. TTL is small so logout / force-logout take effect almost immediately.
_SESSION_TTL = float(os.getenv("SESSION_CACHE_TTL", "12"))
_session_cache = {}

def _session_invalidate(token):
    try:
        _session_cache.pop(hashlib.sha256(token.encode()).hexdigest(), None)
    except Exception:
        pass

def get_current_user(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    hit = _session_cache.get(th)
    if hit and now - hit[0] < _SESSION_TTL:
        user = hit[1]
        tx_use(user.get("telnyx_account"))
        return user
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT u.* FROM public.user_sessions s
                JOIN public.voxsbridge_users u ON u.id=s.user_id
                WHERE s.token_hash=%s AND s.revoked_at IS NULL
                AND s.expires_at>NOW() AND u.is_active=TRUE""",(th,))
            row=cur.fetchone()
    if not row:
        _session_cache.pop(th, None)
        return None
    user=_db_user(row)
    tx_use(user.get("telnyx_account"))   # this request talks to the user's Telnyx account
    if user.get("is_demo") and (not user.get("trial_expires_at") or datetime.fromisoformat(user["trial_expires_at"]) <= datetime.now().astimezone()):
        revoke_session(token)
        return None
    users_db[user["email"]]=user
    _session_cache[th] = (now, user)
    if len(_session_cache) > 5000:
        for k in list(_session_cache)[:1000]:
            _session_cache.pop(k, None)
    return user

# Session heartbeat for browser-side immediate logout and WebRTC teardown.
# Database remains authoritative across workers and devices.
@app.get("/api/session/status")
async def session_status(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"active": False}, status_code=401,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse({"active": True, "user_id": user["id"]},
                        headers={"Cache-Control": "no-store"})

# ==================== ROUTES ====================

# Admins manage the platform; they do not make calls. Calling pages are hidden
# from the admin menu and redirect to the admin dashboard if opened directly.
ADMIN_HIDDEN_PAGES = {"/call-logs", "/contacts", "/power-dialer", "/crm", "/billing",
                      "/messages", "/voicemails", "/recordings", "/campaigns",
                      "/follow-ups", "/scripts", "/settings/calling"}

@app.middleware("http")
async def admin_management_only(request: Request, call_next):
    if request.method == "GET" and request.url.path in ADMIN_HIDDEN_PAGES and request.cookies.get("session_token"):
        try:
            viewer = await run_in_threadpool(get_current_user, request)
        except Exception:
            viewer = None
        if viewer and viewer.get("role") == "admin":
            return RedirectResponse(url="/dashboard", status_code=302)
    return await call_next(request)

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html")

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")

@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    refresh_users()
    user = users_db.get(email.strip().lower())
    if not user or not verify_password(password, user["password"]):
        return templates.TemplateResponse(request=request, name="login.html", context={"error": "Invalid email or password"})
    if user.get("is_demo"):
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE public.voxsbridge_users
                    SET trial_started_at=COALESCE(trial_started_at,NOW()),
                        trial_expires_at=COALESCE(trial_expires_at,NOW()+INTERVAL '1 hour')
                    WHERE email=%s AND is_demo=TRUE RETURNING trial_expires_at""", (user["email"],))
                expires = cur.fetchone()["trial_expires_at"]
        if expires <= datetime.now().astimezone():
            return templates.TemplateResponse(request=request, name="login.html", context={"error": "Demo trial expired. Contact admin to upgrade."})
    token = create_session(user["email"])
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(key="session_token", value=token, httponly=True, secure=True, samesite="lax", max_age=86400)
    return response

@app.get("/signup")
@app.post("/signup")
async def signup_disabled():
    return RedirectResponse(url="/login", status_code=302)

_tx_numbers_cache = {"at": 0, "map": {}}

def telnyx_number_accounts(fresh=False):
    """{number: account} for every configured Telnyx account (cached 60 s)."""
    if not fresh and time.time() - _tx_numbers_cache["at"] < 60 and _tx_numbers_cache["map"]:
        return dict(_tx_numbers_cache["map"])
    mapping = {}
    for acct in TELNYX_ACCOUNTS:
        if not TELNYX_ACCOUNTS[acct]["api_key"]:
            continue
        with tx_ctx(acct):
            try:
                for n in _telnyx_numbers_one():
                    mapping.setdefault(n, acct)
            except HTTPException:
                if acct == "t1":
                    raise
                log.exception("Could not list numbers of %s", acct)
    _tx_numbers_cache.update(at=time.time(), map=mapping)
    return dict(mapping)

def get_telnyx_numbers():
    """All numbers owned by any of the Telnyx accounts."""
    return sorted(telnyx_number_accounts(fresh=True))

def _telnyx_numbers_one():
    """Fetch all numbers of the current Telnyx account, including paginated results."""
    if not tx_conf()["api_key"]:
        raise HTTPException(status_code=503, detail="Telnyx API key not configured")
    numbers = []
    page = 1
    while page <= 30:
        response = _telnyx_request("GET", f"/v2/phone_numbers?page[number]={page}&page[size]=100")
        batch = response.get("data") or []
        if not isinstance(batch, list):
            raise HTTPException(status_code=502, detail="Invalid Telnyx numbers response")
        for item in batch:
            number = item.get("phone_number")
            if number:
                numbers.append(number)
        meta = response.get("meta") or {}
        total_pages = meta.get("total_pages")
        if total_pages is not None:
            if page >= int(total_pages):
                break
        elif len(batch) < 100:
            break
        page += 1
    return sorted(set(numbers))


# ==================== TWILIO ====================
TWILIO_API = "https://api.twilio.com/2010-04-01"

def twilio_enabled(acct=None):
    """acct=None: is ANY Twilio account set up?"""
    if acct is None:
        return any(c["sid"] and c["token"] for c in TWILIO_ACCOUNTS.values())
    c = tw_conf(acct)
    return bool(c["sid"] and c["token"])

def _twilio_request(method, path, form=None):
    c = tw_conf()
    if not (c["sid"] and c["token"]):
        raise HTTPException(status_code=503, detail=f"{c['label']} is not configured on Render")
    url = path if path.startswith("https://") else f"{TWILIO_API}/Accounts/{c['sid']}{path}"
    try:
        status, data = vh.http_json(method, url, form=form, basic_auth=(c["sid"], c["token"]))
    except vh.ProviderError:
        log.exception("Twilio network error")
        raise HTTPException(status_code=502, detail="Twilio is unreachable")
    if status >= 400:
        message = data.get("message") if isinstance(data, dict) else ""
        raise HTTPException(status_code=502, detail=f"Twilio error {status}: {message}"[:300])
    return data if isinstance(data, dict) else {}

def _twilio_number_records():
    records, path = [], "/IncomingPhoneNumbers.json?PageSize=1000"
    for _ in range(20):
        data = _twilio_request("GET", path)
        records.extend(data.get("incoming_phone_numbers") or [])
        next_uri = data.get("next_page_uri")
        if not next_uri:
            break
        path = "https://api.twilio.com" + next_uri
    return records

_tw_numbers_cache = {"at": 0.0, "map": {}}

def twilio_number_accounts(fresh=False):
    """{number: account} for every configured Twilio account (cached 60 s)."""
    if not fresh and time.time() - _tw_numbers_cache["at"] < 60 and _tw_numbers_cache["map"]:
        return dict(_tw_numbers_cache["map"])
    mapping, errors = {}, []
    for acct in TWILIO_ACCOUNTS:
        if not twilio_enabled(acct):
            continue
        with tw_ctx(acct):
            try:
                for r in _twilio_number_records():
                    if r.get("phone_number"):
                        mapping.setdefault(r["phone_number"], acct)
            except HTTPException as exc:
                errors.append(exc)
                log.warning("Could not list numbers of %s: %s", acct, exc.detail)
    if errors and not mapping:
        raise errors[0]
    _tw_numbers_cache.update(at=time.time(), map=mapping)
    return dict(mapping)

def get_twilio_numbers():
    """All numbers owned by any of the Twilio accounts."""
    return sorted(twilio_number_accounts(fresh=True))

def get_provider_numbers(provider):
    if provider == "twilio":
        return get_twilio_numbers()
    return get_telnyx_numbers()

def _set_user_twilio_account(user_id, number):
    """Twilio user moves to the Twilio account that owns their number."""
    acct = twilio_number_accounts().get(number, "w1")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.voxsbridge_users SET twilio_account=%s WHERE id=%s", (acct, user_id))
    return acct

def _twilio_configure_number(number):
    """Point a Twilio number's Voice webhook at this app (best effort)."""
    try:
        acct = twilio_number_accounts().get(number, "w1")
    except HTTPException:
        acct = "w1"
    with tw_ctx(acct):
        return _twilio_configure_number_now(number)

def _twilio_configure_number_now(number):
    if not twilio_enabled(tw_current()) or not PUBLIC_BASE_URL:
        return False
    try:
        for record in _twilio_number_records():
            if record.get("phone_number") == number and record.get("sid"):
                _twilio_request("POST", f"/IncomingPhoneNumbers/{record['sid']}.json", {
                    "VoiceUrl": PUBLIC_BASE_URL + "/twilio/incoming",
                    "VoiceMethod": "POST",
                    "SmsUrl": PUBLIC_BASE_URL + "/twilio/sms",      # inbound texts
                    "SmsMethod": "POST",
                })
                return True
    except Exception:
        log.exception("Could not configure Twilio number webhook for %s", number)
    return False

def _twilio_voice_config():
    """API key + TwiML app used for browser calling.

    Environment variables win. Otherwise the app creates them once through the
    Twilio API and stores them in voxsbridge_settings.
    """
    c = tw_conf()
    pre = c["settings"]          # twilio_ (account 1, same keys as before) / twilio2_
    # Saved keys belong to one Twilio account; if the account SID changed, make new ones.
    saved_sid = get_setting(pre + "account_sid", "")
    if saved_sid != c["sid"]:
        if saved_sid:
            for k in ("api_key_sid", "api_key_secret", "twiml_app_sid"):
                set_setting(pre + k, "")
        set_setting(pre + "account_sid", c["sid"])
    key_sid = c["api_key_sid"] or get_setting(pre + "api_key_sid", "")
    key_secret = c["api_key_secret"] or get_setting(pre + "api_key_secret", "")
    app_sid = c["app_sid"] or get_setting(pre + "twiml_app_sid", "")
    if key_sid and key_secret and app_sid:
        return key_sid, key_secret, app_sid
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("voxsbridge-twilio-setup-" + pre,))
            key_sid = key_sid or get_setting(pre + "api_key_sid", "")
            key_secret = key_secret or get_setting(pre + "api_key_secret", "")
            app_sid = app_sid or get_setting(pre + "twiml_app_sid", "")
            if not (key_sid and key_secret):
                created = _twilio_request("POST", "/Keys.json", {"FriendlyName": "VoxsBridge Web Dialer"})
                key_sid, key_secret = created.get("sid", ""), created.get("secret", "")
                if not (key_sid and key_secret):
                    raise HTTPException(status_code=502, detail="Twilio API key creation failed")
                set_setting(pre + "api_key_sid", key_sid)
                set_setting(pre + "api_key_secret", key_secret)
            if not app_sid:
                created = _twilio_request("POST", "/Applications.json", {
                    "FriendlyName": "VoxsBridge Web Dialer",
                    "VoiceUrl": PUBLIC_BASE_URL + "/twilio/voice",
                    "VoiceMethod": "POST",
                })
                app_sid = created.get("sid", "")
                if not app_sid:
                    raise HTTPException(status_code=502, detail="Twilio TwiML app creation failed")
                set_setting(pre + "twiml_app_sid", app_sid)
    return key_sid, key_secret, app_sid


def number_assignments(number):
    """Database is authoritative, including inactive users and stale user-number rows."""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT u.id,u.email,u.name
                FROM public.voxsbridge_users u
                LEFT JOIN public.caller_id_assignments a ON a.user_id=u.id
                WHERE a.phone_number=%s OR u.phone_number=%s
                ORDER BY u.name""", (number, number))
            return [dict(r) for r in cur.fetchall()]


@app.get("/api/admin/caller-ids")
async def admin_caller_ids(request: Request):
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    provider = request.query_params.get("provider", "telnyx")
    if provider not in PROVIDERS:
        raise HTTPException(status_code=422, detail="Unknown provider")
    numbers = get_provider_numbers(provider)
    # Fetch all assignments in one database roundtrip, not from cached users_db.
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT u.id,u.email,u.name,u.account_id,a.phone_number
                FROM public.caller_id_assignments a
                JOIN public.voxsbridge_users u ON u.id=a.user_id
                WHERE a.phone_number = ANY(%s)
                UNION
                SELECT DISTINCT u.id,u.email,u.name,u.account_id,u.phone_number
                FROM public.voxsbridge_users u
                WHERE u.phone_number = ANY(%s)""", (numbers, numbers))
            assigned = {}
            for r in cur.fetchall():
                assigned.setdefault(r['phone_number'], []).append({
                    'email': r['email'], 'name': r['name'] or r['email'],
                    'account_id': str(r['account_id']) if r['account_id'] else '',
                    'last_name': (r['name'] or '').strip().split()[-1] if (r['name'] or '').strip() else 'Unknown'
                })
    if provider == "twilio":
        owners, labels = twilio_number_accounts(), {k: v["label"] for k, v in TWILIO_ACCOUNTS.items()}
    else:
        owners, labels = telnyx_number_accounts(), {k: v["label"] for k, v in TELNYX_ACCOUNTS.items()}
    only = request.query_params.get("account", "")
    held = _wo["hold_info"](numbers)       # reserved for a website buyer: not offered here
    return JSONResponse({'numbers': [
        {'number': n, 'assigned_to': assigned.get(n, []),
         'account': owners.get(n, ''), 'account_label': labels.get(owners.get(n, ''), '')}
        for n in numbers if (not only or owners.get(n) == only) and n not in held
    ]}, headers={'Cache-Control': 'no-store'})


@app.get("/api/admin/lines")
async def admin_lines(request: Request):
    """Every calling account (Telnyx 1/2, Twilio 1/2) and whether it is connected.
    Keys are never returned, only yes/no and the number count."""
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    lines = []
    tx_map, tw_map, tx_err, tw_err = {}, {}, "", ""
    try:
        tx_map = telnyx_number_accounts(fresh=True)
    except HTTPException as exc:
        tx_err = str(exc.detail)
    try:
        tw_map = twilio_number_accounts(fresh=True) if twilio_enabled() else {}
    except HTTPException as exc:
        tw_err = str(exc.detail)
    for key, label, env in (("t1", "Telnyx 1", "TELNYX_"), ("t2", "Telnyx 2", "TELNYX2_")):
        conf = TELNYX_ACCOUNTS.get(key)
        ok = bool(conf and conf["api_key"])
        lines.append({"value": "telnyx:" + key, "provider": "telnyx", "account": key, "label": label,
                      "configured": ok, "numbers": sum(1 for a in tx_map.values() if a == key),
                      "env": env, "error": tx_err if ok and key == "t1" else ""})
    for key, label, env in (("w1", "Twilio 1", "TWILIO_"), ("w2", "Twilio 2", "TWILIO2_")):
        conf = TWILIO_ACCOUNTS.get(key)
        ok = bool(conf and conf["sid"] and conf["token"])
        lines.append({"value": "twilio:" + key, "provider": "twilio", "account": key, "label": label,
                      "configured": ok, "numbers": sum(1 for a in tw_map.values() if a == key),
                      "env": env, "error": tw_err if ok else ""})
    return JSONResponse({"lines": lines}, headers={"Cache-Control": "no-store"})


# Admin-only customer management and invoice features.
def require_admin(request):
    actor = get_current_user(request)
    if not actor or actor.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='Admin access required')
    return actor


def _customer_data(email):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT u.id,u.name,u.email,u.phone_number,u.provider,u.telnyx_account,u.twilio_account,u.account_id,u.plan_name,
                u.plan_start,u.plan_renewal,u.created_at,u.is_active,u.is_demo,
                u.last_paid_amount,u.recording_premium,u.contact_name,u.contact_phone,
                u.contact_address,u.customer_notes,
                (SELECT COALESCE(sum(p.amount_pkr),0) FROM public.payments p WHERE p.user_id=u.id AND p.status='paid') AS collected,
                (SELECT COALESCE(sum(p.amount_usd),0) FROM public.payments p WHERE p.user_id=u.id AND p.status='paid') AS collected_usd,
                (SELECT p.amount_usd FROM public.payments p WHERE p.user_id=u.id AND p.status='paid' ORDER BY p.paid_at DESC NULLS LAST LIMIT 1) AS last_payment_usd,
                u.sms_credits,
                (SELECT max(p.paid_at) FROM public.payments p WHERE p.user_id=u.id AND p.status='paid') AS last_payment_at
                FROM public.voxsbridge_users u WHERE lower(u.email)=lower(%s)""", (email,))
            row=cur.fetchone()
            if not row: raise HTTPException(404, 'Customer not found')
            data=dict(row)
            # Usage counts the customer's own plan cycle (renewal - 30 days .. renewal),
            # so it resets on renewal and not on the 1st of the calendar month.
            start,end=plan_cycle(data.get('plan_renewal'))
            cur.execute("SELECT count(*) AS n, COALESCE(sum(duration_seconds),0) AS secs "
                        "FROM public.calls WHERE user_id=%s AND created_at >= %s",(data['id'],start))
            u=cur.fetchone()
            data['calls_month']=int(u['n'] or 0)
            data['talk_seconds']=int(u['secs'] or 0)
            data['cycle_start']=start.date().isoformat()
            data['cycle_end']=end.date().isoformat()
            return data


@app.get('/customers', response_class=HTMLResponse)
async def customers_page(request: Request):
    admin=require_admin(request)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT u.name,u.email,u.plan_name,u.phone_number,u.last_paid_amount,
                u.plan_renewal,u.is_active,u.is_demo,u.created_at,u.account_role,
                acc.id AS account_id, acc.name AS account_name,
                (SELECT count(*) FROM public.caller_id_assignments a WHERE a.user_id=u.id) AS numbers
                FROM public.voxsbridge_users u
                LEFT JOIN public.voxsbridge_accounts acc ON acc.id=u.account_id
                WHERE u.role <> 'admin' AND u.user_type='customer'
                ORDER BY acc.name NULLS LAST, u.account_role DESC, u.created_at DESC""")
            customers=cur.fetchall()
    return templates.TemplateResponse(request=request,name='customers.html',context={'user':admin,'customers':customers,'today':date.today()})


@app.get('/customers/{email}', response_class=HTMLResponse)
async def customer_detail(request:Request,email:str):
    admin=require_admin(request)
    c=_customer_data(email)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT ctid::text AS pid,amount_pkr,amount_usd,method,reference,status,paid_at,renewal_date FROM public.payments WHERE user_id=%s ORDER BY paid_at DESC NULLS LAST LIMIT 100',(c['id'],))
            payments=cur.fetchall()
    try:
        costs = cost_summary(c['id'], month_start=plan_cycle(c.get('plan_renewal'))[0])
    except Exception:
        log.exception("Cost summary failed for customer")
        costs = None
    return templates.TemplateResponse(request=request,name='customer_detail.html',context={
        'user':admin,'customer':c,'payments':payments,'costs':costs,
        'payments_json':json.dumps([{'pid':r['pid'],'pkr':float(r['amount_pkr'] or 0),'usd':float(r['amount_usd'] or 0),
                                    'method':r['method'] or '','reference':r['reference'] or ''} for r in payments]).replace('<','\\u003c'),
        'twilio_ready':twilio_enabled()})


@app.post('/customers/{email}/update')
async def customer_update(request:Request,email:str,name:str=Form(...),plan_name:str=Form(''),
    plan_renewal:str=Form(''),contact_name:str=Form(''),contact_phone:str=Form(''),
    contact_address:str=Form(''),customer_notes:str=Form(''),is_active:str=Form('true'),
    new_password:str=Form(''),monthly_amount:int=Form(0)):
    require_admin(request)
    if monthly_amount<0: raise HTTPException(422,'Amount must not be negative')
    if plan_renewal:
        try: date.fromisoformat(plan_renewal)
        except ValueError: raise HTTPException(422,'Invalid renewal date')
    if new_password and len(new_password)<12: raise HTTPException(422,'Password must be at least 12 characters')
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.voxsbridge_users SET name=%s,plan_name=%s,
                plan_renewal=%s::date,contact_name=%s,contact_phone=%s,contact_address=%s,
                customer_notes=%s,is_active=%s,last_paid_amount=%s
                WHERE lower(email)=lower(%s) AND role<>'admin' RETURNING id""",
                (name.strip()[:200],plan_name.strip()[:120],plan_renewal or None,contact_name.strip()[:200],
                contact_phone.strip()[:60],contact_address.strip()[:500],customer_notes.strip()[:3000],
                is_active=='true',monthly_amount,email))
            row=cur.fetchone()
            if not row: raise HTTPException(404,'Customer not found')
            if new_password:
                cur.execute('UPDATE public.voxsbridge_users SET password_hash=%s WHERE id=%s',(hash_password(new_password),row['id']))
                # Password reset by admin: old logins stop working.
                cur.execute('UPDATE public.user_sessions SET revoked_at=NOW() WHERE user_id=%s AND revoked_at IS NULL',(row['id'],))
            if is_active!='true':
                cur.execute('UPDATE public.user_sessions SET revoked_at=NOW() WHERE user_id=%s AND revoked_at IS NULL',(row['id'],))
    refresh_users()
    return RedirectResponse('/customers/'+email+'?saved=1',status_code=303)


@app.post('/customers/{email}/change-number')
async def customer_change_number(request: Request, email: str, phone_number: str = Form(...),
                                 provider: str = Form("telnyx")):
    require_admin(request)
    from urllib.parse import quote
    new_number = phone_number.strip()
    if provider not in PROVIDERS:
        raise HTTPException(422, 'Unknown provider')
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", new_number):
        raise HTTPException(422, 'Invalid E.164 phone number')
    # Verify ownership with the provider before changing the database.
    if new_number not in get_provider_numbers(provider):
        raise HTTPException(422, f'Number is not owned by this {provider.capitalize()} account')
    with db_connect() as conn:
        with conn.cursor() as cur:
            # Serialize changes to this user and the requested number.
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('caller-id:' + new_number,))
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('user-number:' + email.lower(),))
            cur.execute("SELECT id,phone_number,account_id FROM public.voxsbridge_users WHERE lower(email)=lower(%s) AND role <> 'admin' FOR UPDATE", (email,))
            target = cur.fetchone()
            if not target:
                raise HTTPException(404, 'Customer not found')
            cur.execute("""SELECT a.user_id, u.account_id FROM public.caller_id_assignments a
                JOIN public.voxsbridge_users u ON u.id=a.user_id
                WHERE a.phone_number=%s AND a.user_id<>%s""", (new_number,target['id']))
            others = cur.fetchall()
            # A number can be SHARED by users of the same company (all of them ring).
            if others and (not target['account_id'] or any(o['account_id'] != target['account_id'] for o in others)):
                raise HTTPException(409, 'This number belongs to a user of another company')
            # Remove old assignment and insert new assignment in one transaction.
            cur.execute('DELETE FROM public.caller_id_assignments WHERE user_id=%s', (target['id'],))
            cur.execute('INSERT INTO public.caller_id_assignments(user_id,phone_number) VALUES (%s,%s)', (target['id'],new_number))
            cur.execute('UPDATE public.voxsbridge_users SET phone_number=%s,provider=%s WHERE id=%s',
                        (new_number,provider,target['id']))
    if provider == 'telnyx':
        _set_user_telnyx_account(target['id'], telnyx_number_accounts().get(new_number, 't1'))
    else:
        _set_user_twilio_account(target['id'], new_number)
    refresh_users()
    if provider == 'twilio':
        _twilio_configure_number(new_number)
    return RedirectResponse('/customers/' + quote(email, safe='') + '?number_updated=1', status_code=303)


@app.post('/customers/{email}/payment')
async def customer_payment(request:Request,email:str,amount:int=Form(0),amount_usd:float=Form(0),method:str=Form('Manual'),reference:str=Form('')):
    require_admin(request)
    if amount<0 or amount_usd<0 or (amount<=0 and amount_usd<=0): raise HTTPException(422,'Enter an amount in PKR or USD')
    c=_customer_data(email)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.payments(user_id,amount_pkr,status,paid_at,renewal_date,amount_usd,method,reference)
                VALUES(%s,%s,'paid',NOW(),%s,%s,%s,%s)""",(c['id'],amount,c['plan_renewal'],round(amount_usd,2) or None,method.strip()[:40],reference.strip()[:120]))
            if amount>0: cur.execute('UPDATE public.voxsbridge_users SET last_paid_amount=%s WHERE id=%s',(amount,c['id']))
    refresh_users()
    return RedirectResponse('/customers/'+email+'?payment=1',status_code=303)


def _sync_last_paid(cur,user_id):
    """last_paid_amount (the customer's 'monthly revenue') follows the newest PKR payment."""
    cur.execute("""SELECT COALESCE(NULLIF(amount_pkr,0),0) AS a FROM public.payments
        WHERE user_id=%s AND status='paid' AND COALESCE(amount_pkr,0)>0
        ORDER BY paid_at DESC NULLS LAST LIMIT 1""",(user_id,))
    r=cur.fetchone()
    if r: cur.execute('UPDATE public.voxsbridge_users SET last_paid_amount=%s WHERE id=%s',(r['a'],user_id))

@app.post('/customers/{email}/payment/edit')
async def customer_payment_edit(request:Request,email:str,pid:str=Form(...),old_pkr:float=Form(0),
    amount:int=Form(0),amount_usd:float=Form(0),method:str=Form(''),reference:str=Form('')):
    require_admin(request)
    if amount<0 or amount_usd<0 or (amount<=0 and amount_usd<=0):
        raise HTTPException(422,'Enter an amount in PKR or USD')
    if not re.fullmatch(r'\(\d+,\d+\)',pid): raise HTTPException(422,'Invalid payment')
    c=_customer_data(email)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.payments SET amount_pkr=%s,amount_usd=%s,method=%s,reference=%s
                WHERE ctid=%s::tid AND user_id=%s AND COALESCE(amount_pkr,0)=%s RETURNING 1""",
                (amount,round(amount_usd,2) or None,method.strip()[:40],reference.strip()[:120],pid,c['id'],old_pkr))
            if not cur.fetchone(): raise HTTPException(409,'Payment changed or not found. Reload the page and try again.')
            _sync_last_paid(cur,c['id'])
    refresh_users()
    return RedirectResponse('/customers/'+email+'?saved=1',status_code=303)

@app.post('/customers/{email}/payment/delete')
async def customer_payment_delete(request:Request,email:str,pid:str=Form(...),old_pkr:float=Form(0)):
    require_admin(request)
    if not re.fullmatch(r'\(\d+,\d+\)',pid): raise HTTPException(422,'Invalid payment')
    c=_customer_data(email)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""DELETE FROM public.payments WHERE ctid=%s::tid AND user_id=%s
                AND COALESCE(amount_pkr,0)=%s RETURNING 1""",(pid,c['id'],old_pkr))
            if not cur.fetchone(): raise HTTPException(409,'Payment changed or not found. Reload the page and try again.')
            _sync_last_paid(cur,c['id'])
    refresh_users()
    return RedirectResponse('/customers/'+email+'?saved=1',status_code=303)


def _invoice_row(invoice_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM public.voxsbridge_invoices WHERE id=%s',(invoice_id,))
            row=cur.fetchone()
            if not row: raise HTTPException(404,'Invoice not found')
            cur.execute('SELECT * FROM public.voxsbridge_invoice_items WHERE invoice_id=%s ORDER BY position',(invoice_id,))
            return dict(row),cur.fetchall()


@app.get('/invoices',response_class=HTMLResponse)
async def invoices_page(request:Request):
    admin=require_admin(request)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM public.voxsbridge_invoices ORDER BY created_at DESC LIMIT 300')
            invoices=cur.fetchall()
    return templates.TemplateResponse(request=request,name='invoices.html',context={'user':admin,'invoices':invoices})


@app.get('/invoices/new',response_class=HTMLResponse)
async def invoice_new(request:Request,customer:str=''):
    admin=require_admin(request)
    c=_customer_data(customer) if customer else None
    return templates.TemplateResponse(request=request,name='invoice_form.html',context={'user':admin,'customer':c,'invoice':None,'items':[]})


@app.post('/invoices/new')
async def invoice_create(request:Request,customer_email:str=Form(...),issue_date:str=Form(...),due_date:str=Form(...),
    description:List[str]=Form(...),qty:List[int]=Form(...),unit_price:List[int]=Form(...),
    discount:int=Form(0),tax_percent:int=Form(0),notes:str=Form(''),terms:str=Form('Payment is due by the due date shown.'),
    action:str=Form('save'),payment_method:str=Form('bank'),card_name:str=Form(''),renewal_date:str=Form(''),paid_pkr:int=Form(0)):
    require_admin(request)
    if payment_method not in ('bank','card','usdt','other'): payment_method='bank'
    paid_pkr=max(0,paid_pkr)
    try: renewal=date.fromisoformat(renewal_date) if renewal_date else None
    except ValueError: raise HTTPException(422,'Invalid renewal date')
    c=_customer_data(customer_email)
    if not description or len(description)>30 or len(description)!=len(qty) or len(qty)!=len(unit_price):
        raise HTTPException(422,'Invalid invoice items')
    try:
        issued=date.fromisoformat(issue_date); due=date.fromisoformat(due_date)
    except ValueError: raise HTTPException(422,'Invalid dates')
    if any(q<=0 or q>100000 or price<0 or price>100000000 for q,price in zip(qty,unit_price)) or discount<0 or not 0<=tax_percent<=100:
        raise HTTPException(422,'Invalid quantities, discount or tax')
    subtotal=sum(q*p for q,p in zip(qty,unit_price))
    taxable=max(0,subtotal-discount)
    tax_amount=round(taxable*tax_percent/100)
    total=taxable+tax_amount
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT nextval('public.voxsbridge_invoice_seq') AS n")
            number=f'INV-{issued.year}-{cur.fetchone()["n"]:04d}'
            cur.execute("""INSERT INTO public.voxsbridge_invoices
                (invoice_number,customer_id,customer_name,customer_email,contact_phone,contact_address,
                 issue_date,due_date,notes,terms,subtotal_pkr,discount_pkr,tax_percent,tax_pkr,total_pkr,status,
                 payment_method,card_name,renewal_date,paid_pkr)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s,%s,%s,%s) RETURNING id""",
                (number,c['id'],c['name'],c['email'],c.get('contact_phone') or '',c.get('contact_address') or '',
                issued,due,notes[:3000],terms[:3000],subtotal,discount,tax_percent,tax_amount,total,
                payment_method,card_name.strip()[:60],renewal,min(paid_pkr,total)))
            invoice_id=cur.fetchone()['id']
            for i,(d,q,price) in enumerate(zip(description,qty,unit_price)):
                cur.execute("""INSERT INTO public.voxsbridge_invoice_items(invoice_id,position,description,qty,unit_price_pkr,amount_pkr)
                    VALUES(%s,%s,%s,%s,%s,%s)""",(invoice_id,i,d.strip()[:500],q,price,q*price))
    return RedirectResponse('/invoices/'+str(invoice_id),status_code=303)


@app.get('/invoices/{invoice_id}',response_class=HTMLResponse)
async def invoice_detail(request:Request,invoice_id:uuid.UUID):
    admin=require_admin(request)
    invoice,items=_invoice_row(invoice_id)
    import invoice_pdf as ipdf
    return templates.TemplateResponse(request=request,name='invoice_detail.html',context={'user':admin,'invoice':invoice,'items':items,'bank':ipdf.bank_details()})


@app.post('/invoices/{invoice_id}/status')
async def invoice_status(request:Request,invoice_id:uuid.UUID,status:str=Form(...)):
    require_admin(request)
    if status not in ('draft','sent','paid','void'): raise HTTPException(422,'Invalid invoice status')
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE public.voxsbridge_invoices SET status=%s,updated_at=NOW() WHERE id=%s RETURNING id',(status,invoice_id))
            if not cur.fetchone(): raise HTTPException(404,'Invoice not found')
    # Invoice status is not itself a cash receipt: avoid double counting payments.
    return RedirectResponse('/invoices/'+str(invoice_id),status_code=303)


@app.post('/invoices/{invoice_id}/method')
async def invoice_method(request:Request,invoice_id:uuid.UUID,payment_method:str=Form('bank'),card_name:str=Form(''),renewal_date:str=Form(''),paid_pkr:int=Form(0)):
    require_admin(request)
    if payment_method not in ('bank','card','usdt','other'): raise HTTPException(422,'Invalid payment method')
    try: renewal=date.fromisoformat(renewal_date) if renewal_date else None
    except ValueError: raise HTTPException(422,'Invalid renewal date')
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE public.voxsbridge_invoices SET payment_method=%s,card_name=%s,renewal_date=%s,paid_pkr=LEAST(GREATEST(%s,0),total_pkr),updated_at=NOW() WHERE id=%s RETURNING id',
                (payment_method,card_name.strip()[:60],renewal,paid_pkr,invoice_id))
            if not cur.fetchone(): raise HTTPException(404,'Invoice not found')
    return RedirectResponse('/invoices/'+str(invoice_id),status_code=303)


@app.post('/invoices/{invoice_id}/delete')
async def invoice_delete(request:Request,invoice_id:uuid.UUID):
    require_admin(request)
    with db_connect() as conn:
        with conn.cursor() as cur:
            # Any invoice can be deleted (items go with it). Payments recorded on the
            # customer page are separate and are not touched.
            cur.execute("DELETE FROM public.voxsbridge_invoice_items WHERE invoice_id=%s",(invoice_id,))
            cur.execute("DELETE FROM public.voxsbridge_invoices WHERE id=%s",(invoice_id,))
            if cur.rowcount==0: raise HTTPException(404,'Invoice not found')
    return RedirectResponse('/invoices',status_code=303)


@app.post('/invoices/{invoice_id}/duplicate')
async def invoice_duplicate(request:Request,invoice_id:uuid.UUID):
    require_admin(request)
    inv,items=_invoice_row(invoice_id)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT nextval('public.voxsbridge_invoice_seq') AS n")
            number=f'INV-{date.today().year}-{cur.fetchone()["n"]:04d}'
            cur.execute("""INSERT INTO public.voxsbridge_invoices
                (invoice_number,customer_id,customer_name,customer_email,contact_phone,contact_address,issue_date,due_date,
                 notes,terms,subtotal_pkr,discount_pkr,tax_percent,tax_pkr,total_pkr,status,payment_method,card_name)
                VALUES(%s,%s,%s,%s,%s,%s,CURRENT_DATE,CURRENT_DATE+INTERVAL '3 days',%s,%s,%s,%s,%s,%s,%s,'draft',%s,%s) RETURNING id""",
                (number,inv['customer_id'],inv['customer_name'],inv['customer_email'],inv['contact_phone'],inv['contact_address'],
                inv['notes'],inv['terms'],inv['subtotal_pkr'],inv['discount_pkr'],inv['tax_percent'],inv['tax_pkr'],inv['total_pkr'],
                inv.get('payment_method') or 'bank',inv.get('card_name') or ''))
            new_id=cur.fetchone()['id']
            for item in items:
                cur.execute("""INSERT INTO public.voxsbridge_invoice_items(invoice_id,position,description,qty,unit_price_pkr,amount_pkr)
                    VALUES(%s,%s,%s,%s,%s,%s)""",(new_id,item['position'],item['description'],item['qty'],item['unit_price_pkr'],item['amount_pkr']))
    return RedirectResponse('/invoices/'+str(new_id),status_code=303)


@app.get('/invoices/{invoice_id}/pdf')
async def invoice_pdf(request:Request,invoice_id:uuid.UUID):
    require_admin(request)
    inv,items=_invoice_row(invoice_id)
    import invoice_pdf as ipdf
    logo=os.path.join(FRONTEND_DIR,'static','img','voxsbridge-logo-dark.png')
    data=ipdf.build_invoice_pdf(inv,[dict(i) for i in items],logo)
    return Response(data,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="{inv["invoice_number"]}.pdf"','Cache-Control':'private, no-store'})

def _disabled_users():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT name,email,plan_name,plan_renewal,created_at FROM public.voxsbridge_users
                WHERE is_active=FALSE AND role<>'admin' AND signup_status<>'pending' ORDER BY created_at DESC LIMIT 500""")
            return cur.fetchall()

def _users_page(request, mode):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse(request=request, name="users.html", context={
        "user": user, "mode": mode,
        "users": list(users_db.values()),
        "companies": _all_companies(),
        "resellers": _all_resellers(),
        "error": request.query_params.get("error"),
        "created": request.query_params.get("created"),
        "deleted": request.query_params.get("deleted"),
        "purged": request.query_params.get("purged"),
        "disabled_users": _disabled_users(),
    })

@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    """User Accounts: the list of every login."""
    return _users_page(request, "list")

@app.get("/users/new", response_class=HTMLResponse)
async def users_new_page(request: Request):
    """Create User: the form only."""
    return _users_page(request, "create")

@app.post("/users")
async def create_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    phone_number: str = Form(""),
    plan_name: str = Form("1 USER ACCOUNT"),
    plan_start: str = Form(""),
    plan_renewal: str = Form(""),
    is_demo: bool = Form(False),
    provider: str = Form("telnyx"),
    user_type: str = Form("customer"),
    company_mode: str = Form("new"),
    company_name: str = Form(""),
    account_id: str = Form(""),
    account_role: str = Form("owner"),
    reseller_id: str = Form(""),
):
    from urllib.parse import urlencode
    from psycopg.errors import UniqueViolation

    admin = get_current_user(request)
    if not admin or admin.get('role') != 'admin':
        return RedirectResponse('/login', status_code=302)

    def fail(message):
        return RedirectResponse('/users/new?' + urlencode({'error': message}), status_code=303)

    email = email.strip().lower()
    name = name.strip()
    phone_number = phone_number.strip()
    user_type = 'reseller' if user_type == 'reseller' else 'customer'
    account_role = 'owner' if account_role == 'owner' else 'agent'
    if not name or not email or '@' not in email:
        return fail('Valid name and email required')
    if len(password) < 12:
        return fail('Password must be at least 12 characters')
    if user_type == 'reseller':
        phone_number = ''          # resellers only view their clients
    if phone_number and not re.fullmatch(r'\+[1-9][0-9]{7,14}', phone_number):
        return fail('Valid international Caller ID required')
    if user_type == 'customer' and account_role == 'agent' and not phone_number:
        return fail('Agents need a Caller ID to make calls')
    if provider not in PROVIDERS:
        return fail('Select a calling provider')
    if phone_number:
        provider_label = provider.capitalize()
        try:
            owned_numbers = get_provider_numbers(provider)
        except HTTPException:
            return fail(f'Unable to verify {provider_label} numbers')
        if phone_number not in owned_numbers:
            return fail(f'Caller ID not in {provider_label} account')
        if phone_number in _wo["hold_info"]([phone_number]):
            return fail('This number is reserved for a website order right now')

    form = await request.form()
    confirmed_transfer = form.get('confirm_shared_number') == 'yes'
    try:
        amount_paid = int(form.get('amount_paid', '0') or 0)
        if amount_paid < 0:
            raise ValueError()
    except (ValueError, TypeError):
        return fail('Invalid PKR amount')
    try:
        amount_usd = round(float(str(form.get('amount_usd', '') or '0').replace(',', '')), 2)
        if amount_usd < 0:
            raise ValueError()
    except ValueError:
        return fail('Invalid USD amount')
    payment_status = str(form.get('payment_status', 'paid'))
    if payment_status not in ('paid', 'partial', 'unpaid'):
        payment_status = 'unpaid'
    if is_demo or payment_status == 'unpaid' or user_type == 'reseller':
        amount_paid = 0
        amount_usd = 0
        payment_status = 'unpaid'
    try:
        start = date.fromisoformat(plan_start) if plan_start else date.today()
        renewal = date.fromisoformat(plan_renewal) if plan_renewal else date.today() + timedelta(days=30)
    except ValueError:
        return fail('Invalid plan date')
    existing_account = None
    if user_type == 'customer' and company_mode == 'existing':
        try:
            existing_account = uuid.UUID(account_id)
        except ValueError:
            return fail('Select a company')
    reseller_uuid = None
    if user_type == 'customer' and company_mode != 'existing' and reseller_id:
        try:
            reseller_uuid = uuid.UUID(reseller_id)
        except ValueError:
            return fail('Invalid reseller')

    # Ownership check, optional transfer and new account creation are atomic.
    # The advisory lock serializes concurrent assignments of the same number.
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                if phone_number:
                    cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('caller-id:' + phone_number,))
                cur.execute('SELECT id FROM public.voxsbridge_users WHERE lower(email)=lower(%s)', (email,))
                if cur.fetchone():
                    return fail('Email already exists')
                if phone_number:
                    cur.execute("""SELECT u.id FROM public.voxsbridge_users u
                        LEFT JOIN public.caller_id_assignments a ON a.user_id=u.id
                        WHERE a.phone_number=%s OR u.phone_number=%s
                        FOR UPDATE OF u""", (phone_number, phone_number))
                    owner_rows = cur.fetchall()
                    owners = [r['id'] for r in owner_rows]
                    share = form.get('share_number') == 'yes'
                    if owners and share:
                        # Share inside one company: every holder keeps the number and rings.
                        cur.execute("SELECT DISTINCT account_id FROM public.voxsbridge_users WHERE id=ANY(%s)", (owners,))
                        accts = {r['account_id'] for r in cur.fetchall()}
                        if not existing_account or accts != {existing_account}:
                            return fail('A number can only be shared inside the same company')
                        owners = []
                    if owners and not confirmed_transfer:
                        return fail('Caller ID already assigned; confirm transfer first')
                    if owners:
                        cur.execute('UPDATE public.voxsbridge_users SET phone_number=NULL WHERE id=ANY(%s)', (owners,))
                        cur.execute('DELETE FROM public.caller_id_assignments WHERE phone_number=%s', (phone_number,))
                new_account = None
                if user_type == 'customer':
                    if existing_account:
                        cur.execute('SELECT id FROM public.voxsbridge_accounts WHERE id=%s', (existing_account,))
                        if not cur.fetchone():
                            return fail('Company not found')
                        new_account = existing_account
                    else:
                        if reseller_uuid:
                            cur.execute("SELECT 1 FROM public.voxsbridge_users WHERE id=%s AND user_type='reseller'", (reseller_uuid,))
                            if not cur.fetchone():
                                return fail('Reseller not found')
                        cur.execute("""INSERT INTO public.voxsbridge_accounts(name, reseller_id)
                            VALUES (%s,%s) RETURNING id""", ((company_name.strip() or name)[:200], reseller_uuid))
                        new_account = cur.fetchone()['id']
                        account_role = 'owner'          # first user of a new company owns it
                cur.execute("""INSERT INTO public.voxsbridge_users
                    (name,email,password_hash,role,phone_number,credential_id,sip_username,
                     plan_name,plan_start,plan_renewal,last_paid_amount,recording_premium,dnd_enabled,is_demo,provider,
                     account_id,account_role,user_type)
                    VALUES (%s,%s,%s,'agent',%s,NULL,NULL,%s,%s,%s,%s,FALSE,FALSE,%s,%s,%s,%s,%s)
                    RETURNING id""", (name,email,hash_password(password),phone_number or None,
                    (plan_name or '1 USER ACCOUNT').strip(),start,renewal,amount_paid,is_demo,provider,
                    new_account, account_role if user_type == 'customer' else 'agent', user_type))
                new_id = cur.fetchone()['id']
                if phone_number:
                    cur.execute('INSERT INTO public.caller_id_assignments(user_id,phone_number) VALUES (%s,%s)',
                                (new_id,phone_number))
                if amount_paid or amount_usd:
                    cur.execute("""INSERT INTO public.payments(user_id,amount_pkr,status,paid_at,renewal_date,amount_usd,method)
                        VALUES (%s,%s,%s,%s,%s,%s,'manual')""", (new_id,amount_paid,payment_status,
                        datetime.now() if payment_status == 'paid' else None,renewal,amount_usd or None))
    except UniqueViolation:
        return fail('Email or Caller ID already assigned; refresh and retry')

    acct = 't1'
    if phone_number and provider == 'telnyx':
        acct = telnyx_number_accounts().get(phone_number, 't1')
        _set_user_telnyx_account(new_id, acct)
    elif phone_number and provider == 'twilio':
        _set_user_twilio_account(new_id, phone_number)
    refresh_users()
    if phone_number:
        if provider == 'twilio':
            _twilio_configure_number(phone_number)
        else:
            try:
                with tx_ctx(acct):
                    _ensure_credential(email)
            except Exception:
                log.exception('Telnyx credential creation failed for new user')
    return RedirectResponse('/users?created=new', status_code=303)


@app.post("/users/edit")
async def edit_user_details(request: Request, email: str = Form(...), name: str = Form(...), new_email: str = Form(...), plan_name: str = Form(...), plan_renewal: str = Form(...), account_active: str = Form("no")):
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    old_email = email.strip().lower()
    new_email = new_email.strip().lower()
    name = name.strip()
    if not name or not new_email or "@" not in new_email:
        return RedirectResponse("/users?error=Invalid+name+or+email", status_code=303)
    try:
        date.fromisoformat(plan_renewal)
    except ValueError:
        return RedirectResponse("/users?error=Invalid+renewal+date", status_code=303)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,role FROM public.voxsbridge_users WHERE email=%s AND is_active=TRUE FOR UPDATE", (old_email,))
            target = cur.fetchone()
            if not target or target["role"] == "admin":
                return RedirectResponse("/users?error=Protected+or+missing+user", status_code=303)
            cur.execute("SELECT 1 FROM public.voxsbridge_users WHERE email=%s AND id<>%s", (new_email,target["id"]))
            if cur.fetchone():
                return RedirectResponse("/users?error=Email+already+exists", status_code=303)
            # Disabling account is handled by the existing Delete/Disable action;
            # this edit route only updates profile and subscription fields.
            cur.execute("""UPDATE public.voxsbridge_users SET name=%s,email=%s,plan_name=%s,plan_renewal=%s::date
                WHERE id=%s""", (name,new_email,plan_name.strip(),plan_renewal,target["id"]))
    refresh_users()
    return RedirectResponse("/users?updated=1", status_code=303)

@app.post("/users/save")
async def save_user_all(request: Request):
    """One Save for the edit popup: profile, plan, add-on check boxes, SMS allowance and countries."""
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    f = await request.form()
    old_email = str(f.get("email") or "").strip().lower()
    new_email = str(f.get("new_email") or "").strip().lower()
    name = str(f.get("name") or "").strip()[:200]
    plan_name = str(f.get("plan_name") or "").strip()[:120]
    renewal = str(f.get("plan_renewal") or "").strip()
    def bounce(msg):
        return RedirectResponse("/users?error=" + urllib.parse.quote(msg), status_code=303)
    if not name or "@" not in new_email:
        return bounce("Invalid name or email")
    try:
        date.fromisoformat(renewal)
    except ValueError:
        return bounce("Invalid renewal date")
    credits_raw = str(f.get("credits") or "").strip()
    credits = None
    if credits_raw:
        # Any whole number: credits go down by one per SMS segment, so the saved
        # value is often not a round number.
        if not credits_raw.isdigit() or int(credits_raw) > 1000000:
            return bounce("SMS allowance must be a whole number (0 - 1,000,000) or blank for unlimited")
        credits = int(credits_raw)
    zones = ",".join(z for z in CALL_ZONES if z in f.getlist("zones"))
    want_rec, want_power = bool(f.get("recording")), bool(f.get("power"))
    want_sms = bool(f.get("sms"))
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,role,sms_enabled FROM public.voxsbridge_users
                WHERE email=%s AND is_active=TRUE FOR UPDATE""", (old_email,))
            target = cur.fetchone()
            if not target or target["role"] == "admin":
                return bounce("Protected or missing user")
            cur.execute("SELECT 1 FROM public.voxsbridge_users WHERE email=%s AND id<>%s", (new_email, target["id"]))
            if cur.fetchone():
                return bounce("Email already exists")
            cur.execute("""UPDATE public.voxsbridge_users SET name=%s,email=%s,plan_name=%s,plan_renewal=%s::date,
                recording_premium=%s,power_enabled=%s,sms_credits=%s,call_countries=%s WHERE id=%s""",
                        (name, new_email, plan_name, renewal, want_rec, want_power, credits, zones,
                         target["id"]))
    refresh_users()
    problems = []
    # SMS talks to the phone provider, so it reuses the existing toggle handler.
    async def flip(handler, current, wanted):
        if bool(current) == wanted:
            return
        resp = await handler(request, email=new_email)
        loc = resp.headers.get("location", "") if resp is not None else ""
        if "error=" in loc:
            problems.append(urllib.parse.unquote(loc.split("error=", 1)[1].split("&")[0]).replace("+", " "))
    await flip(toggle_sms, target["sms_enabled"], want_sms)
    if problems:
        return bounce("Saved, but: " + " | ".join(problems))
    return RedirectResponse("/users?updated=1", status_code=303)

@app.post("/users/delete")
async def delete_user(request: Request, email: str = Form(...)):
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    email = email.strip().lower()
    if email == admin["email"]:
        return RedirectResponse(url="/users?error=Cannot+delete+your+own+account", status_code=303)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,role FROM public.voxsbridge_users WHERE email=%s AND is_active=TRUE FOR UPDATE", (email,))
            target = cur.fetchone()
            if not target or target["role"] == "admin":
                return RedirectResponse(url="/users?error=User+not+found+or+protected", status_code=303)
            cur.execute("UPDATE public.voxsbridge_users SET is_active=FALSE,phone_number=NULL WHERE id=%s", (target["id"],))
            cur.execute("DELETE FROM public.caller_id_assignments WHERE user_id=%s", (target["id"],))
            cur.execute("UPDATE public.user_sessions SET revoked_at=NOW() WHERE user_id=%s AND revoked_at IS NULL", (target["id"],))
    try:
        _terminate_user_inbound_calls(target["id"])
    except Exception:
        import logging
        logging.exception("Unable to terminate all active incoming calls on user deletion")
    refresh_users()
    return RedirectResponse(url="/users?deleted=1", status_code=303)

@app.post("/users/repayment")
async def record_repayment(request: Request, email: str = Form(...), amount: int = Form(0), amount_usd: float = Form(0), renewal_date: str = Form("")):
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    target = users_db.get(email.strip().lower())
    if not target: raise HTTPException(status_code=404, detail="User not found")
    if amount < 0 or amount_usd < 0 or (amount <= 0 and amount_usd <= 0):
        raise HTTPException(status_code=400, detail="Enter an amount in PKR or USD")
    if renewal_date:
        try: date.fromisoformat(renewal_date)
        except ValueError: raise HTTPException(status_code=400, detail="Invalid renewal date")
        target["plan_renewal"] = renewal_date
    else:
        old = date.fromisoformat(target.get("plan_renewal") or date.today().isoformat())
        target["plan_renewal"] = (max(old, date.today()) + timedelta(days=30)).isoformat()
    target.setdefault("payments", []).append({"amount": amount, "date": date.today().isoformat(), "type": "renewal"})
    target["last_paid_amount"] = amount
    target["payment_status"] = "paid"
    save_users()
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.payments(user_id,amount_pkr,status,paid_at,renewal_date,amount_usd,method)
                SELECT id,%s,'paid',NOW(),%s::date,%s,'manual' FROM public.voxsbridge_users WHERE email=%s""",
                (amount,target["plan_renewal"],round(amount_usd,2) or None,email.strip().lower()))
    return RedirectResponse(url="/dashboard?period=month", status_code=303)

@app.post("/users/plan")
async def update_user_plan(
    request: Request,
    email: str = Form(...),
    plan_name: str = Form(...),
    plan_start: str = Form(...),
    plan_renewal: str = Form(...),
):
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        return RedirectResponse(url="/login", status_code=302)
    target = users_db.get(email.strip().lower())
    if not target:
        return RedirectResponse(url="/users?error=User+not+found", status_code=302)
    target["plan_name"] = plan_name.strip() or "1 USER ACCOUNT"
    target["plan_start"] = plan_start
    target["plan_renewal"] = plan_renewal
    save_users()
    return RedirectResponse(url="/users?created=1", status_code=302)

@app.post("/users/extend")
async def extend_user_plan(request: Request, email: str = Form(...), days: int = Form(30)):
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        return RedirectResponse(url="/login", status_code=302)
    target = users_db.get(email.strip().lower())
    if not target:
        return RedirectResponse(url="/users?error=User+not+found", status_code=302)
    today = datetime.now().date()
    raw = target.get("plan_renewal") or today.isoformat()
    try:
        current = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        current = today
    base = current if current > today else today
    target["plan_renewal"] = (base + timedelta(days=max(1, int(days)))).isoformat()
    if not target.get("plan_start"):
        target["plan_start"] = today.isoformat()
    if not target.get("plan_name"):
        target["plan_name"] = "1 USER ACCOUNT"
    save_users()
    return RedirectResponse(url="/users?created=1", status_code=302)

@app.post("/users/recording")
async def toggle_recording(request: Request, email: str = Form(...)):
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        return RedirectResponse(url="/login", status_code=302)
    target = users_db.get(email.strip().lower())
    if not target:
        return RedirectResponse(url="/users?error=User+not+found", status_code=302)
    target["recording_premium"] = not bool(target.get("recording_premium"))
    save_users()
    return RedirectResponse(url="/users?created=1", status_code=302)

def has_recording(user):
    return user.get("role") == "admin" or bool(user.get("recording_premium"))

def has_power(user):
    """Power Dialer + Campaigns. Existing users keep it (column default TRUE); website orders get it from the add-on."""
    return bool(user and (user.get("role") == "admin" or user.get("power_enabled", True)))

def locked_page(request, user, feature):
    return templates.TemplateResponse(request=request, name="feature_locked.html",
                                      context={"user": user, "feature": feature})

def has_sms(user):
    """Text messages: only users with the SMS add-on switched on by admin, and a number of their own."""
    return bool(user and user.get("sms_enabled") and user.get("phone_number")
                and user.get("user_type") != "reseller" and (user.get("provider") or "telnyx") in ("telnyx", "twilio"))

def plan_status(user):
    if user.get("is_demo"):
        expires = user.get("trial_expires_at")
        remaining = max(0, int((datetime.fromisoformat(expires) - datetime.now().astimezone()).total_seconds())) if expires else 3600
        return {"plan_name":"1 HOUR DEMO", "plan_start":user.get("trial_started_at") or "First login", "plan_renewal":expires or "Not started", "days_left":0 if remaining > 0 else -1, "payment_open":False, "trial_seconds_left":remaining}
    start = user.get("plan_start") or (user.get("created_at") or "")[:10] or datetime.now().date().isoformat()
    renewal = user.get("plan_renewal") or (datetime.now() + timedelta(days=30)).date().isoformat()
    try:
        days = (datetime.strptime(renewal, "%Y-%m-%d").date() - datetime.now().date()).days
    except ValueError:
        days = 0
    return {
        "plan_name": user.get("plan_name") or "1 USER ACCOUNT",
        "plan_start": start,
        "plan_renewal": renewal,
        "days_left": days,
        "payment_open": days <= 3,
    }

templates.env.globals["has_recording"] = has_recording
templates.env.globals["has_power"] = has_power
templates.env.globals["support_whatsapp"] = os.getenv("SUPPORT_WHATSAPP", "447476629105").strip().lstrip("+")
def provider_label(u):
    """'Twilio', 'Telnyx', or 'Telnyx 1' / 'Telnyx 2' when two Telnyx accounts are set up."""
    if (u.get("provider") or "telnyx") == "twilio":
        return tw_conf(u.get("twilio_account") or "w1")["label"] if len(TWILIO_ACCOUNTS) > 1 else "Twilio"
    if len(TELNYX_ACCOUNTS) > 1:
        return tx_conf(u.get("telnyx_account") or "t1")["label"]
    return "Telnyx"
templates.env.globals["provider_label"] = provider_label

def calling_lines():
    """Provider choices for the admin: Telnyx 1, Telnyx 2, Twilio 1, Twilio 2."""
    out = []
    for key, label in (("t1", "Telnyx 1"), ("t2", "Telnyx 2")):
        c = TELNYX_ACCOUNTS.get(key)
        out.append({"value": "telnyx:" + key, "provider": "telnyx", "account": key, "label": label,
                    "ready": bool(c and c["api_key"])})
    for key, label in (("w1", "Twilio 1"), ("w2", "Twilio 2")):
        c = TWILIO_ACCOUNTS.get(key)
        out.append({"value": "twilio:" + key, "provider": "twilio", "account": key, "label": label,
                    "ready": bool(c and c["sid"] and c["token"])})
    return out

def user_line(u):
    if (u.get("provider") or "telnyx") == "twilio":
        return "twilio:" + (u.get("twilio_account") or "w1")
    return "telnyx:" + (u.get("telnyx_account") or "t1")

templates.env.globals["calling_lines"] = calling_lines
templates.env.globals["user_line"] = user_line

# ---- Calling countries: a UK line calls only the UK, a US line only US/Canada,
# unless the admin allows more for that user.
CALL_ZONES = {"US": ("1", "US / Canada"), "UK": ("44", "United Kingdom")}

def number_zone(number):
    digits = re.sub(r"\D", "", str(number or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    for zone, (prefix, _label) in CALL_ZONES.items():
        if digits.startswith(prefix):
            return zone
    return ""

def call_zones(user):
    """Zones this user may call. Empty list = no limit (admin, or a line outside the known zones)."""
    if not user or user.get("role") == "admin":
        return []
    chosen = [z for z in str(user.get("call_countries") or "").split(",") if z in CALL_ZONES]
    if chosen:
        return chosen
    own = number_zone(user.get("phone_number"))
    return [own] if own else []

def is_emergency_number(number):
    """True for emergency / short codes (999, 112, 911, 933 ...) and anything too short
    to be a real phone number. These are never dialed: Telnyx charges $100 for an
    emergency call from a number without a registered emergency address."""
    digits = re.sub(r"\D", "", str(number or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) < 8:
        return True
    zone = number_zone(digits)
    if zone == "US":
        national = digits[1:]
        return len(national) != 10 or national[1:3] == "11" or national[:3] in ("933", "112")
    if zone == "UK":
        national = digits[2:]
        return not (9 <= len(national) <= 10) or national.startswith(("999", "112", "911"))
    return False

def call_allowed(user, to_number):
    if is_emergency_number(to_number):
        return False
    zones = call_zones(user)
    return not zones or number_zone(to_number) in zones

def call_zone_text(user):
    zones = call_zones(user)
    return " and ".join(CALL_ZONES[z][1] for z in zones) if zones else "any country"

def call_block_message(user, to_number=None):
    if to_number is not None and is_emergency_number(to_number):
        return "Emergency and short numbers cannot be called from VoxsBridge."
    return "Your line can only call " + call_zone_text(user) + ". Ask the admin to allow more countries."

templates.env.globals["call_zones"] = call_zones
templates.env.globals["call_zone_text"] = call_zone_text
templates.env.globals["has_sms"] = has_sms
templates.env.globals["plan_status"] = plan_status

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("session_token")
    revoke_session(token)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session_token")
    return response

def _count_contacts(user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS n FROM public.voxsbridge_contacts WHERE user_id=%s',(user_id,))
            return cur.fetchone()['n']

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("user_type") == "reseller":
        return RedirectResponse(url="/clients", status_code=302)
    period = request.query_params.get("period", "today")
    if period not in ("today", "week", "month"): period = "today"
    from zoneinfo import ZoneInfo
    chart_timezone = ZoneInfo(os.getenv("DASHBOARD_TIMEZONE", "Asia/Karachi"))
    today = datetime.now(chart_timezone).date()
    beginning_day = today if period == "today" else (today - timedelta(days=today.weekday()) if period == "week" else today.replace(day=1))
    beginning = datetime.combine(beginning_day, datetime.min.time(), tzinfo=chart_timezone)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT c.status,c.duration_seconds AS duration,c.created_at
                FROM public.calls c WHERE c.user_id=%s AND c.created_at >= %s
                ORDER BY c.created_at DESC""", (user["id"], beginning))
            user_calls = cur.fetchall()
    # Chart buckets are based on the same user and date-filtered calls as the stats.
    # Display times in the configured business timezone (default Pakistan).
    from zoneinfo import ZoneInfo
    chart_tz = ZoneInfo(os.getenv("DASHBOARD_TIMEZONE", "Asia/Karachi"))
    now_local = datetime.now(chart_tz)
    local_today = now_local.date()
    if period == "today":
        chart_labels = [f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}" for h in range(0, 24, 3)]
        chart_counts = [0] * 8
    elif period == "week":
        chart_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        chart_counts = [0] * 7
    else:
        days_in_month = calendar.monthrange(local_today.year, local_today.month)[1]
        chart_starts = list(range(1, days_in_month + 1, 5))
        chart_labels = [f"{start}–{min(start + 4, days_in_month)}" for start in chart_starts]
        chart_counts = [0] * len(chart_labels)
    for call in user_calls:
        ts = call.get("created_at")
        if not ts:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local_ts = ts.astimezone(chart_tz)
        d = local_ts.date()
        if period == "today" and d == local_today:
            chart_counts[local_ts.hour // 3] += 1
        elif period == "week" and local_today - timedelta(days=local_today.weekday()) <= d <= local_today:
            chart_counts[d.weekday()] += 1
        elif period == "month" and d.year == local_today.year and d.month == local_today.month and d <= local_today:
            chart_counts[(d.day - 1) // 5] += 1
    chart_max = max(max(chart_counts, default=0), 1)
    chart_bars = [{"label": label, "count": count, "height": round(count * 100 / chart_max)}
                  for label, count in zip(chart_labels, chart_counts)]
    total_calls = len(user_calls)
    answered = len([c for c in user_calls if c.get("status") in ("answered", "completed", "active")])
    failed = len([c for c in user_calls if c.get("status") not in ("answered", "completed", "active")])
    answer_rate = f"{int((answered/total_calls)*100)}%" if total_calls else "0%"
    talk = [
        int(c.get("duration") or 0)
        for c in user_calls
        if int(c.get("duration") or 0) > 0
    ]
    avg = round(sum(talk) / len(talk)) if talk else 0
    avg_talk = f"{avg // 60}:{avg % 60:02d}"
    revenue = 0
    revenue_life = 0
    renewals = []
    refresh_users()
    if user.get("role") == "admin":
        for account in users_db.values():
            if account.get("role") == "admin": continue
            try:
                remaining = (date.fromisoformat(account.get("plan_renewal", "")) - today).days
            except ValueError:
                continue
            if 0 <= remaining <= 7:
                renewals.append({"name": account.get("name"), "email": account.get("email"), "renewal": account.get("plan_renewal"), "amount": account.get("last_paid_amount", 0), "days": remaining})
        renewals.sort(key=lambda r: r["days"])
    costs = None
    if user.get("role") == "admin":
        try:
            costs = admin_finance()
        except Exception:
            log.exception("Dashboard cost summary failed")
        if costs:
            revenue = int(round(costs["month"]["revenue_pkr"]))
            revenue_life = int(round(costs["life"]["revenue_pkr"]))
    admin_stats = None
    if user.get("role") == "admin":
        # VoxsBridge admin console: management overview instead of personal calling stats.
        customers = [a for a in users_db.values()
                     if a.get("role") != "admin" and a.get("user_type") != "reseller"]
        expired = 0
        for a in customers:
            try:
                if date.fromisoformat(a.get("plan_renewal", "")) < today:
                    expired += 1
            except ValueError:
                pass
        admin_stats = {
            "customers": len(customers),
            "active": len(customers) - expired,
            "expired": expired,
            "demo": len([a for a in customers if a.get("is_demo")]),
            "resellers": len([a for a in users_db.values() if a.get("user_type") == "reseller"]),
            "companies": 0, "numbers": 0, "platform_calls": 0,
        }
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) AS n FROM public.voxsbridge_accounts")
                    admin_stats["companies"] = cur.fetchone()["n"]
                    cur.execute("SELECT count(DISTINCT phone_number) AS n FROM public.caller_id_assignments")
                    admin_stats["numbers"] = cur.fetchone()["n"]
                    cur.execute("SELECT count(*) AS n FROM public.calls WHERE created_at >= %s", (beginning,))
                    admin_stats["platform_calls"] = cur.fetchone()["n"]
        except Exception:
            log.exception("Dashboard admin stats failed")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM public.voxsbridge_recordings WHERE user_id=%s AND kind='call'",
                        (user["id"],))
            recordings_count = cur.fetchone()["n"]
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "chart_bars": chart_bars,
        "period": period, "revenue": revenue, "revenue_life": revenue_life, "renewals": renewals, "costs": costs,
        "admin_stats": admin_stats,
        "user": user, "total_calls": total_calls, "answer_rate": answer_rate,
        "avg_talk_time": avg_talk, "contacts_count": _count_contacts(user["id"]),
        "followups": 0, "failed_calls": failed, "answered_calls": answered,
        "recordings_count": recordings_count,
        "campaigns_count": len(campaigns_db.get(user["email"], []))
    })

@app.get("/dialer", response_class=HTMLResponse)
async def dialer_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    number=request.query_params.get("number","")
    if number:
        number=_crm_number(number)
        return RedirectResponse(url="/dashboard?"+urllib.parse.urlencode({"number":number,"call":"1"}),status_code=302)
    return RedirectResponse(url="/dashboard", status_code=302)

def _telnyx_request(method: str, path: str, body: dict = None):
    api_key = tx_conf()["api_key"]
    if not api_key:
        raise HTTPException(status_code=500, detail=f"{tx_conf()['label']} API key missing")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        "https://api.telnyx.com" + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode().strip()
            if not raw:
                return {}
            if raw.startswith("{") or raw.startswith("["):
                return json.loads(raw)
            return {"raw": raw.strip('"')}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        raise HTTPException(status_code=502, detail=f"Telnyx error: {detail}")

def _set_user_telnyx_account(user_id, acct):
    """Put the user on the Telnyx account that owns their number. Their calling
    login belongs to one account, so switching accounts makes a new one."""
    acct = tx_key(acct)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT telnyx_account, credential_id FROM public.voxsbridge_users WHERE id=%s""", (user_id,))
            row = cur.fetchone()
            if not row or tx_key(row["telnyx_account"]) == acct:
                if row and row["telnyx_account"] != acct:
                    cur.execute("UPDATE public.voxsbridge_users SET telnyx_account=%s WHERE id=%s", (acct, user_id))
                return
            cur.execute("""UPDATE public.voxsbridge_users SET telnyx_account=%s, credential_id=NULL,
                sip_username=NULL WHERE id=%s""", (acct, user_id))
    old_acct, old_cred = tx_key(row["telnyx_account"]), row["credential_id"]
    for u in users_db.values():
        if str(u.get("id")) == str(user_id):
            u.update(telnyx_account=acct, credential_id="", sip_username="")
    if old_cred:
        try:
            with tx_ctx(old_acct):
                _telnyx_request("DELETE", f"/v2/telephony_credentials/{old_cred}")
        except Exception:
            log.info("Old calling login %s on %s was not deleted", old_cred, old_acct)

def _ensure_credential(email: str, force: bool = False) -> str:
    user = users_db.get(email) or {}
    if not force and user.get("credential_id"):
        return user["credential_id"]
    created = _telnyx_request("POST", "/v2/telephony_credentials", {
        "connection_id": tx_conf()["connection_id"],
        "name": f"voxsbridge-{email}"[:60],
        "tag": "voxsbridge",
    })
    data = created.get("data", {})
    cred_id = data.get("id")
    if not cred_id:
        raise HTTPException(status_code=502, detail="Could not create telephony credential")
    if email in users_db:
        users_db[email]["credential_id"] = cred_id
        users_db[email]["sip_username"] = data.get("sip_username", "")
        save_users()
    else:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE public.voxsbridge_users SET credential_id=%s, sip_username=%s WHERE email=%s",
                            (cred_id, data.get("sip_username", ""), email))
    return cred_id

LINE_UNAVAILABLE = "Calling line is temporarily unavailable. Please contact support."

def _customer_safe(fn):
    """Hide provider names/details (Telnyx, Twilio) from non-admin users."""
    import functools
    @functools.wraps(fn)
    async def wrapper(request: Request, *args, **kwargs):
        try:
            return await fn(request, *args, **kwargs)
        except HTTPException as exc:
            if exc.status_code in (401, 402):
                raise
            user = get_current_user(request)
            if user and user.get("role") == "admin":
                raise
            log.warning("Calling token error hidden from customer: %s", exc.detail)
            raise HTTPException(status_code=exc.status_code, detail=LINE_UNAVAILABLE)
    return wrapper

@app.get("/api/webrtc-token")
@_customer_safe
async def webrtc_token(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if user.get("role") != "admin" and plan_status(user)["days_left"] < 0:
        raise HTTPException(status_code=402, detail="Subscription expired")
    if user.get("provider") == "twilio":
        raise HTTPException(status_code=409, detail="This account uses Twilio; request /api/twilio-token")
    def _mint():
        cred_id = _ensure_credential(user["email"])
        resp = _telnyx_request("POST", f"/v2/telephony_credentials/{cred_id}/token")
        tok = None
        if isinstance(resp, dict):
            tok = resp.get("raw") or resp.get("data")
            if isinstance(tok, dict):
                tok = tok.get("token")
        return tok if (tok and isinstance(tok, str)) else None
    def _mint_with_retry():
        try:
            t = _mint()
        except HTTPException:
            t = None
        if not t:
            log.info("Recreating Telnyx credential for %s on the current account", user["email"])
            _ensure_credential(user["email"], force=True)
            t = _mint()
        return t
    token = await run_in_threadpool(_mint_with_retry)
    if not token or not isinstance(token, str):
        raise HTTPException(status_code=502, detail="Token generation failed")
    return JSONResponse({
        "token": token,
        "from_number": user.get("phone_number") or TELNYX_FROM_NUMBER,
    })

@app.get("/api/call-state")
async def call_state(request: Request, id: str = ""):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    call_id = id.strip()
    if not call_id or len(call_id) > 80:
        return JSONResponse({"ended": False})
    try:
        payload = _telnyx_request("GET", "/v2/calls/" + call_id)
    except HTTPException as exc:
        missing = "404" in str(exc.detail) or "not found" in str(exc.detail).lower() or "90015" in str(exc.detail)
        return JSONResponse({"ended": False, "missing": missing})
    info = payload.get("data") or {}
    if info.get("is_alive") is False:
        cause = str(info.get("hangup_cause") or "").lower()
        reason = "Call ended"
        if "busy" in cause:
            reason = "Busy"
        elif "reject" in cause or "decline" in cause:
            reason = "Call declined"
        elif "timeout" in cause or "no_answer" in cause or "unallocated" in cause:
            reason = "No answer"
        return JSONResponse({"ended": True, "reason": reason})
    return JSONResponse({"ended": False, "alive": True})

@app.post("/api/call-log")
async def log_webrtc_call(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = await request.json()
    to_number = str(data.get("to_number") or "").strip()[:40]
    status = str(data.get("status") or "initiated")[:60]
    if not call_allowed(user, to_number):
        return JSONResponse({"success": False, "blocked": True, "error": call_block_message(user, to_number)}, status_code=403)
    from_number = user.get("phone_number") or TELNYX_FROM_NUMBER
    provider = user.get("provider") or "telnyx"
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.calls
                (user_id,from_number,to_number,direction,status,provider,created_at)
                VALUES (%s,%s,%s,'outgoing',%s,%s,NOW()) RETURNING id,created_at""",
                (user["id"], from_number, to_number, status, provider))
            row = cur.fetchone()
    call_entry = {"id": str(row["id"]), "from_number": from_number, "to_number": to_number,
                  "status": status, "duration": 0, "provider": provider,
                  "created_at": row["created_at"].isoformat()}
    return JSONResponse({"success": True, "call": call_entry, "id": call_entry["id"]})

@app.post("/api/call-log/{call_id}")
async def update_call_log(call_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = await request.json()
    try:
        call_uuid = uuid.UUID(call_id)
    except ValueError:
        raise HTTPException(status_code=404,detail="Call not found")
    duration = max(0,int(data.get("duration") or 0))
    status = str(data.get("status") or "initiated")[:60]
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.calls SET duration_seconds=%s,status=%s
                WHERE id=%s AND user_id=%s RETURNING id""",
                (duration,status,call_uuid,user["id"]))
            if not cur.fetchone():
                raise HTTPException(status_code=404,detail="Call not found")
    return JSONResponse({"success":True})

@app.delete("/api/call-log/{call_id}")
async def delete_call_log(call_id: uuid.UUID, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    from psycopg.errors import ForeignKeyViolation
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.calls WHERE id=%s AND user_id=%s RETURNING id",
                            (call_id, user["id"]))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Call not found")
    except ForeignKeyViolation:
        raise HTTPException(status_code=409, detail="This call is linked to other records and cannot be deleted")
    return {"deleted": True}


# ==================== CALL LOGS ====================
def _local_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(os.getenv("DASHBOARD_TIMEZONE", "Asia/Karachi"))

def _to_local(ts):
    if not ts:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(_local_tz())

def _day_label(local_ts, today):
    d = local_ts.date()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return local_ts.strftime("%d %b %Y")

def _contact_names(user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,number FROM public.voxsbridge_contacts WHERE user_id=%s", (user_id,))
            return {r["number"]: {"id": str(r["id"]), "name": r["name"]} for r in cur.fetchall()}

def _clean_number(value):
    value = str(value or "").strip()
    if not value or "anonymous" in value.lower():
        return ""
    return value

def _like(q):
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

@app.get("/api/search")
async def global_search(request: Request, q: str = "", scope: str = ""):
    """Header search. Admin: users/customers (name, email, phone, plan, assigned numbers), contacts of all users
    and call logs of all users. Normal users: only their own contacts and call logs.
    scope=customers returns just the matching customer emails (used to filter the Users / Customers tables)."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    q = (q or "").strip()[:80]
    out = {"q": q, "users": [], "contacts": [], "calls": [], "emails": []}
    if len(q) < 2:
        return out
    admin = user.get("role") == "admin"
    like = _like(q)
    digits = re.sub(r"\D", "", q)
    dlike = "%" + digits + "%" if len(digits) >= 3 else None
    lim = 200 if scope == "customers" else 8
    with db_connect() as conn:
        with conn.cursor() as cur:
            emails = set()
            if admin:
                cur.execute(r"""SELECT u.name,u.email,u.phone_number,u.plan_name,u.is_active,u.signup_status
                    FROM public.voxsbridge_users u WHERE u.role<>'admin' AND (
                        u.name ILIKE %(l)s ESCAPE '\' OR u.email ILIKE %(l)s ESCAPE '\' OR COALESCE(u.plan_name,'') ILIKE %(l)s ESCAPE '\'
                        OR COALESCE(u.phone_number,'') ILIKE %(l)s ESCAPE '\'
                        OR (%(d)s::text IS NOT NULL AND regexp_replace(COALESCE(u.phone_number,''), '\D', '', 'g') LIKE %(d)s)
                        OR u.id IN (SELECT a.user_id FROM public.caller_id_assignments a
                                    WHERE a.phone_number ILIKE %(l)s ESCAPE '\'
                                       OR (%(d)s::text IS NOT NULL AND regexp_replace(a.phone_number, '\D', '', 'g') LIKE %(d)s)))
                    ORDER BY u.is_active DESC, u.created_at DESC LIMIT %(n)s""", {"l": like, "d": dlike, "n": lim})
                rows = cur.fetchall()
                out["users"] = [{"name": r["name"], "email": r["email"], "number": r["phone_number"] or "",
                                 "plan": r["plan_name"] or "", "active": bool(r["is_active"]),
                                 "href": "/customers/" + urllib.parse.quote(r["email"], safe="")} for r in rows]
                emails |= {r["email"].lower() for r in rows}
            # contacts
            where = r"""(c.name ILIKE %(l)s ESCAPE '\' OR c.number ILIKE %(l)s ESCAPE '\'
                         OR (%(d)s::text IS NOT NULL AND regexp_replace(c.number,'\D','','g') LIKE %(d)s))"""
            if admin:
                cur.execute("""SELECT c.name,c.number,u.name AS owner,u.email FROM public.voxsbridge_contacts c
                    JOIN public.voxsbridge_users u ON u.id=c.user_id WHERE """ + where + " ORDER BY c.created_at DESC LIMIT %(n)s",
                            {"l": like, "d": dlike, "n": lim})
            else:
                cur.execute("""SELECT c.name,c.number,NULL AS owner,NULL AS email FROM public.voxsbridge_contacts c
                    WHERE c.user_id=%(u)s AND """ + where + " ORDER BY c.created_at DESC LIMIT %(n)s",
                            {"l": like, "d": dlike, "n": lim, "u": user["id"]})
            for r in cur.fetchall():
                out["contacts"].append({"name": r["name"], "number": r["number"], "owner": r["owner"] or "",
                    "href": ("/customers/" + urllib.parse.quote(r["email"], safe="")) if admin and r["email"]
                            else "/contacts?q=" + urllib.parse.quote(q)})
                if r["email"]: emails.add(r["email"].lower())
            # call logs (by number)
            cw = r"""(c.from_number ILIKE %(l)s ESCAPE '\' OR c.to_number ILIKE %(l)s ESCAPE '\'
                     OR (%(d)s::text IS NOT NULL AND (regexp_replace(COALESCE(c.from_number,''),'\D','','g') LIKE %(d)s
                                                      OR regexp_replace(COALESCE(c.to_number,''),'\D','','g') LIKE %(d)s)))"""
            if digits or re.search(r"\d", q):
                if admin:
                    cur.execute("""SELECT c.from_number,c.to_number,c.direction,c.status,c.created_at,u.name AS owner,u.email
                        FROM public.calls c LEFT JOIN public.voxsbridge_users u ON u.id=c.user_id WHERE """ + cw +
                                " ORDER BY c.created_at DESC LIMIT %(n)s", {"l": like, "d": dlike, "n": lim})
                else:
                    cur.execute("""SELECT c.from_number,c.to_number,c.direction,c.status,c.created_at,NULL AS owner,NULL AS email
                        FROM public.calls c WHERE c.user_id=%(u)s AND """ + cw + " ORDER BY c.created_at DESC LIMIT %(n)s",
                                {"l": like, "d": dlike, "n": lim, "u": user["id"]})
                for r in cur.fetchall():
                    out["calls"].append({"from": r["from_number"] or "", "to": r["to_number"] or "", "direction": r["direction"] or "",
                        "status": r["status"] or "", "when": r["created_at"].strftime("%d %b %Y %H:%M") if r["created_at"] else "",
                        "owner": r["owner"] or "",
                        "href": ("/customers/" + urllib.parse.quote(r["email"], safe="")) if admin and r["email"]
                                else "/call-logs?q=" + urllib.parse.quote(q)})
                    if r["email"]: emails.add(r["email"].lower())
            if admin:
                out["emails"] = sorted(emails)
    if scope == "customers":
        return {"emails": out["emails"]}
    out["users"] = out["users"][:8]; out["contacts"] = out["contacts"][:8]; out["calls"] = out["calls"][:8]
    out.pop("emails", None)
    return out


@app.get("/call-logs", response_class=HTMLResponse)
async def call_logs_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT c.id,c.from_number,c.to_number,c.direction,c.status,c.duration_seconds,
                    c.created_at,COALESCE(c.provider,'telnyx') AS provider,
                    r.id AS recording_id, r.kind AS recording_kind
                FROM public.calls c
                LEFT JOIN LATERAL (
                    SELECT id,kind FROM public.voxsbridge_recordings
                    WHERE call_id=c.id AND user_id=c.user_id ORDER BY created_at DESC LIMIT 1
                ) r ON TRUE
                WHERE c.user_id=%s ORDER BY c.created_at DESC LIMIT 500""", (user["id"],))
            rows = cur.fetchall()
    contacts = _contact_names(user["id"])
    rates = get_rates() if user.get("role") == "admin" else None
    today = datetime.now(_local_tz()).date()
    user_calls = []
    for r in rows:
        number = _clean_number(r["from_number"] if r["direction"] == "incoming" else r["to_number"])
        local = _to_local(r["created_at"])
        contact = contacts.get(number)
        entry = dict(
            id=str(r["id"]), from_number=r["from_number"] or "", to_number=r["to_number"] or "",
            direction=r["direction"] or "unknown", status=r["status"] or "unknown",
            duration=int(r["duration_seconds"] or 0), provider=r["provider"],
            number=number, contact_name=contact["name"] if contact else "",
            created_at=r["created_at"].isoformat() if r["created_at"] else "",
            date_label=_day_label(local, today) if local else "",
            time_label=local.strftime("%I:%M %p").lstrip("0") if local else "",
            full_time=local.strftime("%d %b %Y, %I:%M:%S %p") if local else "",
            recording_id=str(r["recording_id"]) if r["recording_id"] else "",
            recording_kind=r["recording_kind"] or "",
        )
        if rates is not None:
            entry["cost_usd"] = vh.call_cost(entry["duration"], entry["provider"], entry["direction"], rates,
                                             entry["to_number"])
        user_calls.append(entry)
    selected = request.query_params.get('filter', 'all')
    if selected not in ('all','incoming','outgoing','missed'):
        selected = 'all'
    def is_missed(c):
        return c.get('direction') == 'incoming' and c.get('status') in ('missed','declined','failed')
    counts = {
        'all': len(user_calls),
        'incoming': sum(c.get('direction') == 'incoming' for c in user_calls),
        'outgoing': sum(c.get('direction') == 'outgoing' for c in user_calls),
        'missed': sum(is_missed(c) for c in user_calls),
    }
    filtered = [c for c in user_calls if selected == 'all' or
        (selected == 'missed' and is_missed(c)) or c.get('direction') == selected]
    return templates.TemplateResponse(request=request, name="call_logs.html", context={
        "user":user,"logs":filtered,"counts":counts,"selected":selected,
        "show_cost": rates is not None})


# ==================== CONTACTS ====================
@app.get("/contacts", response_class=HTMLResponse)
async def contacts_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,name,number,created_at FROM public.voxsbridge_contacts
                WHERE user_id=%s ORDER BY created_at DESC""",(user["id"],))
            contacts = [dict(id=str(r["id"]),name=r["name"],number=r["number"],
                created_at=r["created_at"].isoformat()) for r in cur.fetchall()]
    return templates.TemplateResponse(request=request, name="contacts.html", context={
        "user":user,"contacts":contacts})

@app.post("/api/contacts")
async def add_contact(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = await request.json()
    name = str(data.get("name") or "").strip()[:200]
    number = re.sub(r"[\s().-]", "", str(data.get("number") or "").strip())[:40]
    if not name or not number:
        raise HTTPException(status_code=422,detail="Name and number required")
    if not re.fullmatch(r"\+?[0-9*#]{3,20}", number):
        raise HTTPException(status_code=422,detail="Enter a valid phone number, e.g. +447700900123")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,created_at FROM public.voxsbridge_contacts
                WHERE user_id=%s AND number=%s LIMIT 1""", (user["id"], number))
            existing = cur.fetchone()
            if existing:
                cur.execute("UPDATE public.voxsbridge_contacts SET name=%s WHERE id=%s", (name, existing["id"]))
                r = existing
            else:
                cur.execute("""INSERT INTO public.voxsbridge_contacts(user_id,name,number)
                    VALUES(%s,%s,%s) RETURNING id,created_at""",(user["id"],name,number))
                r=cur.fetchone()
    contact=dict(id=str(r["id"]),name=name,number=number,created_at=r["created_at"].isoformat())
    return JSONResponse({"success":True,"contact":contact,"updated":bool(existing)})

@app.delete("/api/contacts/{contact_id}")
async def delete_contact(contact_id: uuid.UUID, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.voxsbridge_contacts WHERE id=%s AND user_id=%s RETURNING id",
                        (contact_id, user["id"]))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Contact not found")
    return {"deleted": True}


# ==================== RECORDINGS & VOICEMAILS ====================
AUDIO_TYPES = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mpeg": ".mp3",
               "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/wav": ".wav", "audio/x-wav": ".wav"}

def _store_recording(user_id, kind, data, content_type, *, call_id=None, provider="telnyx",
                     from_number="", to_number="", duration=0, provider_ref=None):
    """Persist audio privately. Returns the new recording id, or None for a duplicate."""
    content_type = (content_type or "audio/webm").split(";")[0].strip().lower()
    if content_type not in AUDIO_TYPES:
        content_type = "audio/webm"
    rec_id = uuid.uuid4()
    storage_kind, storage_path, audio = "db", None, data
    if storage.enabled:
        path = f"{user_id}/{kind}/{rec_id}{AUDIO_TYPES[content_type]}"
        try:
            storage.upload(path, data, content_type)
            storage_kind, storage_path, audio = "supabase", path, None
        except vh.ProviderError:
            log.exception("Supabase Storage upload failed; keeping recording in the database")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.voxsbridge_recordings
                (id,user_id,call_id,kind,provider,from_number,to_number,duration_seconds,
                 content_type,size_bytes,storage,storage_path,audio,provider_ref)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (provider_ref) DO NOTHING RETURNING id""",
                (rec_id, user_id, call_id, kind, provider, (from_number or "")[:40], (to_number or "")[:40],
                 max(0, int(duration or 0)), content_type, len(data), storage_kind, storage_path,
                 audio, provider_ref))
            row = cur.fetchone()
    if not row and storage_path:
        storage.delete(storage_path)
    return row["id"] if row else None

def _recording_rows(user_id, kind):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,call_id,kind,provider,from_number,to_number,duration_seconds,
                    size_bytes,is_read,created_at
                FROM public.voxsbridge_recordings WHERE user_id=%s AND kind=%s
                ORDER BY created_at DESC LIMIT 500""", (user_id, kind))
            rows = cur.fetchall()
    contacts = _contact_names(user_id)
    today = datetime.now(_local_tz()).date()
    items = []
    for r in rows:
        local = _to_local(r["created_at"])
        number = _clean_number(r["from_number"] if kind == "voicemail" else (r["to_number"] or r["from_number"]))
        contact = contacts.get(number)
        items.append(dict(
            id=str(r["id"]), number=number or "Unknown", contact_name=contact["name"] if contact else "",
            duration=int(r["duration_seconds"] or 0), is_read=bool(r["is_read"]),
            size_kb=max(1, int((r["size_bytes"] or 0) / 1024)),
            date_label=_day_label(local, today) if local else "",
            time_label=local.strftime("%I:%M %p").lstrip("0") if local else "",
            is_today=bool(local and local.date() == today),
            url=f"/recordings/{r['id']}/audio",
        ))
    return items

@app.get("/recordings", response_class=HTMLResponse)
async def recordings_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request=request, name="recordings.html", context={
        "user": user,
        "recordings": _recording_rows(user["id"], "call"),
        "recording_ok": has_recording(user),
    })

@app.post("/api/recordings")
async def save_recording(
    request: Request,
    file: UploadFile = File(...),
    to_number: str = Form(""),
    duration: int = Form(0),
    kind: str = Form("call"),
    call_id: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if kind != "call":
        raise HTTPException(status_code=422, detail="Only call recordings can be uploaded")
    if not has_recording(user):
        raise HTTPException(status_code=402, detail="Call recording is a $25 upgrade. Ask your admin to enable it.")
    data = await file.read(MAX_RECORDING_BYTES + 1)
    if not data:
        raise HTTPException(status_code=422, detail="Empty recording")
    if len(data) > MAX_RECORDING_BYTES:
        raise HTTPException(status_code=413, detail="Recording is too large")
    linked_call = None
    if call_id:
        try:
            linked_call = uuid.UUID(call_id)
        except ValueError:
            linked_call = None
    direction_from, direction_to = user.get("phone_number") or "", to_number.strip()[:40]
    if linked_call:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,direction,from_number,to_number FROM public.calls
                    WHERE id=%s AND user_id=%s""", (linked_call, user["id"]))
                row = cur.fetchone()
        if row:
            direction_from, direction_to = row["from_number"] or "", row["to_number"] or ""
        else:
            linked_call = None
    rec_id = _store_recording(user["id"], "call", data, file.content_type or "audio/webm",
                              call_id=linked_call, provider=user.get("provider") or "telnyx",
                              from_number=direction_from, to_number=direction_to,
                              duration=min(max(0, duration), 86400))
    return JSONResponse({"success": True, "recording": {"id": str(rec_id), "url": f"/recordings/{rec_id}/audio"}})

def _range_response(data, content_type, range_header, extra_headers):
    size = len(data)
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", (range_header or "").strip())
    if match and size:
        start_s, end_s = match.groups()
        if start_s == "" and end_s:
            start, end = max(0, size - int(end_s)), size - 1
        else:
            start = int(start_s or 0)
            end = min(int(end_s), size - 1) if end_s else size - 1
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        headers = dict(extra_headers)
        headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes"})
        return Response(data[start:end + 1], status_code=206, media_type=content_type, headers=headers)
    headers = dict(extra_headers)
    headers["Accept-Ranges"] = "bytes"
    return Response(data, media_type=content_type, headers=headers)

@app.get("/recordings/{rec_id}/audio")
async def recording_audio(rec_id: uuid.UUID, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT r.user_id,r.kind,r.content_type,r.storage,r.storage_path,r.audio,r.created_at,
                    u.account_id FROM public.voxsbridge_recordings r
                JOIN public.voxsbridge_users u ON u.id=r.user_id WHERE r.id=%s""", (rec_id,))
            row = cur.fetchone()
            own = bool(row) and str(row["user_id"]) == user["id"]
            if not row or not (own or _can_manage_account(user, row["account_id"])):
                raise HTTPException(status_code=404, detail="Recording not found")
            if row["kind"] == "voicemail" and own:
                cur.execute("UPDATE public.voxsbridge_recordings SET is_read=TRUE WHERE id=%s AND is_read=FALSE",
                            (rec_id,))
    if row["storage"] == "supabase":
        try:
            data, _ = storage.download(row["storage_path"])
        except vh.ProviderError:
            log.exception("Recording download from Supabase Storage failed")
            raise HTTPException(status_code=502, detail="Recording storage is unavailable")
    else:
        data = bytes(row["audio"] or b"")
    ext = AUDIO_TYPES.get(row["content_type"], ".webm")
    stamp = _to_local(row["created_at"]).strftime("%Y%m%d-%H%M") if row["created_at"] else "recording"
    disposition = "attachment" if request.query_params.get("download") else "inline"
    headers = {"Cache-Control": "private, no-store",
               "Content-Disposition": f'{disposition}; filename="voxsbridge-{row["kind"]}-{stamp}{ext}"'}
    return _range_response(data, row["content_type"], request.headers.get("range"), headers)

# ==================== ADMIN MONITOR RECORDINGS ====================
# Every call is recorded by the dialer and uploaded here. Only an admin can
# list or play these; they are deleted automatically after 14 days.
MONITOR_KEEP_DAYS = int(os.getenv("MONITOR_RECORDING_DAYS", "14"))
_monitor_purge = {"at": 0.0}

def _purge_monitor_recordings(force=False):
    if not force and time.time() - _monitor_purge["at"] < 3600:
        return 0
    _monitor_purge["at"] = time.time()
    removed = 0
    try:
        while True:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""DELETE FROM public.monitor_recordings WHERE id IN (
                            SELECT id FROM public.monitor_recordings
                            WHERE created_at < now() - make_interval(days => %s) LIMIT 200)
                        RETURNING storage, storage_path""", (MONITOR_KEEP_DAYS,))
                    rows = cur.fetchall()
            for r in rows:
                if r["storage"] == "supabase" and r["storage_path"]:
                    try:
                        storage.delete(r["storage_path"])
                    except vh.ProviderError:
                        log.warning("Could not delete old monitor recording object")
            removed += len(rows)
            if len(rows) < 200:
                break
    except Exception:
        log.exception("Monitor recording clean-up failed")
    return removed

@app.post("/api/monitor-recordings")
async def save_monitor_recording(request: Request, file: UploadFile = File(...), number: str = Form(""),
                                 direction: str = Form("outgoing"), duration: int = Form(0)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if user.get("role") == "admin":
        return {"ok": True}
    data = await file.read(MAX_RECORDING_BYTES + 1)
    if not data or len(data) > MAX_RECORDING_BYTES:
        return {"ok": False}
    direction = "incoming" if direction == "incoming" else "outgoing"
    number = re.sub(r"[^0-9+]", "", number)[:40]
    duration = min(max(0, int(duration or 0)), 86400)
    content_type = (file.content_type or "audio/webm").split(";")[0].strip().lower()
    if content_type not in AUDIO_TYPES:
        content_type = "audio/webm"
    mine = user.get("phone_number") or ""
    tail = re.sub(r"[^0-9]", "", number)[-9:] or "0000000000"
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id FROM public.calls WHERE user_id=%s AND direction=%s
                AND created_at > now() - make_interval(secs => %s)
                AND regexp_replace(CASE WHEN direction='incoming' THEN from_number ELSE to_number END, '[^0-9]', '', 'g')
                    LIKE '%%' || %s
                ORDER BY created_at DESC LIMIT 1""", (user["id"], direction, duration + 600, tail))
            row = cur.fetchone()
            call_id = row["id"] if row else None
    rec_id = uuid.uuid4()
    kind, path, audio = "db", None, data
    if storage.enabled:
        obj = "monitor/" + str(user["id"]) + "/" + str(rec_id) + AUDIO_TYPES[content_type]
        try:
            storage.upload(obj, data, content_type)
            kind, path, audio = "supabase", obj, None
        except vh.ProviderError:
            log.exception("Monitor recording upload failed; kept in the database")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.monitor_recordings
                (id,user_id,call_id,direction,provider,from_number,to_number,duration_seconds,content_type,
                 size_bytes,storage,storage_path,audio)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (rec_id, user["id"], call_id, direction, user.get("provider") or "telnyx",
                 number if direction == "incoming" else mine, mine if direction == "incoming" else number,
                 duration, content_type, len(data), kind, path, audio))
    _purge_monitor_recordings()
    return {"ok": True}

@app.get("/customers/{email}/recordings", response_class=HTMLResponse)
async def customer_monitor_recordings(request: Request, email: str):
    admin = require_admin(request)
    _purge_monitor_recordings()
    q = request.query_params
    direction = q.get("direction", "")
    number = re.sub(r"[^0-9]", "", q.get("number", ""))[:20]
    day = q.get("date", "")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,email,phone_number FROM public.voxsbridge_users WHERE lower(email)=lower(%s)",
                        (email,))
            cust = cur.fetchone()
            if not cust:
                raise HTTPException(404, "Customer not found")
            where, params = ["r.user_id=%s"], [cust["id"]]
            if direction in ("incoming", "outgoing"):
                where.append("r.direction=%s"); params.append(direction)
            if number:
                where.append("(regexp_replace(r.from_number,'[^0-9]','','g') LIKE %s OR regexp_replace(r.to_number,'[^0-9]','','g') LIKE %s)")
                params += ["%" + number + "%", "%" + number + "%"]
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
                where.append("(r.created_at AT TIME ZONE %s)::date = %s::date")
                params += [os.getenv("DASHBOARD_TIMEZONE", "Asia/Karachi"), day]
            cur.execute("SELECT r.id,r.direction,r.from_number,r.to_number,r.duration_seconds,r.size_bytes,r.created_at "
                        "FROM public.monitor_recordings r WHERE " + " AND ".join(where) +
                        " ORDER BY r.created_at DESC LIMIT 500", params)
            rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS b FROM public.monitor_recordings WHERE user_id=%s",
                        (cust["id"],))
            totals = cur.fetchone()
    items = []
    for r in rows:
        local = _to_local(r["created_at"])
        other = r["from_number"] if r["direction"] == "incoming" else r["to_number"]
        items.append({"id": str(r["id"]), "direction": r["direction"], "number": other or "Unknown",
                      "duration": int(r["duration_seconds"] or 0),
                      "size_kb": max(1, int((r["size_bytes"] or 0) / 1024)),
                      "when": local.strftime("%d %b %Y, %I:%M %p") if local else ""})
    return templates.TemplateResponse(request=request, name="admin_recordings.html", context={
        "user": admin, "customer": cust, "items": items, "keep_days": MONITOR_KEEP_DAYS,
        "total": int(totals["n"] or 0), "total_mb": round(float(totals["b"] or 0) / 1048576, 1),
        "f_direction": direction, "f_number": q.get("number", ""), "f_date": day})

@app.get("/admin/monitor-recordings/{rec_id}/audio")
async def monitor_recording_audio(rec_id: uuid.UUID, request: Request):
    require_admin(request)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT content_type,storage,storage_path,audio,created_at,direction
                FROM public.monitor_recordings WHERE id=%s""", (rec_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    if row["storage"] == "supabase":
        try:
            data, _ = storage.download(row["storage_path"])
        except vh.ProviderError:
            raise HTTPException(status_code=502, detail="Recording storage is unavailable")
    else:
        data = bytes(row["audio"] or b"")
    ext = AUDIO_TYPES.get(row["content_type"], ".webm")
    stamp = _to_local(row["created_at"]).strftime("%Y%m%d-%H%M") if row["created_at"] else "call"
    disp = "attachment" if request.query_params.get("download") else "inline"
    headers = {"Cache-Control": "private, no-store",
               "Content-Disposition": disp + '; filename="call-' + row["direction"] + "-" + stamp + ext + '"'}
    return _range_response(data, row["content_type"], request.headers.get("range"), headers)

def _delete_monitor_rows(ids):
    """Delete admin recordings (database rows + Storage objects). Returns how many were removed."""
    if not ids:
        return 0
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""DELETE FROM public.monitor_recordings WHERE id = ANY(%s::uuid[])
                RETURNING storage, storage_path""", (ids,))
            rows = cur.fetchall()
    for r in rows:
        if r["storage"] == "supabase" and r["storage_path"]:
            try:
                storage.delete(r["storage_path"])
            except Exception:
                log.warning("Could not delete admin recording object from storage")
    return len(rows)

@app.delete("/admin/monitor-recordings/{rec_id}")
async def delete_monitor_recording(rec_id: uuid.UUID, request: Request):
    require_admin(request)
    if not _delete_monitor_rows([str(rec_id)]):
        raise HTTPException(status_code=404, detail="Recording not found")
    return {"deleted": 1}

@app.post("/admin/monitor-recordings/delete")
async def delete_monitor_recordings_bulk(request: Request):
    """Admin: delete several recordings at once (ids from the Recordings page)."""
    require_admin(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    ids = []
    for v in (data.get("ids") or [])[:500]:
        try:
            ids.append(str(uuid.UUID(str(v))))
        except ValueError:
            pass
    if not ids:
        raise HTTPException(status_code=422, detail="Select at least one recording")
    return {"deleted": _delete_monitor_rows(ids)}

@app.delete("/api/recordings/{rec_id}")
async def delete_recording(rec_id: uuid.UUID, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""DELETE FROM public.voxsbridge_recordings WHERE id=%s AND user_id=%s
                RETURNING storage,storage_path""", (rec_id, user["id"]))
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    if row["storage"] == "supabase" and row["storage_path"]:
        try:
            storage.delete(row["storage_path"])
        except vh.ProviderError:
            log.exception("Could not delete recording object from storage")
    return {"deleted": True}

@app.get("/voicemails", response_class=HTMLResponse)
async def voicemails_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    items = _recording_rows(user["id"], "voicemail")
    selected = request.query_params.get("filter", "all")
    stats = {"total": len(items), "unread": sum(not v["is_read"] for v in items),
             "today": sum(v["is_today"] for v in items)}
    if selected == "unread":
        items = [v for v in items if not v["is_read"]]
    else:
        selected = "all"
    return templates.TemplateResponse(request=request, name="voicemails.html", context={
        "user": user, "voicemails": items, "stats": stats, "selected": selected,
    })


# ==================== TELNYX WEBHOOK (incoming + voicemail) ====================
def verify_telnyx_webhook(raw: bytes, signature: str, timestamp: str):
    """Validate the Telnyx API v2 Ed25519 signature (5-minute replay window).
    Returns the Telnyx account ("t1"/"t2") whose key signed it, or None.
    The account is also made current for the rest of the request."""
    if not signature or not timestamp:
        return None
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > 300:
            return None
        sig = base64.b64decode(signature, validate=True)
    except (ValueError, TypeError):
        return None
    signed_message = timestamp.encode("ascii") + b"|" + raw
    for acct, conf in TELNYX_ACCOUNTS.items():
        if not conf["public_key"]:
            continue
        try:
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(conf["public_key"], validate=True))
            key.verify(sig, signed_message)
        except (ValueError, TypeError, InvalidSignature):
            continue
        tx_use(acct)
        return acct
    return None

def _cs_encode(text):
    return base64.b64encode(text.encode()).decode()

def _cs_decode(value):
    if not value:
        return ""
    try:
        return base64.b64decode(value, validate=True).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""

def _telnyx_reject_declined(call_control_id):
    """Refuse an unanswered inbound call with a final decline (SIP 603)."""
    try:
        _telnyx_request("POST", f"/v2/calls/{call_control_id}/actions/reject", {"cause": "CALL_REJECTED"})
    except Exception:
        _telnyx_hangup_quietly(call_control_id)

def _telnyx_hangup_quietly(call_control_id):
    try:
        _telnyx_request("POST", f"/v2/calls/{call_control_id}/actions/hangup", {})
    except Exception:
        log.info("Hangup for %s was not accepted (call probably already ended)", call_control_id)

def _telnyx_start_voicemail(call_control_id, group_id):
    """Answer the parked inbound leg and send the caller to voicemail."""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.incoming_call_groups SET status='missed'
                WHERE id=%s AND status='ringing' RETURNING call_log_id""", (group_id,))
            row = cur.fetchone()
            if not row:
                return False
            cur.execute("""UPDATE public.call_participants SET status='cancelled'
                WHERE group_id=%s AND status='ringing'""", (group_id,))
            if row["call_log_id"]:
                cur.execute("UPDATE public.calls SET status='missed' WHERE id=%s", (row["call_log_id"],))
            rg.mark_missed(cur, group_id)          # shared number: every member's log
    try:
        _telnyx_request("POST", f"/v2/calls/{call_control_id}/actions/answer",
                        {"client_state": _cs_encode(f"vm:{group_id}")})
        return True
    except Exception as exc:
        log.warning("Voicemail answer failed for group %s: %s", group_id, getattr(exc, "detail", exc))
    # The answer failed. Usually the call was answered a moment earlier (the agent
    # picked up while an old ringing leg was ending). Never cut a live call: if it
    # is still up, undo "missed" and leave it connected.
    alive = False
    try:
        st = _telnyx_request("GET", f"/v2/calls/{call_control_id}")
        alive = bool(((st or {}).get("data") or {}).get("is_alive"))
    except Exception:
        alive = False
    if alive:
        log.info("Voicemail skipped for group %s: the call is already connected", group_id)
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE public.incoming_call_groups SET status='answered'
                    WHERE id=%s AND status='missed' RETURNING call_log_id""", (group_id,))
                row = cur.fetchone()
                if row and row["call_log_id"]:
                    cur.execute("UPDATE public.calls SET status='answered' WHERE id=%s AND status='missed'",
                                (row["call_log_id"],))
        return False
    _telnyx_hangup_quietly(call_control_id)
    return False

def _telnyx_voicemail_answered(call_control_id, group_id):
    """Caller already answered (was waiting on hold): play the greeting, then record."""
    try:
        _telnyx_request("POST", f"/v2/calls/{call_control_id}/actions/speak", {
            "payload": VOICEMAIL_GREETING, "voice": "female", "language": "en-US",
            "client_state": _cs_encode(f"vm:{group_id}")})
    except Exception:
        log.exception("Voicemail greeting failed for queued group %s", group_id)
        _telnyx_hangup_quietly(call_control_id)

def _telnyx_save_voicemail(group_id, mp3_url, duration, provider_ref, call_control_id):
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT c.id AS call_id,c.user_id,g.caller_number,g.destination_number
                    FROM public.incoming_call_groups g JOIN public.calls c ON c.id=g.call_log_id
                    WHERE g.id=%s""", (group_id,))
                row = cur.fetchone()
        if not row:
            log.warning("Voicemail for unknown call group %s ignored", group_id)
            return
        data, ctype = vh.http_bytes(mp3_url, max_bytes=MAX_RECORDING_BYTES)
        with db_connect() as conn:
            with conn.cursor() as cur:
                targets = rg.voicemail_targets(cur, group_id) or [(row["user_id"], row["call_id"])]
        for n, (uid, log_id) in enumerate(targets):
            # Shared number: every member gets the voicemail in their list.
            _store_recording(uid, "voicemail", data, "audio/mpeg", call_id=log_id,
                             provider="telnyx", from_number=row["caller_number"] or "",
                             to_number=row["destination_number"] or "", duration=duration,
                             provider_ref=provider_ref if n == 0 else f"{provider_ref}:{uid}")
    except Exception:
        log.exception("Saving Telnyx voicemail failed for group %s", group_id)
    finally:
        _telnyx_hangup_quietly(call_control_id)

def _seconds_between(start, end):
    try:
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        return max(0, int((b - a).total_seconds()))
    except (TypeError, ValueError):
        return 0

def _handle_voicemail_event(event, payload, group_id, background):
    call_id = payload.get("call_control_id")
    state = _cs_encode(f"vm:{group_id}")
    if not call_id:
        return
    if event == "call.answered":
        try:
            _telnyx_request("POST", f"/v2/calls/{call_id}/actions/speak", {
                "payload": VOICEMAIL_GREETING, "voice": "female", "language": "en-US",
                "client_state": state})
        except Exception:
            log.exception("Voicemail greeting failed; recording without greeting")
            event = "call.speak.ended"
    if event == "call.speak.ended":
        try:
            _telnyx_request("POST", f"/v2/calls/{call_id}/actions/record_start", {
                "format": "mp3", "channels": "single", "play_beep": True,
                "max_length": VOICEMAIL_MAX_SECONDS, "client_state": state})
        except Exception:
            log.exception("Voicemail record_start failed")
            _telnyx_hangup_quietly(call_id)
    elif event == "call.recording.saved":
        urls = payload.get("recording_urls") or {}
        mp3 = urls.get("mp3") or (payload.get("public_recording_urls") or {}).get("mp3")
        if mp3:
            duration = _seconds_between(payload.get("recording_started_at"), payload.get("recording_ended_at"))
            ref = "telnyx:" + str(payload.get("recording_id") or payload.get("call_leg_id") or call_id)
            _bg(background, _telnyx_save_voicemail, group_id, mp3, duration, ref, call_id)

ct.setup(db_connect=db_connect, telnyx=_telnyx_request, cs_encode=_cs_encode,
         conf=tx_conf, current_account=tx_current, account_ctx=tx_ctx,
         call_allowed=call_allowed, call_block_message=call_block_message,
         base_url=PUBLIC_BASE_URL, presence_timeout=40, should_run=worker_should_run,
         no_worker=os.getenv("TRANSFER_WORKER", "1") == "0")

def _line_ctx(provider, acct):
    return tw_ctx(acct or "w1") if provider == "twilio" else tx_ctx(acct or "t1")

rg.setup(db_connect=db_connect, telnyx=_telnyx_request, cs_encode=_cs_encode,
         quiet=lambda m, p, b=None: ct._quiet(m, p, b), hangup=_telnyx_hangup_quietly,
         voice_app=lambda: tx_conf()["voice_app_id"], hold_music_url=ct.hold_music_url,
         free=ct._free, on_ring_answered=ct.on_ring_answered, agent_leg_gone=ct.agent_leg_gone,
         voicemail_parked=_telnyx_start_voicemail, voicemail_answered=_telnyx_voicemail_answered,
         current_account=lambda provider: tw_current() if provider == "twilio" else tx_current(),
         account_ctx=_line_ctx, should_run=worker_should_run,
         no_worker=os.getenv("TRANSFER_WORKER", "1") == "0")

def _voicemail_after_ring_end(call_control_id, group_id):
    time.sleep(1.5)                     # runs in the background, never in the webhook request
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM public.incoming_call_groups WHERE id=%s", (group_id,))
            g = cur.fetchone()
    if g and g["status"] == "ringing":
        _telnyx_start_voicemail(call_control_id, group_id)

def _handle_ring_leg_event(event, group_id, background, payload=None):
    """Events from the SIP leg that rings the agent's browser."""
    ring_leg = (payload or {}).get("call_control_id") or ""
    if event == "call.answered" and ring_leg:
        ct.on_ring_answered(group_id, ring_leg)
    with db_connect() as conn:
        with conn.cursor() as cur:
            if event in ("call.answered", "call.bridged"):
                cur.execute("""UPDATE public.incoming_call_groups SET status='answered'
                    WHERE id=%s AND status='ringing' RETURNING call_log_id""", (group_id,))
                row = cur.fetchone()
                if row and row["call_log_id"]:
                    cur.execute("UPDATE public.calls SET status='answered' WHERE id=%s AND status='ringing'",
                                (row["call_log_id"],))
                return
            if event != "call.hangup":
                return
            cur.execute("SELECT status,telnyx_call_id FROM public.incoming_call_groups WHERE id=%s", (group_id,))
            group = cur.fetchone()
    if not group:
        return
    if group["status"] == "ringing":
        # Agent did not pick up (timeout, rejected, offline): go to voicemail.
        # A duplicate ringing leg can end at the same moment the agent answers
        # another one; wait a moment and re-check before sending to voicemail.
        _bg(background, _voicemail_after_ring_end, group["telnyx_call_id"], group_id)
    elif group["status"] in ("answered", "transferred"):
        # Agent hung up first: do not leave the caller in silence -- unless the
        # call was transferred and another agent is talking to the caller now.
        try:
            someone_else = ct.agent_leg_gone(ring_leg)
        except Exception:
            log.exception("Transfer check failed for group %s", group_id)
            someone_else = group["status"] == "transferred"
        if not someone_else:
            _bg(background, _telnyx_hangup_quietly, group["telnyx_call_id"])

@app.post("/webhooks/telnyx")
async def telnyx_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not verify_telnyx_webhook(
        raw,
        request.headers.get("telnyx-signature-ed25519", ""),
        request.headers.get("telnyx-timestamp", ""),
    ):
        raise HTTPException(status_code=401, detail="Invalid Telnyx webhook signature")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    data = body.get("data") or {}
    payload = data.get("payload") or {}
    event = data.get("event_type") or ""
    if event.startswith("message."):
        # Messaging webhooks may also be pointed at this URL.
        try:
            _handle_telnyx_sms_event(event, payload)
        except Exception:
            log.exception("SMS webhook handling failed")
        return JSONResponse({"ok": True})
    client_state = _cs_decode(payload.get("client_state"))
    try:
        if ct.handle_webhook(event, payload, client_state):
            return JSONResponse({"ok": True})
    except Exception:
        log.exception("Transfer webhook handling failed (%s)", event)
    if client_state.startswith("vm:"):
        _handle_voicemail_event(event, payload, client_state[3:], background)
        if event != "call.hangup":
            return JSONResponse({"ok": True})
    elif client_state.startswith("ring:"):
        _handle_ring_leg_event(event, client_state[5:], background, payload)
        return JSONResponse({"ok": True})
    elif client_state.startswith("rg:"):
        # Shared number: the leg ringing one member's browser.
        try:
            gid, uid = client_state[3:].split(":", 1)
            uuid.UUID(gid); uuid.UUID(uid)
            rg.on_ring_event(event, gid, uid, payload)
        except ValueError:
            pass
        except Exception:
            log.exception("Ring-group event failed (%s)", event)
        return JSONResponse({"ok": True})
    elif client_state.startswith("q:"):
        # Shared number: caller waiting on hold for a free member.
        try:
            uuid.UUID(client_state[2:])
            if rg.on_queue_event(event, client_state[2:], payload):
                return JSONResponse({"ok": True})
        except ValueError:
            return JSONResponse({"ok": True})
        except Exception:
            log.exception("Ring-group queue event failed (%s)", event)
    # Single-number/single-user Voice API routing.
    # Telnyx Voice API originates a SIP leg to the assigned WebRTC credential,
    # automatically bridging it to the inbound leg once answered.
    # Only the first, idempotently inserted call.initiated event originates a leg.
    if event in ("call.initiated", "call.answered", "call.bridged", "call.hangup"):
        call_id = payload.get("call_control_id")
        destination = payload.get("to") or ""
        origin = payload.get("from") or ""
        direction = str(payload.get("direction") or "").lower()
        if call_id and event == "call.initiated" and direction == "incoming" and destination:
            ring_user = None
            voicemail_ok = False
            group_id = None
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO public.incoming_call_groups
                        (telnyx_call_id,caller_number,destination_number,status)
                        VALUES (%s,%s,%s,'ringing')
                        ON CONFLICT(telnyx_call_id) DO NOTHING RETURNING id""",
                        (call_id, origin, destination))
                    inserted = cur.fetchone()
                    retry_of_declined = False
                    if inserted and origin:
                        # Same caller -> same number again within 20 s of a decline is
                        # the caller's network retrying: refuse it instead of ringing.
                        cur.execute("""SELECT 1 FROM public.incoming_call_groups
                            WHERE caller_number=%s AND destination_number=%s AND status='declined'
                              AND COALESCE(ended_at, created_at) > NOW() - INTERVAL '20 seconds'
                            LIMIT 1""", (origin, destination))
                        if cur.fetchone():
                            retry_of_declined = True
                            cur.execute("""UPDATE public.incoming_call_groups
                                SET status='declined', ended_at=NOW() WHERE id=%s""", (inserted["id"],))
                    shared_rows = None
                    if inserted and not retry_of_declined:
                        group_id = inserted["id"]
                        rows = rg.members(cur, destination, "telnyx")
                        if rg.is_shared(rows):
                            shared_rows = rows
                    if inserted and not retry_of_declined and shared_rows is None:
                        cur.execute("""SELECT u.id,u.email,u.sip_username,u.credential_id,
                            u.dnd_enabled,u.plan_renewal FROM public.caller_id_assignments a
                            JOIN public.voxsbridge_users u ON u.id=a.user_id
                            WHERE a.phone_number=%s AND u.is_active=TRUE
                            AND COALESCE(u.provider,'telnyx')='telnyx'
                            LIMIT 1""", (destination,))
                        recipient = cur.fetchone()
                        if recipient and (
                            not recipient["plan_renewal"] or recipient["plan_renewal"] >= datetime.now().date()
                        ):
                            # Idempotent incoming call log; no duplicate records on webhook retries.
                            cur.execute("""INSERT INTO public.calls
                                (user_id,from_number,to_number,direction,status,provider,provider_call_id,created_at)
                                VALUES (%s,%s,%s,'incoming','ringing','telnyx',%s,NOW()) RETURNING id""",
                                (recipient["id"], origin or 'Unknown', destination, call_id))
                            new_log_id = cur.fetchone()['id']
                            cur.execute("UPDATE public.incoming_call_groups SET call_log_id=%s WHERE id=%s",
                                        (new_log_id, group_id))
                            voicemail_ok = True
                            if not recipient["dnd_enabled"]:
                                cur.execute("""INSERT INTO public.call_participants
                                    (group_id,user_id,status) VALUES (%s,%s,'ringing')
                                    ON CONFLICT(group_id,user_id) DO NOTHING""",
                                    (group_id,recipient["id"]))
                                ring_user = recipient
            if retry_of_declined:
                _bg(background, _telnyx_reject_declined, call_id)
            if group_id and shared_rows is not None:
                try:
                    rg.telnyx_start(group_id, call_id, origin, destination, shared_rows)
                except Exception:
                    log.exception("Ring group start failed for %s", group_id)
                    _bg(background, _telnyx_start_voicemail, call_id, group_id)
            elif group_id:
                # Do not create new SIP credentials in a webhook: the user must
                # have logged in once and obtained a WebRTC credential already.
                sip_username = (ring_user or {}).get("sip_username", "")
                if sip_username and tx_conf()["api_key"] and tx_conf()["voice_app_id"]:
                    try:
                        ring_resp = _telnyx_request("POST", "/v2/calls", {
                            "connection_id": tx_conf()["voice_app_id"],
                            "to": f"sip:{sip_username}@sip.telnyx.com",
                            "from": origin or destination,
                            "link_to": call_id,
                            "bridge_intent": True,
                            "bridge_on_answer": True,
                            "prevent_double_bridge": True,
                            "park_after_unbridge": "self",   # live listening can move the caller
                            "timeout_secs": 30,
                            "command_id": str(group_id),
                            "client_state": _cs_encode(f"ring:{group_id}"),
                        })
                        # Remember the browser leg so Decline can stop it too
                        # (otherwise Telnyx keeps re-offering it for up to 30 s).
                        ring_id = ((ring_resp or {}).get("data") or {}).get("call_control_id")
                        if ring_id:
                            try:
                                with db_connect() as conn:
                                    with conn.cursor() as cur:
                                        cur.execute("UPDATE public.incoming_call_groups SET ring_call_id=%s WHERE id=%s",
                                                    (ring_id, group_id))
                            except Exception:
                                log.exception("Could not store ring leg for group %s", group_id)
                    except Exception:
                        # Do not expose provider credentials or error bodies in responses.
                        log.exception("Inbound SIP dial failed for call group %s", group_id)
                        _bg(background, _telnyx_start_voicemail, call_id, group_id)
                elif voicemail_ok and tx_conf()["api_key"]:
                    # Agent is on Do Not Disturb or has never logged in: voicemail.
                    _bg(background, _telnyx_start_voicemail, call_id, group_id)
                else:
                    # Unassigned number or expired plan. Do not leave a caller
                    # ringing indefinitely.
                    with db_connect() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""UPDATE public.incoming_call_groups
                                SET status='missed',ended_at=NOW() WHERE id=%s""", (group_id,))
                    _bg(background, _telnyx_hangup_quietly, call_id)
        if call_id and event in ("call.answered", "call.bridged", "call.hangup"):
            with db_connect() as conn:
                with conn.cursor() as cur:
                    # Linked SIP leg webhooks can have their own call control id.
                    # For now, only update the inbound leg's stored id.
                    if event in ("call.answered", "call.bridged"):
                        # Shared numbers are marked answered by the member's own leg (ring_group).
                        cur.execute("""UPDATE public.incoming_call_groups SET status='answered'
                            WHERE telnyx_call_id=%s AND status='ringing' AND NOT is_shared""", (call_id,))
                        cur.execute("""UPDATE public.calls c SET status='answered'
                            FROM public.incoming_call_groups g WHERE g.call_log_id=c.id
                            AND g.telnyx_call_id=%s AND c.status='ringing' AND NOT g.is_shared""", (call_id,))
                    else:
                        try:
                            rg.customer_gone(call_id)      # shared number: stop every ringing member
                        except Exception:
                            log.exception("Ring-group cleanup failed for %s", call_id)
                        cur.execute("""UPDATE public.incoming_call_groups
                            SET status=CASE WHEN status='ringing' THEN 'missed'
                                WHEN status='answered' THEN 'ended' ELSE status END,
                                ended_at=NOW()
                            WHERE telnyx_call_id=%s AND status IN ('ringing','answered')""", (call_id,))
                        cur.execute("""UPDATE public.calls c SET
                            status=CASE WHEN c.status='answered' THEN 'completed'
                                WHEN c.status='ringing' THEN 'missed' ELSE c.status END,
                            duration_seconds=CASE WHEN c.status='answered' THEN
                                GREATEST(0,EXTRACT(EPOCH FROM (NOW()-c.created_at))::integer)
                                ELSE COALESCE(c.duration_seconds,0) END
                            FROM public.incoming_call_groups g
                            WHERE g.call_log_id=c.id AND g.telnyx_call_id=%s""", (call_id,))
    return JSONResponse({"ok": True})


# ==================== TWILIO WEBHOOKS & TOKEN ====================
async def _twilio_params(request: Request):
    """Read Twilio's form POST and verify X-Twilio-Signature."""
    if not twilio_enabled():
        raise HTTPException(status_code=503, detail="Twilio not configured")
    form = await request.form()
    params = {}
    for key, value in form.multi_items():
        params.setdefault(key, []).append(str(value))
    query = request.url.query
    path = request.url.path + ("?" + query if query else "")
    candidates = [PUBLIC_BASE_URL + path, str(request.url),
                  str(request.url).replace("http://", "https://", 1)]
    signature = request.headers.get("x-twilio-signature", "")
    # The account whose Auth Token signed the request is the account of this call.
    for acct, conf in TWILIO_ACCOUNTS.items():
        if conf["token"] and vh.twilio_signature_valid(conf["token"], candidates, params, signature):
            _TW.set(acct)
            return {k: v[0] for k, v in params.items()}
    raise HTTPException(status_code=403, detail="Invalid Twilio signature")

def _twiml_response(*verbs):
    return Response(vh.twiml(*verbs), media_type="application/xml")

def _say(text):
    return f'<Say voice="alice" language="en-US">{vh.xml_attr(text)}</Say>'

def _twilio_voicemail_twiml(log_id):
    callback = f"/twilio/recording?log={log_id}"
    return (_say(VOICEMAIL_GREETING) +
            f'<Record maxLength="{VOICEMAIL_MAX_SECONDS}" playBeep="true" trim="trim-silence" '
            f'action="/twilio/voicemail-done" method="POST" '
            f'recordingStatusCallback="{vh.xml_attr(callback)}" recordingStatusCallbackMethod="POST"/>')

def _db_user_by_id(user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM public.voxsbridge_users WHERE id=%s AND is_active=TRUE", (user_id,))
            row = cur.fetchone()
    return _db_user(row) if row else None

@app.get("/api/twilio-token")
@_customer_safe
async def twilio_token(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if user.get("role") != "admin" and plan_status(user)["days_left"] < 0:
        raise HTTPException(status_code=402, detail="Subscription expired")
    if user.get("provider") != "twilio":
        raise HTTPException(status_code=409, detail="This account does not use Twilio")
    acct = user.get("twilio_account") or "w1"
    if not twilio_enabled(acct):
        raise HTTPException(status_code=503, detail=f"{tw_conf(acct)['label']} is not configured on the server")
    def _tw_cfg():
        with tw_ctx(acct):
            return _twilio_voice_config(), tw_conf()["sid"]
    (key_sid, key_secret, app_sid), account_sid = await run_in_threadpool(_tw_cfg)
    identity = vh.identity_for_user(user["id"])
    token = vh.twilio_access_token(account_sid, key_sid, key_secret, identity, app_sid, ttl=3600)
    return JSONResponse({"token": token, "identity": identity, "from_number": user.get("phone_number") or ""},
                        headers={"Cache-Control": "no-store"})

@app.post("/twilio/voice/outgoing")   # alias: a TwiML App configured with this URL works too
@app.post("/twilio/outgoing")
@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """TwiML App Voice URL: outgoing calls placed from the browser."""
    params = await _twilio_params(request)
    user_id = vh.user_id_from_identity(params.get("From") or params.get("Caller") or "")
    user = _db_user_by_id(user_id) if user_id else None
    to_number = re.sub(r"[\s().-]", "", params.get("To") or "")
    if not user or user.get("provider") != "twilio":
        return _twiml_response(_say("This account is not allowed to place calls."), "<Hangup/>")
    if user.get("role") != "admin" and plan_status(user)["days_left"] < 0:
        return _twiml_response(_say("Your subscription has expired."), "<Hangup/>")
    if not re.fullmatch(r"\+[1-9]\d{6,14}", to_number):
        return _twiml_response(_say("The number you dialed is not valid."), "<Hangup/>")
    if not user.get("phone_number"):
        return _twiml_response(_say("No caller ID is assigned to this account."), "<Hangup/>")
    if not call_allowed(user, to_number):
        return _twiml_response(_say("This number cannot be called from your line."), "<Hangup/>")
    return _twiml_response(
        f'<Dial callerId="{vh.xml_attr(user["phone_number"])}" answerOnBridge="true" timeLimit="14400">'
        f'<Number>{vh.xml_attr(to_number)}</Number></Dial>')

# ---- Shared Twilio numbers (ring every free member, first to answer wins)
def _tw_url(path):
    return vh.xml_attr(PUBLIC_BASE_URL + path)

def _twilio_group_ring_twiml(gid, users, logs):
    parts = []
    for u in users:
        cb = _tw_url("/twilio/group-answered?g=%s&u=%s" % (gid, u["id"]))
        ident = vh.identity_for_user(u["id"])
        log_id = logs.get(str(u["id"])) or ""
        parts.append('<Client statusCallbackEvent="answered" statusCallbackMethod="POST" '
                     'statusCallback="%s"><Identity>%s</Identity>'
                     '<Parameter name="logId" value="%s"/></Client>' % (cb, ident, log_id))
    clients = "".join(parts)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.call_participants SET status='ringing', updated_at=now()
                WHERE group_id=%s AND user_id = ANY(%s)""", (gid, [u["id"] for u in users]))
    return (f'<Dial timeout="{rg.RING_SECONDS}" answerOnBridge="true" method="POST" '
            'action="%s">%s</Dial>' % (_tw_url("/twilio/group-dial?g=" + gid), clients))

def _twilio_group_hold_twiml(gid, part=1):
    nxt = 2 if part == 1 else 1
    return ('<Play>%s</Play><Redirect method="POST">%s</Redirect>'
            % (_tw_url("/static/audio/hold-part%d.mp3" % part), _tw_url("/twilio/group-queue?g=%s&p=%d" % (gid, nxt))))

def _twilio_group_logs(gid):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id,call_log_id FROM public.call_participants WHERE group_id=%s", (gid,))
            return {str(r["user_id"]): r["call_log_id"] for r in cur.fetchall()}

def _twilio_group_voicemail(gid):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.incoming_call_groups SET status='missed', ended_at=now()
                WHERE id=%s AND status IN ('ringing','queued') RETURNING call_log_id""", (gid,))
            row = cur.fetchone()
            rg.mark_missed(cur, gid)
            if not row:
                cur.execute("SELECT call_log_id FROM public.incoming_call_groups WHERE id=%s", (gid,))
                row = cur.fetchone()
    if not row or not row["call_log_id"]:
        return _twiml_response(_say("Sorry, nobody is available. Goodbye."), "<Hangup/>")
    return _twiml_response(_twilio_voicemail_twiml(row["call_log_id"]))

def _twilio_group_incoming(params, rows):
    to_number = params.get("To", "")
    from_number = params.get("From", "") or "Unknown"
    call_sid = params.get("CallSid") or ""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.incoming_call_groups
                (telnyx_call_id,caller_number,destination_number,status,provider)
                VALUES (%s,%s,%s,'ringing','twilio') ON CONFLICT(telnyx_call_id) DO NOTHING RETURNING id""",
                (call_sid, from_number, to_number))
            ins = cur.fetchone()
            if not ins:
                return _twiml_response("<Hangup/>")          # duplicate webhook
            gid = str(ins["id"])
            logs = rg.create_group_logs(cur, gid, rows, from_number, to_number, "twilio", call_sid)
            free, busy = rg.classify(cur, rows, gid, "twilio")
    if not logs:
        return _twiml_response(_say("This number is not available right now. Goodbye."), "<Hangup/>")
    if free:
        return _twiml_response(_twilio_group_ring_twiml(gid, free, logs))
    if busy:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE public.incoming_call_groups SET status='queued',
                    queue_deadline=now() + make_interval(secs => %s) WHERE id=%s""", (rg.QUEUE_SECONDS, gid))
        return _twiml_response(_twilio_group_hold_twiml(gid))
    return _twilio_group_voicemail(gid)

def _gid_param(request):
    try:
        return str(uuid.UUID(request.query_params.get("g", "")))
    except ValueError:
        return ""

@app.post("/twilio/group-answered")
async def twilio_group_answered(request: Request):
    """A member's browser answered: they win, everybody else's log is removed."""
    params = await _twilio_params(request)
    gid = _gid_param(request)
    try:
        uid = str(uuid.UUID(request.query_params.get("u", "")))
    except ValueError:
        uid = ""
    if gid and uid and params.get("CallStatus") in ("in-progress", "answered", ""):
        with db_connect() as conn:
            with conn.cursor() as cur:
                rg.claim(cur, gid, uid)
    return Response(status_code=204)

@app.post("/twilio/group-dial")
async def twilio_group_dial(request: Request):
    """End of the <Dial> that rang the free members."""
    params = await _twilio_params(request)
    gid = _gid_param(request)
    if not gid:
        return _twiml_response("<Hangup/>")
    status = params.get("DialCallStatus", "")
    duration = int(params.get("DialCallDuration") or 0) if str(params.get("DialCallDuration") or "0").isdigit() else 0
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status,call_log_id,queue_deadline < now() AS late, queue_deadline IS NOT NULL AS was_queued "
                        "FROM public.incoming_call_groups WHERE id=%s", (gid,))
            g = cur.fetchone()
            if g and g["status"] == "answered":
                cur.execute("UPDATE public.incoming_call_groups SET status='ended', ended_at=now() WHERE id=%s", (gid,))
                if g["call_log_id"]:
                    cur.execute("UPDATE public.calls SET status='completed', duration_seconds=%s WHERE id=%s",
                                (duration, g["call_log_id"]))
    if not g or g["status"] in ("answered", "ended") or status in ("completed", "canceled"):
        if g and g["status"] in ("ringing", "queued") and status == "canceled":
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE public.incoming_call_groups SET status='missed', ended_at=now() WHERE id=%s", (gid,))
                    rg.mark_missed(cur, gid)
        return _twiml_response("<Hangup/>")
    if g["was_queued"] and not g["late"]:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE public.call_participants SET status='waiting', updated_at=now()
                    WHERE group_id=%s AND status='ringing'""", (gid,))
                cur.execute("UPDATE public.incoming_call_groups SET status='queued' WHERE id=%s", (gid,))
        return _twiml_response(_twilio_group_hold_twiml(gid))
    return _twilio_group_voicemail(gid)       # nobody picked up

@app.post("/twilio/group-queue")
async def twilio_group_queue(request: Request):
    """Caller on hold: ring the first member who is free, or voicemail after 3 min."""
    await _twilio_params(request)
    gid = _gid_param(request)
    part = 2 if request.query_params.get("p") == "2" else 1
    if not gid:
        return _twiml_response("<Hangup/>")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, queue_deadline < now() AS late FROM public.incoming_call_groups WHERE id=%s", (gid,))
            g = cur.fetchone()
            if not g or g["status"] != "queued":
                return _twiml_response("<Hangup/>")
            if g["late"]:
                free = None
            else:
                cur.execute("""SELECT u.id,u.email,u.name,u.sip_username,u.dnd_enabled,u.plan_renewal,
                        u.account_id,u.created_at,u.twilio_account,u.telnyx_account
                    FROM public.call_participants p JOIN public.voxsbridge_users u ON u.id=p.user_id
                    WHERE p.group_id=%s AND u.is_active""", (gid,))
                free, _busy = rg.classify(cur, cur.fetchall(), gid, "twilio")
    if free is None:
        return _twilio_group_voicemail(gid)
    if free:
        return _twiml_response(_twilio_group_ring_twiml(gid, free, _twilio_group_logs(gid)))
    return _twiml_response(_twilio_group_hold_twiml(gid, part))

@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Voice webhook of each Twilio number: ring the assigned user's browser."""
    params = await _twilio_params(request)
    to_number = params.get("To", "")
    from_number = params.get("From", "") or "Unknown"
    with db_connect() as conn:
        with conn.cursor() as cur:
            shared = rg.members(cur, to_number, "twilio")
    if rg.is_shared(shared):
        return _twilio_group_incoming(params, shared)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT u.* FROM public.caller_id_assignments a
                JOIN public.voxsbridge_users u ON u.id=a.user_id
                WHERE a.phone_number=%s AND u.is_active=TRUE AND u.provider='twilio' LIMIT 1""", (to_number,))
            row = cur.fetchone()
    user = _db_user(row) if row else None
    if not user or (user.get("role") != "admin" and plan_status(user)["days_left"] < 0):
        return _twiml_response(_say("This number is not available right now. Goodbye."), "<Hangup/>")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.calls
                (user_id,from_number,to_number,direction,status,provider,provider_call_id,created_at)
                VALUES (%s,%s,%s,'incoming','ringing','twilio',%s,NOW()) RETURNING id""",
                (user["id"], from_number, to_number, params.get("CallSid")))
            log_id = cur.fetchone()["id"]
    if user.get("dnd_enabled"):
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE public.calls SET status='missed' WHERE id=%s", (log_id,))
        return _twiml_response(_twilio_voicemail_twiml(log_id))
    identity = vh.identity_for_user(user["id"])
    return _twiml_response(
        f'<Dial timeout="25" answerOnBridge="true" action="/twilio/dial-status?log={log_id}" method="POST">'
        f'<Client><Identity>{identity}</Identity>'
        f'<Parameter name="logId" value="{log_id}"/></Client></Dial>')

@app.post("/twilio/dial-status")
async def twilio_dial_status(request: Request):
    params = await _twilio_params(request)
    try:
        log_id = uuid.UUID(request.query_params.get("log", ""))
    except ValueError:
        return _twiml_response("<Hangup/>")
    status = params.get("DialCallStatus", "")
    duration = int(params.get("DialCallDuration") or 0) if str(params.get("DialCallDuration") or "0").isdigit() else 0
    with db_connect() as conn:
        with conn.cursor() as cur:
            if status == "completed":
                cur.execute("UPDATE public.calls SET status='completed',duration_seconds=%s WHERE id=%s",
                            (duration, log_id))
            else:
                cur.execute("UPDATE public.calls SET status='missed' WHERE id=%s", (log_id,))
    if status in ("completed", "canceled"):
        return _twiml_response("<Hangup/>")
    return _twiml_response(_twilio_voicemail_twiml(log_id))

@app.post("/twilio/voicemail-done")
async def twilio_voicemail_done(request: Request):
    await _twilio_params(request)
    return _twiml_response(_say("Thank you. Goodbye."), "<Hangup/>")

def _twilio_save_voicemail(log_id, recording_url, recording_sid, duration):
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id,user_id,from_number,to_number FROM public.calls WHERE id=%s", (log_id,))
                row = cur.fetchone()
        if not row:
            return
        c = tw_conf()
        data, _ = vh.http_bytes(recording_url + ".mp3", basic_auth=(c["sid"], c["token"]),
                                max_bytes=MAX_RECORDING_BYTES)
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT g.id FROM public.incoming_call_groups g WHERE g.call_log_id=%s AND g.is_shared
                    LIMIT 1""", (row["id"],))
                g = cur.fetchone()
                targets = rg.voicemail_targets(cur, g["id"]) if g else []
        targets = targets or [(row["user_id"], row["id"])]
        for n, (uid, call_id) in enumerate(targets):
            # Shared number: every member gets the voicemail.
            _store_recording(uid, "voicemail", data, "audio/mpeg", call_id=call_id,
                             provider="twilio", from_number=row["from_number"] or "",
                             to_number=row["to_number"] or "", duration=duration,
                             provider_ref="twilio:" + recording_sid + ("" if n == 0 else ":" + str(uid)))
    except Exception:
        log.exception("Saving Twilio voicemail failed for call %s", log_id)

@app.post("/twilio/recording")
async def twilio_recording(request: Request, background: BackgroundTasks):
    params = await _twilio_params(request)
    try:
        log_id = uuid.UUID(request.query_params.get("log", ""))
    except ValueError:
        return JSONResponse({"ok": True})
    url = params.get("RecordingUrl", "")
    sid = params.get("RecordingSid", "")
    if params.get("RecordingStatus", "completed") == "completed" and url.startswith("https://api.twilio.com/") and sid:
        duration = int(params.get("RecordingDuration") or 0) if str(params.get("RecordingDuration") or "0").isdigit() else 0
        _tw_bg(background, _twilio_save_voicemail, log_id, url, sid, duration)
    return JSONResponse({"ok": True})


# ==================== TWILIO TRANSFER ====================
# Blind transfer for Twilio lines: the customer's leg is redirected to new TwiML
# that rings the teammate's browser (or dials a phone number). Your own browser
# leg ends; nothing else on the account is touched.
def _twilio_targets(user):
    if not user.get("account_id"):
        return []
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,name,email,account_role FROM public.voxsbridge_users
                WHERE account_id=%s AND is_active AND id<>%s AND provider='twilio'
                  AND COALESCE(twilio_account,'w1')=%s AND COALESCE(phone_number,'')<>''
                ORDER BY lower(name)""", (user["account_id"], user["id"], user.get("twilio_account") or "w1"))
            rows = cur.fetchall()
            out = [{"id": str(r["id"]), "name": r["name"] or r["email"], "email": r["email"],
                    "role": r["account_role"], "status": ct._free(cur, r["id"])} for r in rows]
    order = {"free": 0, "busy": 1, "offline": 2}
    out.sort(key=lambda a: (order.get(a["status"], 3), a["name"].lower()))
    return out

@app.get("/api/twilio/transfer/targets")
async def twilio_transfer_targets(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"agents": _twilio_targets(user)}

def _twilio_customer_leg(user, call_sid):
    """The customer's call leg for this user's browser leg, after checking it is theirs."""
    leg = _twilio_request("GET", f"/Calls/{call_sid}.json")
    me = "client:" + vh.identity_for_user(user["id"])
    if me not in (str(leg.get("from") or ""), str(leg.get("to") or "")):
        raise HTTPException(status_code=403, detail="This is not your call")
    if leg.get("status") != "in-progress":
        raise HTTPException(status_code=409, detail="The call is not connected")
    if leg.get("parent_call_sid"):                 # incoming: customer is the parent leg
        return leg["parent_call_sid"], str(leg.get("from") or "")
    kids = _twilio_request("GET", f"/Calls.json?ParentCallSid={call_sid}&Status=in-progress&PageSize=5")
    for kid in kids.get("calls") or []:            # outgoing: customer is the dialed child leg
        return kid["sid"], str(kid.get("to") or "")
    raise HTTPException(status_code=409, detail="The other side is not connected yet")

@app.post("/api/twilio/transfer")
async def twilio_transfer(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if user.get("provider") != "twilio":
        raise HTTPException(status_code=409, detail="This line is not a Twilio line")
    data = await request.json()
    call_sid = str(data.get("call_sid") or "")
    if not re.fullmatch(r"CA[0-9a-f]{32}", call_sid):
        raise HTTPException(status_code=422, detail="Call not found")
    to_user = str(data.get("to_user") or "")
    to_number = re.sub(r"[\s().-]", "", str(data.get("to_number") or ""))
    with tw_ctx(user.get("twilio_account") or "w1"):
        if to_user:
            target = next((a for a in _twilio_targets(user) if a["id"] == to_user), None)
            if not target:
                raise HTTPException(status_code=404, detail="Agent not found")
            if target["status"] == "offline":
                raise HTTPException(status_code=409, detail=f"{target['name']} is offline right now")
            customer, customer_number = _twilio_customer_leg(user, call_sid)
            caller = _clean_number(data.get("number")) or _clean_number(customer_number) or "Unknown"
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT phone_number FROM public.voxsbridge_users WHERE id=%s", (target["id"],))
                    tnum = (cur.fetchone() or {}).get("phone_number") or ""
                    cur.execute("""INSERT INTO public.calls
                        (user_id,from_number,to_number,direction,status,provider,provider_call_id,created_at)
                        VALUES (%s,%s,%s,'incoming','ringing','twilio',%s,NOW()) RETURNING id""",
                        (target["id"], caller, tnum, customer))
                    log_id = cur.fetchone()["id"]
            twiml = vh.twiml(
                f'<Dial timeout="25" answerOnBridge="true" action="/twilio/dial-status?log={log_id}" method="POST">'
                f'<Client><Identity>{vh.identity_for_user(target["id"])}</Identity>'
                f'<Parameter name="logId" value="{log_id}"/></Client></Dial>')
            name = target["name"]
        elif to_number:
            if not re.fullmatch(r"\+[1-9]\d{6,14}", to_number):
                raise HTTPException(status_code=422, detail="Enter a valid number with country code")
            if not call_allowed(user, to_number):
                raise HTTPException(status_code=403, detail=call_block_message(user, to_number))
            if not user.get("phone_number"):
                raise HTTPException(status_code=409, detail="No caller ID on this account")
            customer, _num = _twilio_customer_leg(user, call_sid)
            twiml = vh.twiml(
                f'<Dial callerId="{vh.xml_attr(user["phone_number"])}" answerOnBridge="true" timeout="30" timeLimit="14400">'
                f'<Number>{vh.xml_attr(to_number)}</Number></Dial>')
            name = to_number
        else:
            raise HTTPException(status_code=422, detail="Pick an agent or enter a number")
        _twilio_request("POST", f"/Calls/{customer}.json", {"Twiml": twiml})
    log.info("Twilio transfer by %s to %s", user["email"], name)
    return {"ok": True, "to_name": name}


@app.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not has_power(user):
        return locked_page(request, user, "Campaigns")
    return templates.TemplateResponse(request=request, name="campaigns.html", context={
        "user": user,
        "campaigns": campaigns_db.get(user["email"], []),
        "premium": has_recording(user) or user.get("signup_source") == "website",
    })

@app.post("/api/campaigns")
async def create_campaign(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not has_power(user):
        raise HTTPException(status_code=403, detail="Campaigns are an optional add-on for your plan")
    if not (has_recording(user) or user.get("signup_source") == "website"):
        raise HTTPException(status_code=402, detail="Power Dialer, CRM & Campaigns are an add-on. Ask your admin to enable it.")
    data = await request.json()
    if user["email"] not in campaigns_db:
        campaigns_db[user["email"]] = []
    campaign = {
        "id": secrets.token_hex(6),
        "name": data.get("name", "Untitled"),
        "numbers": data.get("numbers", []),
        "status": "draft",
        "created_at": datetime.now().isoformat(),
        "dialed": 0
    }
    campaigns_db[user["email"]].append(campaign)
    return JSONResponse({"success": True, "campaign": campaign})

# Scripts are stored in PostgreSQL, not the Render ephemeral filesystem.
def _load_scripts():
    result = {}
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT u.email,s.id,s.title,s.category,s.content,s.is_default
                FROM public.scripts s JOIN public.voxsbridge_users u ON u.id=s.user_id""")
            for row in cur.fetchall():
                result.setdefault(row["email"],[]).append({
                    "id":str(row["id"]),"title":row["title"],
                    "category":row["category"] or "","content":row["content"],
                    "default":row["is_default"]})
    return result

def _save_scripts(data):
    # This method is used only by the scripts routes below. Apply user-scoped changes.
    with db_connect() as conn:
        with conn.cursor() as cur:
            for email,items in data.items():
                cur.execute("SELECT id FROM public.voxsbridge_users WHERE email=%s",(email,))
                row=cur.fetchone()
                if not row:
                    continue
                uid=row["id"]
                ids=[uuid.UUID(item["id"]) for item in items]
                cur.execute("DELETE FROM public.scripts WHERE user_id=%s AND NOT(id = ANY(%s::uuid[]))",(uid,ids))
                for item in items:
                    cur.execute("""INSERT INTO public.scripts(id,user_id,title,category,content,is_default)
                        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET
                        title=EXCLUDED.title,category=EXCLUDED.category,content=EXCLUDED.content,
                        is_default=EXCLUDED.is_default,updated_at=NOW()""",(
                        uuid.UUID(item["id"]),uid,item["title"],item["category"],item["content"],bool(item.get("default"))))

@app.get("/scripts", response_class=HTMLResponse)
async def scripts_page(request: Request, edit: str = ""):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    all_scripts = _load_scripts().get(user["email"], [])
    current = next((item for item in all_scripts if item["id"] == edit), None)
    return templates.TemplateResponse(request=request, name="scripts.html", context={"user": user, "saved_scripts": all_scripts, "current_script": current})

@app.post("/scripts/save")
async def save_script(request: Request, title: str = Form(...), category: str = Form(""), content: str = Form(""), script_id: str = Form(""), is_default: Optional[str] = Form(None)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    title = title.strip()[:150]
    if not title:
        raise HTTPException(status_code=400, detail="Script title required")
    content = bleach.clean(content, tags=["p", "br", "div", "b", "strong", "i", "em", "u", "ul", "ol", "li", "a", "blockquote"], attributes={"a": ["href", "title"]}, protocols=["https"], strip=True)
    data = _load_scripts()
    items = data.setdefault(user["email"], [])
    existing = next((item for item in items if item["id"] == script_id), None)
    if is_default:
        for item in items:
            item["default"] = False
    if existing:
        existing.update(title=title, category=category.strip()[:80], content=content[:50000], default=bool(is_default))
    else:
        script_id = str(uuid.uuid4())
        items.append({"id": script_id, "title": title, "category": category.strip()[:80], "content": content[:50000], "default": bool(is_default)})
    _save_scripts({user["email"]: items})   # only this user's scripts
    return RedirectResponse(url="/scripts?edit=" + script_id, status_code=303)

@app.post("/scripts/delete")
async def delete_script(request: Request, script_id: str = Form(...)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    data = _load_scripts()
    data[user["email"]] = [item for item in data.get(user["email"], []) if item["id"] != script_id]
    _save_scripts({user["email"]: data[user["email"]]})
    return RedirectResponse(url="/scripts", status_code=303)

# ---------------------------------------------------------------- Text messages (SMS)
# Only users with the SMS add-on (admin switch) and their own Telnyx number can text.
# Messages are sent from the user's own calling number. Customers never see provider names.
TELNYX_SMS_PROFILE_ID = os.getenv("TELNYX_SMS_PROFILE_ID", "").strip()
SMS_DAILY_LIMIT = int(os.getenv("SMS_DAILY_LIMIT", "500"))
SMS_MAX_CHARS = 1600
SMS_UNAVAILABLE = "Message could not be sent. Please check the number and try again, or contact support."
_sms_burst = {}

def _sms_number(raw):
    value = re.sub(r"[\s().-]", "", str(raw or ""))
    if value.startswith("00"):
        value = "+" + value[2:]
    return value if re.fullmatch(r"\+[1-9]\d{7,14}", value) else ""

def _sms_segments(text):
    gsm = re.fullmatch(r"[A-Za-z0-9 \r\n@£$¥èéùìòÇØøÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ!\"#¤%&'()*+,\-./:;<=>?¡ÄÖÑÜ§¿äöñüà^{}\\\[~\]|€]*", text or "")
    per, single = (153, 160) if gsm else (67, 70)
    n = len(text or "")
    return 1 if n <= single else -(-n // per)

def _sms_row(r, contacts=None):
    local = _to_local(r["created_at"])
    return {"id": str(r["id"]), "direction": r["direction"], "body": r["body"] or "",
            "media": r.get("media") or [], "status": r.get("status") or "",
            "at": r["created_at"].isoformat() if r["created_at"] else "",
            "time": local.strftime("%d %b, %I:%M %p") if local else ""}

def _sms_require(request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    if not has_sms(user):
        raise HTTPException(status_code=403, detail="Text messages are not enabled on your account.")
    return user

def _telnyx_sms_send(from_number, to_number, text):
    """Returns (provider_id, status, error_detail)."""
    body = {"from": from_number, "to": to_number, "text": text,
            "webhook_url": PUBLIC_BASE_URL + "/webhooks/telnyx/sms"}
    profile = tx_conf()["sms_profile_id"]
    if profile:
        body["messaging_profile_id"] = profile
    status, data = vh.http_json("POST", "https://api.telnyx.com/v2/messages", body=body,
                                headers={"Authorization": f"Bearer {tx_conf()['api_key']}"})
    if status >= 400 or not isinstance(data, dict):
        errors = (data.get("errors") if isinstance(data, dict) else None) or []
        detail = "; ".join(str(e.get("detail") or e.get("title") or "") for e in errors) or str(data)[:300]
        return None, "failed", f"HTTP {status}: {detail}"[:500]
    msg = data.get("data") or {}
    to = (msg.get("to") or [{}])[0] if isinstance(msg.get("to"), list) else {}
    return msg.get("id"), to.get("status") or "queued", ""

def _twilio_sms_send(from_number, to_number, text, acct):
    """Send through the user's Twilio account. Returns (sid, status, error)."""
    with tw_ctx(acct):
        if not twilio_enabled(tw_current()):
            return None, "failed", "Twilio account is not configured"
        form = {"From": from_number, "To": to_number, "Body": text}
        if PUBLIC_BASE_URL:
            form["StatusCallback"] = PUBLIC_BASE_URL + "/twilio/sms-status"
        try:
            data = _twilio_request("POST", "/Messages.json", form)
        except HTTPException as exc:
            return None, "failed", str(exc.detail)[:500]
    return data.get("sid"), data.get("status") or "queued", ""

def _link_number_to_sms_profile(number):
    """Assign the number to the SMS messaging profile so inbound texts reach the dialer. Returns error or ''."""
    conf = tx_conf()
    if not (conf["api_key"] and conf["sms_profile_id"]):
        return f"The SMS profile id for {conf['label']} is not set; link the number to the messaging profile in Telnyx."
    auth = {"Authorization": f"Bearer {conf['api_key']}"}
    try:
        status, data = vh.http_json("GET", "https://api.telnyx.com/v2/phone_numbers?" +
                                    urllib.parse.urlencode({"filter[phone_number]": number}), headers=auth)
        rows = data.get("data") if isinstance(data, dict) else None
        if status >= 400 or not rows:
            return f"Number {number} not found in the Telnyx account."
        status, data = vh.http_json("PATCH", f"https://api.telnyx.com/v2/phone_numbers/{rows[0]['id']}/messaging",
                                    body={"messaging_profile_id": conf["sms_profile_id"]}, headers=auth)
        if status >= 400:
            errors = (data.get("errors") if isinstance(data, dict) else None) or []
            return "Telnyx refused: " + ("; ".join(str(e.get("detail") or e.get("title")) for e in errors) or str(status))
    except vh.ProviderError as exc:
        return str(exc)
    return ""

@app.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request=request, name="messages.html", context={
        "user": user, "sms_ok": has_sms(user), "open_number": _sms_number(request.query_params.get("to", "")),
        "is_admin": user.get("role") == "admin"})

_sms_scope_cache = {}

def _sms_scope(user):
    """SQL filter for this user's SMS inbox. A number shared by several users of
    one company is one inbox that all of them see. The shared-user list is
    cached for 60 s so the 15 s unread-badge poll does not hit the database for
    the member list every time."""
    num = user.get("phone_number") or ""
    if not (num and user.get("account_id")):
        return "user_id=%s", [str(user["id"])]
    ck = (user["id"], num)
    hit = _sms_scope_cache.get(ck)
    if hit and time.time() - hit[0] < 60:
        ids = hit[1]
    else:
        ids = [str(user["id"])]
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT DISTINCT a.user_id FROM public.caller_id_assignments a
                    JOIN public.voxsbridge_users u ON u.id=a.user_id
                    WHERE a.phone_number=%s AND u.account_id=%s""", (num, user["account_id"]))
                ids += [str(r["user_id"]) for r in cur.fetchall() if str(r["user_id"]) not in ids]
        _sms_scope_cache[ck] = (time.time(), ids)
    if len(ids) > 1:
        return "user_id = ANY(%s::uuid[]) AND our_number=%s", [ids, num]
    return "user_id=%s", [str(user["id"])]

@app.get("/api/sms/unread")
async def sms_unread(request: Request):
    user = get_current_user(request)
    if not user or not has_sms(user):
        return {"unread": 0}
    with db_connect() as conn:
        with conn.cursor() as cur:
            scope, sp = _sms_scope(user)
            cur.execute("SELECT COUNT(*) AS n FROM public.sms_messages WHERE " + scope +
                        " AND direction='in' AND is_read=false", sp)
            return {"unread": int(cur.fetchone()["n"])}

@app.get("/api/sms/threads")
async def sms_threads(request: Request):
    user = _sms_require(request)
    scope, sp = _sms_scope(user)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT t.contact_number, t.body, t.direction, t.status, t.created_at, u.unread
                FROM (SELECT DISTINCT ON (contact_number) contact_number, body, direction, status, created_at
                      FROM public.sms_messages WHERE """ + scope + """
                      ORDER BY contact_number, created_at DESC) t
                LEFT JOIN (SELECT contact_number, COUNT(*) AS unread FROM public.sms_messages
                      WHERE """ + scope + """ AND direction='in' AND is_read=false GROUP BY contact_number) u
                  ON u.contact_number=t.contact_number
                ORDER BY t.created_at DESC LIMIT 300""", sp + sp)
            rows = cur.fetchall()
    contacts = _contact_names(user["id"])
    out = []
    for r in rows:
        local = _to_local(r["created_at"])
        c = contacts.get(r["contact_number"])
        out.append({"number": r["contact_number"], "name": c["name"] if c else "",
                    "preview": (("You: " if r["direction"] == "out" else "") + (r["body"] or "[media]"))[:90],
                    "unread": int(r["unread"] or 0), "at": r["created_at"].isoformat() if r["created_at"] else "",
                    "time": local.strftime("%d %b, %I:%M %p") if local else ""})
    return {"threads": out, "from": user.get("phone_number", "")}

@app.get("/api/sms/thread")
async def sms_thread(request: Request, number: str = ""):
    user = _sms_require(request)
    contact = _sms_number(number)
    if not contact:
        raise HTTPException(status_code=422, detail="Enter a number with country code, e.g. +12025550123")
    scope, sp = _sms_scope(user)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT * FROM (SELECT id,direction,body,media,status,created_at FROM public.sms_messages
                WHERE """ + scope + """ AND contact_number=%s ORDER BY created_at DESC LIMIT 300) x
                ORDER BY created_at ASC""", sp + [contact])
            rows = cur.fetchall()
            cur.execute("UPDATE public.sms_messages SET is_read=true WHERE " + scope +
                        " AND contact_number=%s AND direction='in' AND is_read=false", sp + [contact])
    c = _contact_names(user["id"]).get(contact)
    return {"number": contact, "name": c["name"] if c else "", "messages": [_sms_row(r) for r in rows]}

@app.post("/api/sms/send")
async def sms_send(request: Request):
    user = _sms_require(request)
    data = await request.json()
    to = _sms_number(data.get("to"))
    text = str(data.get("text") or "").replace("\r\n", "\n").strip()
    if not to:
        raise HTTPException(status_code=422, detail="Enter a number with country code, e.g. +12025550123")
    if to == _sms_number(user.get("phone_number")):
        raise HTTPException(status_code=422, detail="You cannot text your own number.")
    if not text:
        raise HTTPException(status_code=422, detail="Type a message first.")
    if len(text) > SMS_MAX_CHARS:
        raise HTTPException(status_code=422, detail=f"Message is too long (max {SMS_MAX_CHARS} characters).")
    now_ts = time.time()
    recent = [t for t in _sms_burst.get(user["id"], []) if now_ts - t < 60]
    if len(recent) >= 20:
        raise HTTPException(status_code=429, detail="Too many messages in a minute. Please wait a moment.")
    recent.append(now_ts); _sms_burst[user["id"]] = recent
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*) AS n FROM public.sms_messages WHERE user_id=%s AND direction='out'
                AND created_at > NOW() - INTERVAL '24 hours'""", (user["id"],))
            if int(cur.fetchone()["n"]) >= SMS_DAILY_LIMIT:
                raise HTTPException(status_code=429, detail="Daily message limit reached. Please contact support.")
    is_twilio = (user.get("provider") or "telnyx") == "twilio"
    if not is_twilio and not tx_conf()["api_key"]:
        raise HTTPException(status_code=503, detail=SMS_UNAVAILABLE)
    # SMS credits (packs of 100). NULL = unlimited (older users). One credit per message segment.
    segs = _sms_segments(text)
    charged = False
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT sms_credits FROM public.voxsbridge_users WHERE id=%s", (user["id"],))
            cr = (cur.fetchone() or {}).get("sms_credits")
            if cr is not None:
                cur.execute("""UPDATE public.voxsbridge_users SET sms_credits=sms_credits-%s
                    WHERE id=%s AND sms_credits>=%s RETURNING sms_credits""", (segs, user["id"], segs))
                left = cur.fetchone()
                if not left:
                    raise HTTPException(status_code=402, detail="You have used all your SMS. Contact support to add more messages.")
                charged = True
    try:
        if is_twilio:
            provider_id, status, error = _twilio_sms_send(user["phone_number"], to, text, user.get("twilio_account"))
        else:
            provider_id, status, error = _telnyx_sms_send(user["phone_number"], to, text)
    except vh.ProviderError as exc:
        provider_id, status, error = None, "failed", str(exc)[:500]
    if charged and error:   # failed sends cost nothing
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE public.voxsbridge_users SET sms_credits=sms_credits+%s WHERE id=%s", (segs, user["id"]))
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.sms_messages(user_id,our_number,contact_number,direction,body,status,error,provider_id,is_read)
                VALUES(%s,%s,%s,'out',%s,%s,%s,%s,true) RETURNING id,direction,body,media,status,created_at""",
                (user["id"], user["phone_number"], to, text, status, error, provider_id))
            row = cur.fetchone()
    if error:
        log.warning("SMS send failed for %s: %s", user["email"], error)
        detail = error if user.get("role") == "admin" else SMS_UNAVAILABLE
        return JSONResponse({"ok": False, "detail": detail, "message": _sms_row(row)}, status_code=502)
    return {"ok": True, "message": _sms_row(row), "segments": _sms_segments(text)}

@app.delete("/api/sms/thread")
async def sms_delete_thread(request: Request, number: str = ""):
    user = _sms_require(request)
    contact = _sms_number(number)
    if not contact:
        raise HTTPException(status_code=422, detail="Invalid number")
    with db_connect() as conn:
        with conn.cursor() as cur:
            scope, sp = _sms_scope(user)
            cur.execute("DELETE FROM public.sms_messages WHERE " + scope + " AND contact_number=%s", sp + [contact])
    return {"ok": True}

@app.post("/twilio/sms")
async def twilio_sms_inbound(request: Request):
    """Twilio number 'A message comes in' webhook. Stores the text for the number's owner."""
    params = await _twilio_params(request)
    ours = _sms_number(params.get("To"))
    sender = _sms_number(params.get("From")) or str(params.get("From") or "")[:40]
    sid = params.get("MessageSid") or params.get("SmsSid") or ""
    body = str(params.get("Body") or "")[:5000]
    if not ours or not sender:
        return _twiml_response()
    media = []
    try:
        for i in range(int(params.get("NumMedia") or 0)):
            if params.get(f"MediaUrl{i}"):
                media.append({"url": params.get(f"MediaUrl{i}"), "type": params.get(f"MediaContentType{i}")})
    except ValueError:
        pass
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id FROM public.voxsbridge_users WHERE phone_number=%s AND is_active=TRUE
                ORDER BY sms_enabled DESC, created_at ASC LIMIT 1""", (ours,))
            owner = cur.fetchone()
            if not owner:
                log.info("Inbound Twilio SMS to unassigned number %s ignored", ours)
                return _twiml_response()
            cur.execute("""INSERT INTO public.sms_messages(user_id,our_number,contact_number,direction,body,media,status,provider_id)
                VALUES(%s,%s,%s,'in',%s,%s::jsonb,'received',%s) ON CONFLICT (provider_id) DO NOTHING""",
                (owner["id"], ours, sender, body, json.dumps(media), sid or None))
    return _twiml_response()

@app.post("/twilio/sms-status")
async def twilio_sms_status(request: Request):
    """Delivery status of texts sent through Twilio (queued, sent, delivered, failed ...)."""
    params = await _twilio_params(request)
    sid = params.get("MessageSid") or params.get("SmsSid") or ""
    status = (params.get("MessageStatus") or params.get("SmsStatus") or "")[:40]
    error = ""
    if params.get("ErrorCode"):
        error = ("Twilio error %s %s" % (params.get("ErrorCode"), params.get("ErrorMessage") or ""))[:500]
    if sid and status:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE public.sms_messages SET status=%s, error=CASE WHEN %s<>'' THEN %s ELSE error END
                    WHERE provider_id=%s AND direction='out'""", (status, error, error, sid))
    return Response(status_code=204)

@app.post("/webhooks/telnyx/sms")
@app.post("/webhooks/telnyx-sms")          # old VoxsBridge messaging-profile URL keeps working
async def telnyx_sms_webhook(request: Request):
    raw = await request.body()
    if not verify_telnyx_webhook(raw, request.headers.get("telnyx-signature-ed25519", ""),
                                 request.headers.get("telnyx-timestamp", "")):
        raise HTTPException(status_code=401, detail="Invalid Telnyx webhook signature")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    data = body.get("data") or {}
    return _handle_telnyx_sms_event(data.get("event_type") or "", data.get("payload") or {})

def _handle_telnyx_sms_event(event, p):
    """Inbound text or delivery status from Telnyx Messaging."""
    if event == "message.received":
        if str(p.get("type") or "SMS").upper() not in ("SMS", "MMS"):
            return {"ok": True}
        to_list = p.get("to") or []
        ours = _sms_number((to_list[0] or {}).get("phone_number") if isinstance(to_list, list) and to_list else "")
        sender = _sms_number((p.get("from") or {}).get("phone_number")) or str((p.get("from") or {}).get("phone_number") or "")[:40]
        if not ours or not sender:
            return {"ok": True}
        media = [{"url": m.get("url"), "type": m.get("content_type")} for m in (p.get("media") or []) if isinstance(m, dict)]
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id FROM public.voxsbridge_users WHERE phone_number=%s AND is_active=TRUE
                    ORDER BY sms_enabled DESC, created_at ASC LIMIT 1""", (ours,))
                owner = cur.fetchone()
                if not owner:
                    log.info("Inbound SMS to unassigned number %s ignored", ours)
                    return {"ok": True}
                cur.execute("""INSERT INTO public.sms_messages(user_id,our_number,contact_number,direction,body,media,status,provider_id)
                    VALUES(%s,%s,%s,'in',%s,%s::jsonb,'received',%s) ON CONFLICT (provider_id) DO NOTHING""",
                    (owner["id"], ours, sender, str(p.get("text") or "")[:5000], json.dumps(media), p.get("id")))
    elif event.startswith("message.") and p.get("id"):
        to_list = p.get("to") or []
        status = (to_list[0] or {}).get("status") if isinstance(to_list, list) and to_list else ""
        errors = p.get("errors") or []
        error = "; ".join(str(e.get("detail") or e.get("title") or "") for e in errors if isinstance(e, dict))[:500]
        if status:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""UPDATE public.sms_messages SET status=%s, error=CASE WHEN %s<>'' THEN %s ELSE error END
                        WHERE provider_id=%s AND direction='out'""", (status, error, error, p["id"]))
    return {"ok": True}

@app.post("/users/call-countries")
async def set_call_countries(request: Request):
    """Admin: which countries a user may call. Nothing ticked = country of their own number."""
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        return RedirectResponse(url="/login", status_code=302)
    form = await request.form()
    email = str(form.get("email") or "").strip().lower()
    zones = [z for z in CALL_ZONES if z in form.getlist("zones")]
    back = urllib.parse.urlsplit(request.headers.get("referer") or "").path or "/users"
    if not (back.startswith("/users") or back.startswith("/customers/")):
        back = "/users"
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.voxsbridge_users SET call_countries=%s WHERE email=%s RETURNING id",
                        (",".join(zones), email))
            row = cur.fetchone()
    if not row:
        return RedirectResponse(url=back + "?error=User+not+found", status_code=302)
    if email in users_db:
        users_db[email]["call_countries"] = ",".join(zones)
    return RedirectResponse(url=back + "?created=1", status_code=302)

@app.post("/users/sms")
async def toggle_sms(request: Request, email: str = Form(...)):
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        return RedirectResponse(url="/login", status_code=302)
    email = email.strip().lower()
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.voxsbridge_users SET sms_enabled = NOT sms_enabled
                WHERE email=%s RETURNING sms_enabled, phone_number, provider""", (email,))
            row = cur.fetchone()
    if not row:
        return RedirectResponse(url="/users?error=User+not+found", status_code=302)
    if email in users_db:
        users_db[email]["sms_enabled"] = bool(row["sms_enabled"])
    back = urllib.parse.urlsplit(request.headers.get("referer") or "").path or "/users"
    if not (back.startswith("/users") or back.startswith("/customers/")):
        back = "/users"
    sep = "?"
    if row["sms_enabled"] and row["phone_number"] and (row["provider"] or "telnyx") == "twilio":
        ok = _twilio_configure_number(row["phone_number"])
        if not ok:
            return RedirectResponse(url=back + sep + "error=" + urllib.parse.quote(
                "SMS turned ON, but the Twilio number webhook could not be set. Check PUBLIC_BASE_URL and the Twilio credentials."), status_code=302)
    if row["sms_enabled"] and row["phone_number"] and (row["provider"] or "telnyx") == "telnyx":
        with tx_ctx(telnyx_number_accounts().get(row["phone_number"], "t1")):
            problem = _link_number_to_sms_profile(row["phone_number"])
        if problem:
            log.warning("SMS profile link for %s: %s", email, problem)
            return RedirectResponse(url=back + sep + "error=" + urllib.parse.quote("SMS turned ON, but: " + problem), status_code=302)
    return RedirectResponse(url=back + sep + "created=1", status_code=302)

@app.post("/users/sms-credits")
async def set_sms_credits(request: Request, email: str = Form(...), credits: str = Form("")):
    """Admin: SMS allowance in multiples of 100 (100, 200, 300 ...). Blank = unlimited."""
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        return RedirectResponse(url="/login", status_code=302)
    email = email.strip().lower()
    credits = credits.strip()
    value = None
    if credits:
        if not credits.isdigit() or int(credits) % 100 != 0 or int(credits) > 1000000:
            return RedirectResponse(url="/users?error=" + urllib.parse.quote("SMS must be a multiple of 100 (100, 200, 300 ...) or blank for unlimited"), status_code=303)
        value = int(credits)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.voxsbridge_users SET sms_credits=%s WHERE email=%s AND role<>'admin' RETURNING id", (value, email))
            if not cur.fetchone():
                return RedirectResponse(url="/users?error=User+not+found", status_code=303)
    refresh_users()
    back = urllib.parse.urlsplit(request.headers.get("referer") or "").path or "/users"
    if not (back.startswith("/users") or back.startswith("/customers/")):
        back = "/users"
    return RedirectResponse(url=back + "?created=1", status_code=303)

@app.post("/users/enable")
async def enable_user(request: Request, email: str = Form(...)):
    """Admin: switch a disabled user back on (keeps their data)."""
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.voxsbridge_users SET is_active=TRUE
                WHERE email=%s AND role<>'admin' AND signup_status<>'pending' RETURNING id""", (email.strip().lower(),))
            if not cur.fetchone():
                return RedirectResponse(url="/users?error=User+not+found", status_code=303)
    refresh_users()
    return RedirectResponse(url="/users?created=1", status_code=303)

@app.post("/users/delete-permanent")
async def delete_user_permanently(request: Request, email: str = Form(...), confirm: str = Form("")):
    """Admin: erase a user and ALL their data from the database. Cannot be undone."""
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    email = email.strip().lower()
    if email == admin["email"]:
        return RedirectResponse(url="/users?error=Cannot+delete+your+own+account", status_code=303)
    if confirm.strip().lower() != email:
        return RedirectResponse(url="/users?error=" + urllib.parse.quote("Type the user's email exactly to confirm permanent delete"), status_code=303)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,role,account_id FROM public.voxsbridge_users WHERE email=%s FOR UPDATE", (email,))
            target = cur.fetchone()
            if not target or target["role"] == "admin":
                return RedirectResponse(url="/users?error=User+not+found+or+protected", status_code=303)
            uid, account_id = target["id"], target["account_id"]
            # Every table with a user_id / *_user column, plus every foreign key into the users table.
            cur.execute("""SELECT DISTINCT c.table_name, c.column_name FROM information_schema.columns c
                JOIN information_schema.tables t ON t.table_schema=c.table_schema AND t.table_name=c.table_name
                WHERE c.table_schema='public' AND t.table_type='BASE TABLE' AND c.table_name<>'voxsbridge_users'
                  AND c.column_name IN ('user_id','from_user','to_user','agent_id','owner_id')
                  AND c.data_type='uuid'""")
            targets = {(r["table_name"], r["column_name"]) for r in cur.fetchall()}
            cur.execute("""SELECT cl.relname AS t, a.attname AS col FROM pg_constraint k
                JOIN pg_class cl ON cl.oid=k.conrelid JOIN pg_namespace n ON n.oid=cl.relnamespace
                JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=ANY(k.conkey)
                WHERE k.contype='f' AND k.confrelid='public.voxsbridge_users'::regclass AND n.nspname='public'
                  AND cl.relname<>'voxsbridge_users'""")
            targets |= {(r["t"], r["col"]) for r in cur.fetchall()}
            for table, col in sorted(targets):
                cur.execute("SAVEPOINT pdel")
                try:
                    cur.execute('DELETE FROM public."%s" WHERE "%s"=%%s' % (table.replace('"', ''), col.replace('"', '')), (uid,))
                    cur.execute("RELEASE SAVEPOINT pdel")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT pdel")
                    log.exception("Permanent delete: could not clear %s.%s", table, col)
            for tbl, q in (("sms_messages", "DELETE FROM public.sms_messages WHERE user_id=%s"),
                           ("voxsbridge_invoice_items", "DELETE FROM public.voxsbridge_invoice_items WHERE invoice_id IN (SELECT id FROM public.voxsbridge_invoices WHERE customer_id=%s)"),
                           ("voxsbridge_invoices", "DELETE FROM public.voxsbridge_invoices WHERE customer_id=%s")):
                cur.execute("SAVEPOINT pdel2")
                try:
                    cur.execute(q, (uid,)); cur.execute("RELEASE SAVEPOINT pdel2")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT pdel2")
            cur.execute("DELETE FROM public.voxsbridge_users WHERE id=%s", (uid,))
            if account_id:
                cur.execute("""DELETE FROM public.voxsbridge_accounts a WHERE a.id=%s
                    AND NOT EXISTS (SELECT 1 FROM public.voxsbridge_users u WHERE u.account_id=a.id)""", (account_id,))
    refresh_users()
    return RedirectResponse(url="/users?purged=1", status_code=303)

@app.post("/users/power")
async def toggle_power(request: Request, email: str = Form(...)):
    """Admin: switch Power Dialer, Campaigns, CRM and Follow-ups on/off for one user."""
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        return RedirectResponse(url="/login", status_code=302)
    email = email.strip().lower()
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.voxsbridge_users SET power_enabled = NOT power_enabled
                WHERE email=%s RETURNING power_enabled""", (email,))
            row = cur.fetchone()
    if not row:
        return RedirectResponse(url="/users?error=User+not+found", status_code=302)
    if email in users_db:
        users_db[email]["power_enabled"] = bool(row["power_enabled"])
    back = urllib.parse.urlsplit(request.headers.get("referer") or "").path or "/users"
    if not (back.startswith("/users") or back.startswith("/customers/")):
        back = "/users"
    return RedirectResponse(url=back + "?created=1", status_code=302)

# CRM and power dialer: user-scoped, opt-in only, PostgreSQL persistence.
CRM_STATUSES = {"new", "interested", "callback", "converted", "not_interested"}

def _crm_number(raw):
    value = re.sub(r"[\s().-]", "", str(raw or ""))
    if not re.fullmatch(r"\+[1-9]\d{7,14}", value):
        raise HTTPException(status_code=422, detail="Phone must be E.164, e.g. +12025550123")
    return value

def _user_country_code(user):
    """Country code of the user's own line: US/Canada line -> '1', UK line -> '44'.
    Lines outside the known zones fall back to UK, as before."""
    zone = number_zone((user or {}).get("phone_number"))
    return CALL_ZONES[zone][0] if zone in CALL_ZONES else "44"

def _import_number(raw, default_cc="44"):
    """Best-effort E.164 for messy import data. Numbers written without a country
    code get the country code of the importing user's line (default_cc):
    US line: '2525550123' / '(252) 555-0123' -> +12525550123
    UK line: '7717316354' / '07717316354'    -> +447717316354
    Returns None when it cannot be made into a valid number."""
    if isinstance(raw, float) and raw.is_integer():     # Excel stores numbers as 2525550123.0
        raw = int(raw)
    v = re.sub(r"[^\d+]", "", str(raw or ""))
    if v.startswith("+"):
        return v if re.fullmatch(r"\+[1-9]\d{7,14}", v) else None
    d = re.sub(r"\D", "", v)
    if not d:
        return None
    if d.startswith("00"):                          # international access prefix
        d = d[2:]
        return "+" + d if re.fullmatch(r"[1-9]\d{7,14}", d) else None
    if default_cc == "1":                           # US / Canada line
        if len(d) == 10 and d[0] in "23456789":     # 252 555 0123
            return "+1" + d
        if len(d) == 11 and d.startswith("1") and d[1] in "23456789":
            return "+" + d                          # 1 252 555 0123
        if 11 <= len(d) <= 15 and d[0] != "0":      # another country, already with its code
            return "+" + d
        return None
    if d.startswith(default_cc) and len(d) >= 11:   # already carries the country code
        return "+" + d
    if d.startswith("0"):                           # national trunk 0 -> country code
        rest = d[1:]
        return "+" + default_cc + rest if re.fullmatch(r"[1-9]\d{6,13}", rest) else None
    if len(d) == 10 and d[0] == "7":                # UK mobile typed without the 0
        return "+" + default_cc + d
    if 11 <= len(d) <= 15 and d[0] != "0":          # looks like it already has a country code
        return "+" + d
    return None

def _crm_row(row):
    return {k: (str(v) if isinstance(v, uuid.UUID) else v.isoformat() if isinstance(v, (datetime, date)) else v)
            for k, v in dict(row).items()}

@app.get("/crm", response_class=HTMLResponse)
async def crm_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not has_power(user):
        return locked_page(request, user, "CRM")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT * FROM public.crm_leads WHERE user_id=%s
                           ORDER BY created_at DESC LIMIT 1000""", (user["id"],))
            leads = [_crm_row(r) for r in cur.fetchall()]
    return templates.TemplateResponse(request=request, name="crm.html", context={"user":user,"leads":leads})

@app.post("/api/crm/leads")
async def crm_save_lead(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    if not has_power(user):
        raise HTTPException(status_code=403, detail="CRM is an optional add-on for your plan")
    data = await request.json()
    name = str(data.get("name") or "").strip()[:150]
    if not name:
        raise HTTPException(status_code=422, detail="Name required")
    phone = _crm_number(data.get("phone"))
    status = str(data.get("status") or "new")
    if status not in CRM_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")
    consent = data.get("consent") is True
    dnc = data.get("do_not_call") is True
    email = str(data.get("email") or "").strip()[:254]
    notes = str(data.get("notes") or "")[:5000]
    followup = data.get("followup_at") or None
    try:
        followup = datetime.fromisoformat(followup) if followup else None
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Invalid follow-up time")
    lead_id = data.get("id")
    with db_connect() as conn:
        with conn.cursor() as cur:
            if lead_id:
                try: lead_id = uuid.UUID(str(lead_id))
                except ValueError: raise HTTPException(status_code=422,detail="Invalid lead ID")
                cur.execute("""UPDATE public.crm_leads SET name=%s,phone=%s,email=%s,status=%s,
                    notes=%s,followup_at=%s,consent=%s,do_not_call=%s,updated_at=NOW()
                    WHERE id=%s AND user_id=%s RETURNING *""",
                    (name,phone,email,status,notes,followup,consent,dnc,lead_id,user["id"]))
            else:
                cur.execute("""INSERT INTO public.crm_leads
                    (user_id,name,phone,email,status,notes,followup_at,consent,do_not_call)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (user["id"],name,phone,email,status,notes,followup,consent,dnc))
            row = cur.fetchone()
    if not row: raise HTTPException(status_code=404, detail="Lead not found")
    return {"lead": _crm_row(row)}

def _delete_leads(user_id, ids):
    """Delete the user's own leads, together with any power-dialer queue rows pointing at them."""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM public.crm_leads WHERE user_id=%s AND id=ANY(%s)", (user_id, ids))
            own = [r["id"] for r in cur.fetchall()]
            if not own:
                return 0
            cur.execute("DELETE FROM public.power_queue_items WHERE lead_id=ANY(%s)", (own,))
            cur.execute("DELETE FROM public.crm_leads WHERE user_id=%s AND id=ANY(%s)", (user_id, own))
            return len(own)

@app.delete("/api/crm/leads/{lead_id}")
async def crm_delete_lead(lead_id: uuid.UUID, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    if not has_power(user):
        raise HTTPException(status_code=403, detail="CRM is an optional add-on for your plan")
    if not _delete_leads(user["id"], [lead_id]):
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"deleted": True}

@app.post("/api/crm/leads/bulk-delete")
async def crm_bulk_delete(request: Request):
    """Delete many leads at once (CRM and Power Dialer multi-select)."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    if not has_power(user):
        raise HTTPException(status_code=403, detail="CRM is an optional add-on for your plan")
    data = await request.json()
    raw = data.get("ids")
    if not isinstance(raw, list) or not raw or len(raw) > 2000:
        raise HTTPException(status_code=422, detail="Select 1 to 2000 leads")
    try:
        ids = list({uuid.UUID(str(x)) for x in raw})
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid lead IDs")
    return {"deleted": _delete_leads(user["id"], ids)}

@app.post("/api/crm/import")
async def crm_import(request: Request, file: UploadFile = File(...)):
    user = get_current_user(request)
    if not user: raise HTTPException(status_code=401,detail="Login required")
    if not has_power(user): raise HTTPException(status_code=403,detail="CRM is an optional add-on for your plan")
    fname = (file.filename or "").lower()
    is_xlsx = fname.endswith(".xlsx")
    is_csv = fname.endswith(".csv")
    if not (is_csv or is_xlsx):
        raise HTTPException(status_code=422, detail="Upload a CSV or Excel (.xlsx) file")
    limit = 3_000_000 if is_xlsx else 250_000
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status_code=413, detail="File is too large")

    # Read the file into a plain table: a list of rows, each a list of cell values.
    if is_xlsx:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.worksheets[0]
            table = [list(r) for r in ws.iter_rows(values_only=True)]
        except Exception:
            raise HTTPException(status_code=422, detail="Could not read the Excel file. Save it as .xlsx or CSV and retry.")
    else:
        try:
            table = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
        except (UnicodeError, csv.Error):
            raise HTTPException(status_code=422, detail="Invalid CSV file")

    table = [r for r in table if any(c is not None and str(c).strip() != "" for c in r)]
    if not table:
        return {"added": 0, "skipped": 0}

    YES = ("yes", "true", "1", "y")
    def cell(v): return "" if v is None else str(v).strip()
    def ncol(v): return re.sub(r"[^a-z0-9]", "", cell(v).lower())
    KNOWN_HEADERS = {"name","fullname","contact","contactname","leadname","phone","phonenumber","mobile",
        "number","telephone","tel","cell","company","business","organisation","organization","businessname",
        "website","url","web","site","email","emailaddress","mail","notes","note","comment","comments","remarks",
        "consent","optin","permission","donotcall","dnc","donotcontact","firstname","lastname","first","last",
        "title","address","city","county","postcode","zip","state","region","age","provider"}
    header_cells = [ncol(c) for c in table[0]]
    has_header = sum(1 for h in header_cells if h in KNOWN_HEADERS) >= 2

    # Build a common list of leads: (name, phone_raw, company, website, email, notes, consent_ok, dnc)
    leads_in = []
    if has_header:
        headers = header_cells
        has_consent_col = any(h in ("consent", "optin", "permission") for h in headers)
        def pickr(r, *keys):
            for k in keys:
                if r.get(k): return cell(r[k])
            return ""
        for row in table[1:]:
            r = {headers[i]: row[i] for i in range(min(len(headers), len(row)))}
            dnc = pickr(r, "donotcall", "dnc", "donotcontact").lower() in YES
            consent_ok = (not has_consent_col) or pickr(r, "consent", "optin", "permission").lower() in YES
            phone_raw = pickr(r, "phone", "phonenumber", "mobile", "number", "telephone", "tel", "cell")
            name = pickr(r, "name", "fullname", "contact", "contactname", "leadname")
            if not name:
                name = (pickr(r, "title") + " " + pickr(r, "firstname", "first") + " " + pickr(r, "lastname", "last")).strip()
            company = pickr(r, "company", "business", "organisation", "organization", "businessname")
            website = pickr(r, "website", "url", "web", "site")
            email = pickr(r, "email", "emailaddress", "mail")
            notes = pickr(r, "notes", "note", "comment", "comments", "remarks")
            leads_in.append((name or company, phone_raw, company, website, email, notes, consent_ok, dnc))
    else:
        # No header row (e.g. a raw telesales lead list). Detect columns by content.
        ncols = max(len(r) for r in table)
        n = len(table)
        def digs(v): return re.sub(r"\D", "", cell(v))
        phone_idx, best = -1, 0
        for c in range(ncols):
            cnt = sum(1 for r in table if c < len(r) and 9 <= len(digs(r[c])) <= 13)
            if cnt > best: best, phone_idx = cnt, c
        if phone_idx < 0 or best < max(1, n // 2):
            raise HTTPException(status_code=422, detail="Couldn't find a phone column. Add a header row (Name, Phone, Notes) and retry.")
        TITLES = {"mr", "mrs", "miss", "ms", "mx", "dr", "sir", "mstr"}
        title_idx = -1
        for c in range(ncols):
            if c == phone_idx: continue
            if sum(1 for r in table if c < len(r) and cell(r[c]).lower().rstrip(".") in TITLES) > n // 2:
                title_idx = c; break
        date_cols = {c for c in range(ncols)
                     if sum(1 for r in table if c < len(r) and isinstance(r[c], (datetime, date))) > n // 2}
        if title_idx >= 0:
            name_cols = [i for i in (title_idx, title_idx + 1, title_idx + 2) if i < ncols and i != phone_idx]
        else:
            def alpha_score(c):
                return sum(1 for r in table if c < len(r) and re.search(r"[A-Za-z]", cell(r[c])) and len(digs(r[c])) < 4)
            cand = sorted([c for c in range(ncols) if c != phone_idx and c not in date_cols],
                          key=alpha_score, reverse=True)
            name_cols = sorted(cand[:2])
        used = set(name_cols) | {phone_idx} | date_cols
        for row in table:
            name = " ".join(cell(row[c]) for c in name_cols if c < len(row) and cell(row[c])).strip()
            phone_raw = row[phone_idx] if phone_idx < len(row) else ""
            extras = [cell(row[c]) for c in range(ncols) if c not in used and c < len(row) and cell(row[c])]
            leads_in.append((name, phone_raw, "", "", "", " · ".join(extras), True, False))

    if len(leads_in) > 500:
        raise HTTPException(status_code=422, detail="Maximum 500 rows per import")

    added = 0; skipped = 0
    home_cc = _user_country_code(user)     # leads get the country code of this user's own number
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                for (name, phone_raw, company, website, email, notes, consent_ok, dnc) in leads_in:
                    if dnc or not consent_ok:
                        skipped += 1; continue
                    phone = _import_number(phone_raw, home_cc)
                    name = (name or "").strip()[:150]
                    if not phone or not name:
                        skipped += 1; continue
                    cur.execute("SELECT 1 FROM public.crm_leads WHERE user_id=%s AND phone=%s", (user["id"], phone))
                    if cur.fetchone():
                        skipped += 1; continue      # same number already in this user's CRM
                    # status/do_not_call set explicitly so the insert never depends on
                    # column defaults (matches the manual "Add lead" insert).
                    # Use "" (not None) for empty text so NOT NULL columns never break.
                    cur.execute("""INSERT INTO public.crm_leads
                        (user_id,name,phone,email,company,website,notes,consent,status,do_not_call)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,TRUE,'new',FALSE)""",
                        (user["id"], name, phone, (email or "")[:254],
                         (company or "")[:200], (website or "")[:300], (notes or "")[:2000]))
                    added += 1
    except psycopg.Error as e:
        # Surface a readable reason instead of a bare 500 (e.g. a missing column).
        detail = (getattr(e, "diag", None) and e.diag.message_primary) or str(e)
        raise HTTPException(status_code=422, detail="Import failed: " + str(detail)[:200])
    return {"added": added, "skipped": skipped}

@app.get("/power-dialer", response_class=HTMLResponse)
async def power_dialer_page(request: Request):
    user=get_current_user(request)
    if not user: return RedirectResponse(url="/login",status_code=302)
    if not has_power(user): return locked_page(request, user, "Power Dialer")
    return templates.TemplateResponse(request=request,name="power_dialer.html",context={"user":user})

@app.post("/api/power/queue")
async def power_queue_create(request: Request):
    user=get_current_user(request)
    if not user: raise HTTPException(status_code=401,detail="Login required")
    if not has_power(user): raise HTTPException(status_code=403,detail="Power Dialer is an optional add-on for your plan")
    if user.get("role")!="admin" and plan_status(user)["days_left"]<0:
        raise HTTPException(status_code=402,detail="Plan expired")
    data=await request.json()
    mode=data.get("mode")
    if mode not in ("auto","manual"): raise HTTPException(status_code=422,detail="Invalid mode")
    try: delay=int(data.get("delay",5))
    except (ValueError,TypeError): raise HTTPException(status_code=422,detail="Invalid delay")
    if not 3<=delay<=30: raise HTTPException(status_code=422,detail="Delay must be 3-30 seconds")
    requested=data.get("lead_ids")
    if not isinstance(requested,list) or not requested or len(requested)>500:
        raise HTTPException(status_code=422,detail="Select 1 to 500 CRM leads")
    try: requested_ids=[uuid.UUID(str(x)) for x in requested]
    except (TypeError,ValueError): raise HTTPException(status_code=422,detail="Invalid lead IDs")
    if len(set(requested_ids))!=len(requested_ids):
        raise HTTPException(status_code=422,detail="Duplicate lead IDs")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id FROM public.crm_leads WHERE user_id=%s AND id=ANY(%s)
                AND consent=TRUE AND do_not_call=FALSE AND status NOT IN ('converted','not_interested')""",(user["id"],requested_ids))
            eligible={r["id"] for r in cur.fetchall()}
            if len(eligible)!=len(requested_ids):
                raise HTTPException(status_code=422,detail="Some selected leads are ineligible or no longer available")
            ids=requested_ids
            if not ids: raise HTTPException(status_code=422,detail="No eligible opted-in CRM leads")
            cur.execute("""INSERT INTO public.power_queues(user_id,mode,delay_seconds,status)
                VALUES(%s,%s,%s,'paused') RETURNING id""",(user["id"],mode,delay))
            qid=cur.fetchone()["id"]
            for idx,lead in enumerate(ids):
                cur.execute("""INSERT INTO public.power_queue_items(queue_id,lead_id,position)
                    VALUES(%s,%s,%s)""",(qid,lead,idx))
    return {"queue_id":str(qid),"count":len(ids)}

@app.get("/api/power/queue/{queue_id}")
async def power_queue_get(queue_id: uuid.UUID,request: Request):
    user=get_current_user(request)
    if not user: raise HTTPException(status_code=401,detail="Login required")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM public.power_queues WHERE id=%s AND user_id=%s",(queue_id,user["id"]))
            queue=cur.fetchone()
            if not queue: raise HTTPException(status_code=404,detail="Queue not found")
            cur.execute("""SELECT i.id,i.position,i.result,i.finished_at,i.call_notes,l.name,l.phone,l.status,l.consent,l.do_not_call,l.company,l.website,l.notes
                FROM public.power_queue_items i JOIN public.crm_leads l ON l.id=i.lead_id
                WHERE i.queue_id=%s ORDER BY i.position""",(queue_id,))
            items=[_crm_row(r) for r in cur.fetchall()]
    return {"queue":_crm_row(queue),"items":items}

@app.post("/api/power/queue/{queue_id}/state")
async def power_queue_state(queue_id: uuid.UUID,request: Request):
    user=get_current_user(request)
    if not user: raise HTTPException(status_code=401,detail="Login required")
    data=await request.json(); state=data.get("state")
    if state not in ("running","paused","stopped"):
        raise HTTPException(status_code=422,detail="Invalid state")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.power_queues SET status=%s,updated_at=NOW()
                WHERE id=%s AND user_id=%s RETURNING id""",
                (state,queue_id,user["id"]))
            if not cur.fetchone(): raise HTTPException(status_code=404,detail="Queue unavailable")
    return {"status":state}

@app.post("/api/power/queue/{queue_id}/notes")
async def power_queue_notes(queue_id: uuid.UUID, request: Request):
    user=get_current_user(request)
    if not user: raise HTTPException(status_code=401,detail="Login required")
    data=await request.json()
    try: item_id=uuid.UUID(str(data.get("item_id")))
    except (ValueError,TypeError): raise HTTPException(status_code=422,detail="Invalid item ID")
    notes=data.get("notes","")
    if not isinstance(notes,str) or len(notes)>5000:
        raise HTTPException(status_code=422,detail="Notes must be 5000 characters or fewer")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.power_queue_items i SET call_notes=%s
                FROM public.power_queues q WHERE i.queue_id=q.id AND q.id=%s
                AND q.user_id=%s AND i.id=%s RETURNING i.id""",
                (notes,queue_id,user["id"],item_id))
            if not cur.fetchone(): raise HTTPException(status_code=404,detail="Call not found")
    return {"saved":True}

@app.post("/api/power/queue/{queue_id}/authorize")
async def power_queue_authorize(queue_id: uuid.UUID, request: Request):
    """Recheck consent, DNC, ownership and queue state immediately before dialing."""
    user = get_current_user(request)
    if not user: raise HTTPException(status_code=401, detail="Login required")
    data = await request.json()
    try: item_id = uuid.UUID(str(data.get("item_id")))
    except (ValueError, TypeError): raise HTTPException(status_code=422, detail="Invalid item ID")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT l.phone FROM public.power_queue_items i
                JOIN public.power_queues q ON q.id=i.queue_id
                JOIN public.crm_leads l ON l.id=i.lead_id
                WHERE q.id=%s AND q.user_id=%s AND q.status='running'
                  AND i.id=%s AND i.finished_at IS NULL
                  AND l.consent=TRUE AND l.do_not_call=FALSE
                  AND l.status NOT IN ('converted','not_interested')""",
                (queue_id,user["id"],item_id))
            row=cur.fetchone()
    if not row: raise HTTPException(status_code=409, detail="Lead is not eligible for calling")
    if not call_allowed(user, row["phone"]): raise HTTPException(status_code=409, detail=call_block_message(user, row["phone"]))
    return {"authorized":True,"phone":row["phone"]}

@app.post("/api/power/queue/{queue_id}/skip")
async def power_queue_skip(queue_id: uuid.UUID, request: Request):
    """Skip one pending item without placing a call; retain audit history."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    data = await request.json()
    try:
        item_id = uuid.UUID(str(data.get("item_id")))
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Invalid item ID")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.power_queue_items i SET result='skipped',finished_at=NOW()
                FROM public.power_queues q WHERE i.queue_id=q.id AND q.id=%s
                AND q.user_id=%s AND q.status IN ('running','paused')
                AND i.id=%s AND i.finished_at IS NULL RETURNING i.id""",
                (queue_id, user["id"], item_id))
            if not cur.fetchone():
                raise HTTPException(status_code=409, detail="Queue item already completed or unavailable")
    return {"skipped": True}

@app.post("/api/power/queue/{queue_id}/result")
async def power_queue_result(queue_id: uuid.UUID,request: Request):
    user=get_current_user(request)
    if not user: raise HTTPException(status_code=401,detail="Login required")
    data=await request.json()
    result=str(data.get("result") or "")
    if result not in ("answered","no_answer","busy","failed","callback","interested","not_interested"):
        raise HTTPException(status_code=422,detail="Invalid result")
    try: item_id=uuid.UUID(str(data.get("item_id")))
    except (ValueError,TypeError): raise HTTPException(status_code=422,detail="Invalid item ID")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.power_queue_items i SET result=%s,finished_at=NOW()
                FROM public.power_queues q WHERE i.queue_id=q.id AND q.id=%s AND q.user_id=%s
                AND i.id=%s AND i.finished_at IS NULL RETURNING i.lead_id""",
                (result,queue_id,user["id"],item_id))
            row=cur.fetchone()
            if not row: raise HTTPException(status_code=409,detail="Already completed or not authorized")
            if result in ("callback","interested","not_interested"):
                cur.execute("UPDATE public.crm_leads SET status=%s,updated_at=NOW() WHERE id=%s AND user_id=%s",
                    (result,row["lead_id"],user["id"]))
    return {"saved":True}


@app.get("/follow-ups", response_class=HTMLResponse)
async def followups_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not has_power(user):
        return locked_page(request, user, "Follow-ups")
    return templates.TemplateResponse(request=request, name="followups.html", context={"user": user, "items": []})

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    context = {"user": user, "plan": plan_status(user),
               "profile_msg": request.query_params.get("profile", ""),
               "profile_error": request.query_params.get("perror", "")}
    if user.get("role") == "admin":
        context.update({
            "rates": get_rates(),
            "twilio_ready": twilio_enabled(),
            "storage_mode": "Supabase Storage (private bucket)" if storage.enabled else "Supabase database",
            "public_base_url": PUBLIC_BASE_URL,
            "saved": request.query_params.get("saved"),
        })
    return templates.TemplateResponse(request=request, name="settings.html", context=context)

def _settings_back(msg="", error=""):
    query = urllib.parse.urlencode({"profile": msg} if msg else {"perror": error})
    return RedirectResponse(url="/settings?" + query + "#profile", status_code=303)

@app.post("/settings/profile")
async def settings_profile(request: Request, name: str = Form(...)):
    """The user changes their own display name."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    name = re.sub(r"\s+", " ", name or "").strip()
    if not 2 <= len(name) <= 120:
        return _settings_back(error="Name must be 2 to 120 characters")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.voxsbridge_users SET name=%s WHERE id=%s", (name, user["id"]))
    if user["email"] in users_db:
        users_db[user["email"]]["name"] = name
    return _settings_back(msg="Name updated")

@app.post("/settings/password")
async def settings_password(request: Request, current_password: str = Form(...),
                            new_password: str = Form(...), confirm_password: str = Form(...)):
    """The user changes their own password (current password required)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("is_demo"):
        return _settings_back(error="The password of a demo account cannot be changed")
    if not verify_password(current_password, user.get("password") or ""):
        return _settings_back(error="Current password is not correct")
    if len(new_password) < 12:
        return _settings_back(error="New password must be at least 12 characters")
    if len(new_password) > 200:
        return _settings_back(error="New password is too long")
    if new_password != confirm_password:
        return _settings_back(error="The two new passwords do not match")
    if new_password == current_password:
        return _settings_back(error="New password must be different from the current one")
    new_hash = hash_password(new_password)
    token = request.cookies.get("session_token") or ""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.voxsbridge_users SET password_hash=%s WHERE id=%s", (new_hash, user["id"]))
            # Sign out every other device; this browser stays logged in.
            cur.execute("""UPDATE public.user_sessions SET revoked_at=NOW()
                WHERE user_id=%s AND revoked_at IS NULL AND token_hash<>%s""",
                (user["id"], hashlib.sha256(token.encode()).hexdigest()))
    if user["email"] in users_db:
        users_db[user["email"]]["password"] = new_hash
    return _settings_back(msg="Password changed. Other devices were signed out.")

# ==================== COMPANIES, TEAM, RESELLERS ====================
PRESENCE_TIMEOUT_SECONDS = 40

def _is_admin(user):
    return bool(user) and user.get("role") == "admin"

def _is_reseller(user):
    return bool(user) and user.get("user_type") == "reseller"

def _is_owner(user):
    return bool(user) and user.get("user_type") == "customer" and user.get("account_role") == "owner" and bool(user.get("account_id"))

templates.env.globals["is_owner"] = _is_owner
templates.env.globals["is_reseller"] = _is_reseller

def _account_row(account_id):
    try:
        aid = uuid.UUID(str(account_id))
    except (ValueError, TypeError):
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT a.*, r.name AS reseller_name, r.email AS reseller_email
                FROM public.voxsbridge_accounts a LEFT JOIN public.voxsbridge_users r ON r.id=a.reseller_id
                WHERE a.id=%s""", (aid,))
            return cur.fetchone()

def _can_manage_account(viewer, account_id):
    """Admin: every company. Owner: own company (calls, recordings, everything)."""
    if not viewer or not account_id:
        return False
    if _is_admin(viewer):
        return True
    return _is_owner(viewer) and str(account_id) == viewer.get("account_id")

def _can_view_account(viewer, account_id):
    """Managers plus the reseller the company belongs to (status and counts only)."""
    if _can_manage_account(viewer, account_id):
        return True
    if _is_reseller(viewer) and account_id:
        acc = _account_row(account_id)
        return bool(acc) and str(acc["reseller_id"] or "") == viewer["id"]
    return False

def _all_companies():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT a.id,a.name,a.reseller_id,a.created_at,r.name AS reseller_name,
                    (SELECT count(*) FROM public.voxsbridge_users u WHERE u.account_id=a.id AND u.is_active) AS users,
                    (SELECT string_agg(u.name, ', ') FROM public.voxsbridge_users u
                        WHERE u.account_id=a.id AND u.is_active AND u.account_role='owner') AS owners,
                    (SELECT count(*) FROM public.voxsbridge_users u JOIN public.user_presence p ON p.user_id=u.id
                        WHERE u.account_id=a.id AND u.is_active
                        AND p.updated_at > now() - make_interval(secs => %s) AND p.status <> 'offline') AS online
                FROM public.voxsbridge_accounts a
                LEFT JOIN public.voxsbridge_users r ON r.id=a.reseller_id
                -- hide companies whose users were all deleted (disabled); keep brand-new empty ones
                WHERE EXISTS (SELECT 1 FROM public.voxsbridge_users u WHERE u.account_id=a.id AND u.is_active)
                   OR NOT EXISTS (SELECT 1 FROM public.voxsbridge_users u WHERE u.account_id=a.id)
                ORDER BY lower(a.name)""", (PRESENCE_TIMEOUT_SECONDS,))
            return cur.fetchall()

def _all_resellers():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT u.id,u.name,u.email,u.is_active,u.created_at,
                    (SELECT count(*) FROM public.voxsbridge_accounts a WHERE a.reseller_id=u.id) AS companies,
                    (SELECT count(*) FROM public.voxsbridge_users m JOIN public.voxsbridge_accounts a ON a.id=m.account_id
                        WHERE a.reseller_id=u.id AND m.is_active) AS users
                FROM public.voxsbridge_users u WHERE u.user_type='reseller' AND u.is_active
                ORDER BY lower(u.name)""")
            return cur.fetchall()

def _team_members(account_id):
    """Members of a company with live status and today's numbers."""
    starts = _period_starts()
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT u.id,u.name,u.email,u.phone_number,u.account_role,u.is_active,
                    p.status,p.call_number,p.call_direction,p.call_started_at,p.updated_at,
                    EXTRACT(EPOCH FROM (now()-p.updated_at))::int AS seen_ago,
                    EXTRACT(EPOCH FROM (now()-p.call_started_at))::int AS call_seconds,
                    COALESCE(t.calls,0) AS calls_today, COALESCE(t.answered,0) AS answered_today,
                    COALESCE(t.talk,0) AS talk_today, t.last_call
                FROM public.voxsbridge_users u
                LEFT JOIN public.user_presence p ON p.user_id=u.id
                LEFT JOIN LATERAL (
                    SELECT count(*) AS calls,
                           count(*) FILTER (WHERE c.status IN ('answered','completed','active')) AS answered,
                           COALESCE(sum(c.duration_seconds),0) AS talk, max(c.created_at) AS last_call
                    FROM public.calls c WHERE c.user_id=u.id AND c.created_at >= %s
                ) t ON TRUE
                WHERE u.account_id=%s AND u.is_active
                ORDER BY (u.account_role='owner') DESC, lower(u.name)""", (starts["today"], account_id))
            rows = cur.fetchall()
    members = []
    for r in rows:
        status = r["status"] or "offline"
        if r["updated_at"] is None or (r["seen_ago"] or 0) > PRESENCE_TIMEOUT_SECONDS:
            status = "offline"
        last_call = _to_local(r["last_call"])
        members.append({
            "id": str(r["id"]), "name": r["name"], "email": r["email"],
            "phone_number": r["phone_number"] or "", "account_role": r["account_role"],
            "status": status if status in ("online", "on_call", "ringing", "dialing", "offline") else "online",
            "call_number": r["call_number"] if status in ("on_call", "ringing", "dialing") else "",
            "call_direction": r["call_direction"] if status in ("on_call", "ringing", "dialing") else "",
            "call_seconds": max(0, int(r["call_seconds"] or 0)) if status == "on_call" and r["call_started_at"] else 0,
            "calls_today": int(r["calls_today"]), "answered_today": int(r["answered_today"]),
            "talk_today": int(r["talk_today"]),
            "last_call": last_call.strftime("%I:%M %p").lstrip("0") if last_call else "",
        })
    return members

def _team_summary(members):
    return {
        "total": len(members),
        "online": sum(m["status"] != "offline" for m in members),
        "on_call": sum(m["status"] in ("on_call", "dialing") for m in members),
        "calls_today": sum(m["calls_today"] for m in members),
        "talk_today": sum(m["talk_today"] for m in members),
    }

def _resolve_team_account(user, requested):
    if requested and (_is_admin(user) or _is_reseller(user)):
        if not _can_view_account(user, requested):
            raise HTTPException(status_code=404, detail="Company not found")
        return requested
    if _is_owner(user):
        return user["account_id"]
    raise HTTPException(status_code=403, detail="Team view is for company owners")

@app.post("/api/presence")
async def presence_update(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = await request.json()
    status = str(data.get("status") or "online")
    if status not in ("online", "on_call", "ringing", "dialing", "offline"):
        status = "online"
    number = re.sub(r"[^\d+*#]", "", str(data.get("number") or ""))[:40]
    direction = "incoming" if data.get("direction") in ("incoming", "inbound") else ("outgoing" if number else "")
    started = None
    try:
        ms = int(data.get("started_at") or 0)
        if ms > 0 and status == "on_call":
            started = datetime.fromtimestamp(min(ms / 1000, time.time()), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        started = None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.user_presence(user_id,status,call_number,call_direction,call_started_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,now())
                ON CONFLICT (user_id) DO UPDATE SET status=EXCLUDED.status, call_number=EXCLUDED.call_number,
                    call_direction=EXCLUDED.call_direction, call_started_at=EXCLUDED.call_started_at, updated_at=now()""",
                (user["id"], status, number, direction, started))
    return {"ok": True}

@app.get("/team", response_class=HTMLResponse)
async def team_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    try:
        account_id = _resolve_team_account(user, request.query_params.get("account"))
    except HTTPException:
        return RedirectResponse(url="/dashboard", status_code=302)
    account = _account_row(account_id)
    if not account:
        return RedirectResponse(url="/dashboard", status_code=302)
    members = _team_members(account["id"])
    return templates.TemplateResponse(request=request, name="team.html", context={
        "user": user, "account": account, "members": members, "summary": _team_summary(members),
        "can_open": _can_manage_account(user, account["id"]),
        "back_url": "/clients" if _is_reseller(user) else ("/companies/" + str(account["id"]) if _is_admin(user) else ""),
    })

@app.get("/api/team")
async def team_api(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    account_id = _resolve_team_account(user, request.query_params.get("account"))
    members = _team_members(account_id)
    return JSONResponse({"members": members, "summary": _team_summary(members)}, headers={"Cache-Control": "no-store"})

@app.get("/team/member/{member_id}", response_class=HTMLResponse)
async def team_member_page(member_id: uuid.UUID, request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    target = _db_user_by_id(str(member_id))
    if not target or not _can_manage_account(user, target.get("account_id")):
        return RedirectResponse(url="/team", status_code=302)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT c.id,c.from_number,c.to_number,c.direction,c.status,c.duration_seconds,c.created_at,
                    r.id AS recording_id, r.kind AS recording_kind
                FROM public.calls c
                LEFT JOIN LATERAL (SELECT id,kind FROM public.voxsbridge_recordings
                    WHERE call_id=c.id ORDER BY created_at DESC LIMIT 1) r ON TRUE
                WHERE c.user_id=%s ORDER BY c.created_at DESC LIMIT 300""", (member_id,))
            rows = cur.fetchall()
    today = datetime.now(_local_tz()).date()
    calls = []
    for r in rows:
        local = _to_local(r["created_at"])
        calls.append({
            "number": _clean_number(r["from_number"] if r["direction"] == "incoming" else r["to_number"]) or "Private Number",
            "direction": r["direction"] or "", "status": (r["status"] or "").replace("_", " "),
            "duration": int(r["duration_seconds"] or 0),
            "date_label": _day_label(local, today) if local else "",
            "time_label": local.strftime("%I:%M %p").lstrip("0") if local else "",
            "recording_url": f"/recordings/{r['recording_id']}/audio" if r["recording_id"] else "",
            "recording_kind": r["recording_kind"] or "",
        })
    recordings = _recording_rows(str(member_id), "call") + _recording_rows(str(member_id), "voicemail")
    members = _team_members(target["account_id"])
    me = next((m for m in members if m["id"] == target["id"]), None)
    return templates.TemplateResponse(request=request, name="team_member.html", context={
        "user": user, "member": target, "live": me, "calls": calls, "recordings": recordings,
        "back_url": "/team" + ("?account=" + target["account_id"] if _is_admin(user) else ""),
    })

# ---- Admin: companies ----
@app.get("/companies", response_class=HTMLResponse)
async def companies_page(request: Request):
    admin = require_admin(request)
    return templates.TemplateResponse(request=request, name="companies.html", context={
        "user": admin, "companies": _all_companies(), "resellers": _all_resellers(),
        "msg": request.query_params.get("msg")})

@app.post("/companies")
async def company_create(request: Request, name: str = Form(...), reseller_id: str = Form("")):
    require_admin(request)
    name = name.strip()[:200]
    if not name:
        raise HTTPException(status_code=422, detail="Company name required")
    reseller = None
    if reseller_id:
        try:
            reseller = uuid.UUID(reseller_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid reseller")
    with db_connect() as conn:
        with conn.cursor() as cur:
            if reseller:
                cur.execute("SELECT 1 FROM public.voxsbridge_users WHERE id=%s AND user_type='reseller'", (reseller,))
                if not cur.fetchone():
                    raise HTTPException(status_code=422, detail="Reseller not found")
            cur.execute("INSERT INTO public.voxsbridge_accounts(name,reseller_id) VALUES (%s,%s) RETURNING id", (name, reseller))
            new_id = cur.fetchone()["id"]
    return RedirectResponse(f"/companies/{new_id}?msg=created", status_code=303)

@app.get("/companies/{account_id}", response_class=HTMLResponse)
async def company_detail(account_id: uuid.UUID, request: Request):
    admin = require_admin(request)
    account = _account_row(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Company not found")
    members = _team_members(account_id)
    extra = {}
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,email,plan_name,plan_renewal,provider,is_demo FROM public.voxsbridge_users
                WHERE account_id=%s""", (account_id,))
            for r in cur.fetchall():
                extra[str(r["id"])] = r
            cur.execute("""SELECT id,name,email FROM public.voxsbridge_users
                WHERE is_active AND role<>'admin' AND user_type='customer' AND (account_id IS NULL OR account_id<>%s)
                ORDER BY lower(name)""", (account_id,))
            others = cur.fetchall()
    for m in members:
        e = extra.get(m["id"]) or {}
        m.update(plan_name=e.get("plan_name") or "", plan_renewal=e.get("plan_renewal"),
                 provider=e.get("provider") or "telnyx", is_demo=bool(e.get("is_demo")))
    return templates.TemplateResponse(request=request, name="company_detail.html", context={
        "user": admin, "account": account, "members": members, "summary": _team_summary(members),
        "resellers": _all_resellers(), "others": others, "companies": _all_companies(),
        "msg": request.query_params.get("msg")})

@app.post("/companies/{account_id}/update")
async def company_update(account_id: uuid.UUID, request: Request, name: str = Form(...),
                         reseller_id: str = Form(""), notes: str = Form("")):
    require_admin(request)
    reseller = None
    if reseller_id:
        try:
            reseller = uuid.UUID(reseller_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid reseller")
    with db_connect() as conn:
        with conn.cursor() as cur:
            if reseller:
                cur.execute("SELECT 1 FROM public.voxsbridge_users WHERE id=%s AND user_type='reseller'", (reseller,))
                if not cur.fetchone():
                    raise HTTPException(status_code=422, detail="Reseller not found")
            cur.execute("""UPDATE public.voxsbridge_accounts SET name=%s,reseller_id=%s,notes=%s
                WHERE id=%s RETURNING id""", (name.strip()[:200] or "Company", reseller, notes.strip()[:2000], account_id))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Company not found")
    return RedirectResponse(f"/companies/{account_id}?msg=saved", status_code=303)

@app.post("/companies/{account_id}/members")
async def company_member(account_id: uuid.UUID, request: Request, user_id: str = Form(...),
                         action: str = Form(...), account_role: str = Form("agent"), target_account: str = Form("")):
    """add (move a user into this company) / role (owner or agent) / move (to another company)."""
    require_admin(request)
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid user")
    role = "owner" if account_role == "owner" else "agent"
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM public.voxsbridge_users WHERE id=%s AND user_type='customer' AND role<>'admin'", (uid,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="User not found")
            if action == "add":
                cur.execute("UPDATE public.voxsbridge_users SET account_id=%s,account_role=%s WHERE id=%s", (account_id, role, uid))
            elif action == "role":
                cur.execute("UPDATE public.voxsbridge_users SET account_role=%s WHERE id=%s AND account_id=%s", (role, uid, account_id))
            elif action == "move":
                try:
                    dest = uuid.UUID(target_account)
                except ValueError:
                    raise HTTPException(status_code=422, detail="Select a company")
                cur.execute("SELECT 1 FROM public.voxsbridge_accounts WHERE id=%s", (dest,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Company not found")
                cur.execute("UPDATE public.voxsbridge_users SET account_id=%s,account_role='agent' WHERE id=%s AND account_id=%s",
                            (dest, uid, account_id))
            else:
                raise HTTPException(status_code=422, detail="Unknown action")
    refresh_users()
    return RedirectResponse(f"/companies/{account_id}?msg=members", status_code=303)

# ---- Admin: resellers ----
@app.get("/resellers", response_class=HTMLResponse)
async def resellers_page(request: Request):
    admin = require_admin(request)
    return templates.TemplateResponse(request=request, name="resellers.html", context={
        "user": admin, "resellers": _all_resellers(), "msg": request.query_params.get("msg")})

@app.get("/resellers/{reseller_id}", response_class=HTMLResponse)
async def reseller_detail(reseller_id: uuid.UUID, request: Request):
    admin = require_admin(request)
    reseller = _db_user_by_id(str(reseller_id))
    if not reseller or reseller.get("user_type") != "reseller":
        raise HTTPException(status_code=404, detail="Reseller not found")
    companies = [c for c in _all_companies() if str(c["reseller_id"] or "") == reseller["id"]]
    unassigned = [c for c in _all_companies() if not c["reseller_id"]]
    return templates.TemplateResponse(request=request, name="reseller_detail.html", context={
        "user": admin, "reseller": reseller, "companies": companies, "unassigned": unassigned,
        "msg": request.query_params.get("msg")})

@app.post("/resellers/{reseller_id}/companies")
async def reseller_assign(reseller_id: uuid.UUID, request: Request, account_id: str = Form(...), action: str = Form("assign")):
    require_admin(request)
    try:
        aid = uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Select a company")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public.voxsbridge_users WHERE id=%s AND user_type='reseller'", (reseller_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Reseller not found")
            if action == "remove":
                cur.execute("UPDATE public.voxsbridge_accounts SET reseller_id=NULL WHERE id=%s AND reseller_id=%s", (aid, reseller_id))
            else:
                cur.execute("UPDATE public.voxsbridge_accounts SET reseller_id=%s WHERE id=%s", (reseller_id, aid))
    return RedirectResponse(f"/resellers/{reseller_id}?msg=saved", status_code=303)

# ---- Reseller portal (view only, no provider names or costs) ----
@app.get("/clients", response_class=HTMLResponse)
async def reseller_clients(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not _is_reseller(user):
        return RedirectResponse(url="/dashboard", status_code=302)
    companies = [c for c in _all_companies() if str(c["reseller_id"] or "") == user["id"]]
    starts = _period_starts()
    stats = {}
    if companies:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT u.account_id,
                        count(c.id) FILTER (WHERE c.created_at >= %s) AS today,
                        count(c.id) FILTER (WHERE c.created_at >= %s) AS week,
                        count(c.id) FILTER (WHERE c.created_at >= %s) AS month
                    FROM public.voxsbridge_users u LEFT JOIN public.calls c ON c.user_id=u.id AND c.created_at >= %s
                    WHERE u.account_id = ANY(%s) GROUP BY u.account_id""",
                    (starts["today"], starts["week"], starts["month"], min(starts.values()),
                     [c["id"] for c in companies]))
                for r in cur.fetchall():
                    stats[str(r["account_id"])] = r
    for c in companies:
        st = stats.get(str(c["id"])) or {}
        c["calls_today"] = int(st.get("today") or 0)
        c["calls_week"] = int(st.get("week") or 0)
        c["calls_month"] = int(st.get("month") or 0)
    totals = {k: sum(c[k] for c in companies) for k in ("users", "online", "calls_today", "calls_month")}
    return templates.TemplateResponse(request=request, name="clients.html", context={
        "user": user, "companies": companies, "totals": totals})


# ==================== CALLING PREFERENCES (per user) ====================
RINGTONES = ("classic", "uk", "digital", "chime", "pulse", "silent")
DEFAULT_PREFS = {"ringtone": "classic", "ring_volume": 70, "auto_record": False}

def get_user_prefs(user_id):
    prefs = dict(DEFAULT_PREFS)
    try:
        raw = get_setting(f"prefs:{user_id}")
        if raw:
            data = json.loads(raw)
            if data.get("ringtone") in RINGTONES:
                prefs["ringtone"] = data["ringtone"]
            if isinstance(data.get("ring_volume"), int) and 0 <= data["ring_volume"] <= 100:
                prefs["ring_volume"] = data["ring_volume"]
            prefs["auto_record"] = data.get("auto_record") is True
    except Exception:
        log.exception("Could not read calling preferences")
    return prefs

templates.env.globals["user_prefs"] = lambda user: get_user_prefs(user.get("id")) if user and user.get("id") else dict(DEFAULT_PREFS)

@app.get("/settings/calling", response_class=HTMLResponse)
async def calling_prefs_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request=request, name="calling_prefs.html", context={
        "user": user, "prefs": get_user_prefs(user["id"]), "recording_ok": has_recording(user)})

@app.post("/api/preferences")
async def save_calling_prefs(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = await request.json()
    ringtone = data.get("ringtone")
    if ringtone not in RINGTONES:
        raise HTTPException(status_code=422, detail="Unknown ringtone")
    try:
        volume = int(data.get("ring_volume", 70))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid volume")
    if not 0 <= volume <= 100:
        raise HTTPException(status_code=422, detail="Volume must be 0-100")
    auto_record = data.get("auto_record") is True
    if auto_record and not has_recording(user):
        raise HTTPException(status_code=402, detail="Call recording is not enabled on this account")
    prefs = {"ringtone": ringtone, "ring_volume": volume, "auto_record": auto_record}
    set_setting(f"prefs:{user['id']}", json.dumps(prefs))
    return {"saved": True, "prefs": prefs}

@app.post("/settings/rates")
async def save_rates(request: Request):
    require_admin(request)
    form = await request.form()
    updates = {}
    for key in vh.DEFAULT_RATES:
        raw = str(form.get(key, "")).strip()
        if raw == "":
            continue
        try:
            value = float(raw)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid number for {key}")
        if value < 0 or value > (1000 if key == "usd_to_pkr" else 5):
            raise HTTPException(status_code=422, detail=f"Value out of range for {key}")
        updates[key] = value
    for key, value in updates.items():
        set_setting("rate_" + key, value)
    return RedirectResponse("/settings?saved=1", status_code=303)

@app.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request=request, name="billing.html", context={
        "user": user,
        "plan": plan_status(user),
    })


# Atomic group-call coordination; the telephony leg orchestration must be
# configured separately through Telnyx Voice API before enabling live routing.
@app.post("/api/incoming-groups/{group_id}/answer")
async def claim_incoming_group(group_id: uuid.UUID, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM public.voxsbridge_users WHERE email=%s", (user["email"],))
            actor = cur.fetchone()
            if not actor:
                raise HTTPException(status_code=403, detail="Unknown user")
            uid = actor["id"]
            cur.execute("SELECT status FROM public.call_participants WHERE group_id=%s AND user_id=%s", (group_id,uid))
            participant = cur.fetchone()
            if not participant:
                raise HTTPException(status_code=403, detail="Not a participant")
            cur.execute("""UPDATE public.incoming_call_groups SET status='answered',answered_by=%s
                WHERE id=%s AND status='ringing' RETURNING id""", (uid,group_id))
            claimed = cur.fetchone()
            if claimed:
                cur.execute("""UPDATE public.call_participants SET status=CASE
                    WHEN user_id=%s THEN 'answered' ELSE 'cancelled' END WHERE group_id=%s""", (uid,group_id))
                return {"claimed":True,"status":"answered"}
            cur.execute("SELECT status FROM public.incoming_call_groups WHERE id=%s", (group_id,))
            current=cur.fetchone()
            return {"claimed":False,"status":current["status"] if current else "missing"}

@app.post("/api/incoming-groups/{group_id}/decline")
async def decline_incoming_group(group_id: uuid.UUID, request: Request):
    """Decline the ORIGINAL PSTN Call Control leg, not just the browser SIP leg.

    Do not mark a call declined before Telnyx accepts the hangup request:
    failed requests must remain retryable. The call.hangup webhook is the
    authoritative confirmation that the carrier leg actually ended.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT g.telnyx_call_id, g.status, g.ring_call_id, g.is_shared, g.line_account, u.id AS uid
                FROM public.incoming_call_groups g
                JOIN public.call_participants p ON p.group_id=g.id
                JOIN public.voxsbridge_users u ON u.id=p.user_id
                WHERE g.id=%s AND u.email=%s""", (group_id, user["email"]))
            group = cur.fetchone()
    if not group:
        raise HTTPException(status_code=403, detail="Not a participant")
    if group.get("is_shared"):
        # Shared number: only YOUR ringing stops; teammates keep ringing. When
        # nobody is left ringing the caller goes to voicemail.
        leg = rg.decline(str(group_id), group["uid"])
        if leg:
            with tx_ctx(group.get("line_account") or "t1"):
                _telnyx_hangup_quietly(leg)
        return {"group_declined": False, "carrier_hangup_requested": True, "status": "declined_by_you"}
    if group["status"] not in ("ringing", "answered"):
        return {"group_declined": False, "carrier_hangup_requested": False,
                "status": group["status"]}
    try:
        if group["status"] == "ringing":
            # Unanswered call: send a final "call rejected" (SIP 603 Decline).
            # A plain hangup can make the caller's network retry the call a few
            # seconds later, which looked like the call "coming back".
            try:
                _telnyx_request("POST", f"/v2/calls/{group['telnyx_call_id']}/actions/reject",
                                {"cause": "CALL_REJECTED"})
            except Exception:
                _telnyx_request("POST", f"/v2/calls/{group['telnyx_call_id']}/actions/hangup", {})
        else:
            _telnyx_request("POST", f"/v2/calls/{group['telnyx_call_id']}/actions/hangup", {})
    except Exception:
        import logging
        logging.exception("Carrier decline failed for incoming group %s", group_id)
        raise HTTPException(status_code=502, detail="Carrier hangup request failed")
    # Also stop the leg that rings the browser so it is not offered again.
    if group.get("ring_call_id"):
        _telnyx_hangup_quietly(group["ring_call_id"])
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.incoming_call_groups
                SET status='declined', ended_at=NOW()
                WHERE id=%s AND status IN ('ringing','answered')
                RETURNING id""", (group_id,))
            if cur.fetchone():
                cur.execute("""UPDATE public.call_participants SET status='declined'
                    WHERE group_id=%s""", (group_id,))
                cur.execute("""UPDATE public.calls c
                    SET status='declined',duration_seconds=0
                    FROM public.incoming_call_groups g
                    WHERE g.id=%s AND g.call_log_id=c.id""", (group_id,))
    return {"group_declined": True, "carrier_hangup_requested": True,
            "status": "declined", "note": "Await call.hangup webhook to confirm carrier termination"}

@app.get("/api/incoming/pending")
async def pending_incoming_for_user(request: Request):
    """Read-only diagnostic: pending inbound Voice API events for this user.

    This does NOT create a SIP/WebRTC call leg or permit answering the carrier.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT g.id,g.caller_number,g.destination_number,g.created_at
                FROM public.incoming_call_groups g
                JOIN public.call_participants p ON p.group_id=g.id
                JOIN public.voxsbridge_users u ON u.id=p.user_id
                WHERE u.email=%s AND g.status IN ('ringing','queued') AND COALESCE(p.status,'ringing')='ringing'
                  AND g.created_at > NOW() - INTERVAL '5 minutes'
                ORDER BY g.created_at DESC LIMIT 5""", (user["email"],))
            rows = cur.fetchall()
    pending = [{"id":str(r["id"]),"from":r["caller_number"],
                "to":r["destination_number"],"created_at":r["created_at"].isoformat()}
               for r in rows]
    try:
        pending = ct.pending_for(user) + pending      # transfers from a teammate
    except Exception:
        log.exception("Pending transfer lookup failed")
    return {"pending": pending}


@app.get("/api/incoming-groups/{group_id}/status")
async def incoming_group_status(group_id: uuid.UUID, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401,detail="Unauthorized")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT g.status,g.answered_by FROM public.incoming_call_groups g
                JOIN public.call_participants p ON p.group_id=g.id
                JOIN public.voxsbridge_users u ON u.id=p.user_id
                WHERE g.id=%s AND u.email=%s""",(group_id,user["email"]))
            row=cur.fetchone()
    if not row:
        raise HTTPException(status_code=404,detail="Group not found")
    return {"status":row["status"],"answered_by":str(row["answered_by"]) if row["answered_by"] else None}

# ==================== CALL TRANSFER (agent -> agent) ====================
def _xfer_user(request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user

@app.get("/api/transfer/targets")
async def transfer_targets(request: Request):
    user = _xfer_user(request)
    return {"agents": ct.targets(user)}

@app.post("/api/transfer")
async def transfer_start(request: Request):
    user = _xfer_user(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        result = ct.start(user, str(data.get("number") or "")[:40],
                          to_user=data.get("to_user") or None,
                          to_number=str(data.get("to_number") or "")[:40] or None)
    except ct.TransferError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    return result

@app.get("/api/transfer/{tid}")
async def transfer_status(tid: uuid.UUID, request: Request):
    user = _xfer_user(request)
    result = ct.status_of(str(tid), user)
    if not result:
        raise HTTPException(status_code=404, detail="Transfer not found")
    return result

@app.post("/api/transfer/{tid}/cancel")
async def transfer_cancel(tid: uuid.UUID, request: Request):
    user = _xfer_user(request)
    return {"cancelled": ct.cancel(str(tid), user)}

@app.post("/api/transfer/{tid}/decline")
async def transfer_decline(tid: uuid.UUID, request: Request):
    user = _xfer_user(request)
    return {"declined": ct.decline(str(tid), user)}

@app.post("/api/admin/users/{user_id}/force-logout")
async def admin_force_logout(user_id: uuid.UUID, request: Request):
    admin=get_current_user(request)
    if not admin or admin.get("role")!="admin":
        raise HTTPException(status_code=403,detail="Admin only")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.user_sessions SET revoked_at=NOW() WHERE user_id=%s AND revoked_at IS NULL",(user_id,))
            count=cur.rowcount
    terminated = _terminate_user_inbound_calls(user_id)
    return {"revoked_sessions": count,
            "tracked_incoming_calls": terminated['tracked'],
            "carrier_hangups_sent": terminated['hangup_sent'],
            "carrier_hangups_failed": terminated['hangup_failed'],
            "active_calls_terminated": terminated['tracked'] > 0 and terminated['hangup_failed'] == 0,
            "note": "Untracked outbound WebRTC carrier legs cannot be terminated by this API."}


# ==================== WEBSITE ORDERS (marketing site -> dialer) ====================
# Secured server-to-server endpoints under /webhooks/marketing/*, the pending-orders
# admin page and the 10-minute / 1-hour expiry sweeper. See backend/website_orders.py.
_wo = wo.register(app, dict(
    db_connect=db_connect, templates=templates, get_current_user=get_current_user,
    refresh_users=refresh_users, hash_password=hash_password, get_setting=get_setting, get_rates=get_rates,
    telnyx_number_accounts=telnyx_number_accounts, twilio_number_accounts=twilio_number_accounts,
    _set_user_twilio_account=_set_user_twilio_account, _twilio_configure_number=_twilio_configure_number,
    _set_user_telnyx_account=_set_user_telnyx_account, tx_ctx=tx_ctx, _ensure_credential=_ensure_credential,
    worker_should_run=worker_should_run))
