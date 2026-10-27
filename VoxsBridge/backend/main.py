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
# WhatsApp support number (digits only, with country code). Change via env if needed.
templates.env.globals["support_whatsapp"] = os.getenv("SUPPORT_WHATSAPP", "447476629105").strip().lstrip("+")

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

# ==================== CONFIG (Twilio backup provider) ====================
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
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
                             max_idle=int(os.getenv("DB_POOL_SIZE", "8")))

def db_connect():
    return _db_pool.connection()

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
        "is_demo": bool(row.get("is_demo", False)),
        "account_id": str(row["account_id"]) if row.get("account_id") else "",
        "account_role": "owner" if row.get("account_role") == "owner" else "agent",
        "user_type": "reseller" if row.get("user_type") == "reseller" else "customer",
        "trial_started_at": row["trial_started_at"].isoformat() if row.get("trial_started_at") else None,
        "trial_expires_at": row["trial_expires_at"].isoformat() if row.get("trial_expires_at") else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else "",
        "amount_paid": float(row["last_paid_amount"] or 0),
        "sms_enabled": bool(row.get("sms_enabled", False)),
        "sms_limit": int(row.get("sms_limit") or 0),
        "sms_used": int(row.get("sms_used") or 0),
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
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS sms_enabled boolean NOT NULL DEFAULT false;
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS sms_limit integer NOT NULL DEFAULT 0;
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS sms_used integer NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS public.voxsbridge_sms (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    direction text NOT NULL CHECK (direction IN ('in','out')),
    from_number text NOT NULL DEFAULT '',
    to_number text NOT NULL DEFAULT '',
    body text NOT NULL DEFAULT '',
    segments integer NOT NULL DEFAULT 1,
    status text NOT NULL DEFAULT 'queued',
    error text NOT NULL DEFAULT '',
    provider_message_id text UNIQUE,
    is_read boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS voxsbridge_sms_user_idx ON public.voxsbridge_sms(user_id, created_at DESC);
ALTER TABLE public.voxsbridge_sms ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.voxsbridge_sms FROM anon, authenticated;
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
"""

def ensure_schema():
    """Idempotent additive migration (see SUPABASE_SETUP_VOXSBRIDGE.sql)."""
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('voxsbridge-schema-07'))")
                cur.execute(SCHEMA_SQL)
    except Exception:
        log.exception("Automatic schema update failed; run SUPABASE_SETUP_VOXSBRIDGE.sql manually")

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
    return {"today": at(today),
            "week": at(today - timedelta(days=today.weekday())),
            "month": at(today.replace(day=1))}

def cost_summary(user_id=None):
    """Estimated provider cost (USD) for today / this week / this month.

    Billable minutes are rounded up per call and multiplied by the admin-set
    per-minute rate of the provider and direction the call used.
    """
    rates = get_rates()
    starts = _period_starts()
    params = {"tx_in": rates["telnyx_in"], "tx_out": rates["telnyx_out"],
              "tw_in": rates["twilio_in"], "tw_out": rates["twilio_out"],
              "since": min(starts.values()), "today": starts["today"],
              "week": starts["week"], "month": starts["month"], "uid": user_id}
    sql = """
        WITH c AS (
            SELECT created_at, direction,
                   CEIL(GREATEST(COALESCE(duration_seconds,0),0)/60.0)::float8 AS mins,
                   CASE WHEN COALESCE(provider,'telnyx')='twilio'
                        THEN CASE WHEN direction='incoming' THEN %(tw_in)s::float8 ELSE %(tw_out)s::float8 END
                        ELSE CASE WHEN direction='incoming' THEN %(tx_in)s::float8 ELSE %(tx_out)s::float8 END
                   END AS rate
            FROM public.calls
            WHERE created_at >= %(since)s AND (%(uid)s::uuid IS NULL OR user_id=%(uid)s::uuid)
        )
        SELECT
          COALESCE(SUM(mins*rate) FILTER (WHERE created_at >= %(today)s),0) AS today_cost,
          COALESCE(SUM(mins*rate) FILTER (WHERE created_at >= %(week)s),0)  AS week_cost,
          COALESCE(SUM(mins*rate) FILTER (WHERE created_at >= %(month)s),0) AS month_cost,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(today)s AND direction='incoming'),0) AS today_in,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(today)s AND direction<>'incoming'),0) AS today_out,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(week)s AND direction='incoming'),0) AS week_in,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(week)s AND direction<>'incoming'),0) AS week_out,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(month)s AND direction='incoming'),0) AS month_in,
          COALESCE(SUM(mins) FILTER (WHERE created_at >= %(month)s AND direction<>'incoming'),0) AS month_out,
          COUNT(*) FILTER (WHERE created_at >= %(today)s) AS today_calls,
          COUNT(*) FILTER (WHERE created_at >= %(week)s) AS week_calls,
          COUNT(*) FILTER (WHERE created_at >= %(month)s) AS month_calls
        FROM c"""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    pkr = rates["usd_to_pkr"]
    out = {"rates": rates}
    for period in ("today", "week", "month"):
        usd = float(row[f"{period}_cost"] or 0)
        out[period] = {"usd": usd, "pkr": usd * pkr,
                       "in_min": int(row[f"{period}_in"] or 0),
                       "out_min": int(row[f"{period}_out"] or 0),
                       "calls": int(row[f"{period}_calls"] or 0)}
    return out

templates.env.globals["call_cost"] = vh.call_cost


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
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.user_sessions SET revoked_at=NOW() WHERE token_hash=%s",(hashlib.sha256(token.encode()).hexdigest(),))

def get_current_user(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT u.* FROM public.user_sessions s
                JOIN public.voxsbridge_users u ON u.id=s.user_id
                WHERE s.token_hash=%s AND s.revoked_at IS NULL
                AND s.expires_at>NOW() AND u.is_active=TRUE""",(hashlib.sha256(token.encode()).hexdigest(),))
            row=cur.fetchone()
    if not row:
        return None
    user=_db_user(row)
    if user.get("is_demo") and (not user.get("trial_expires_at") or datetime.fromisoformat(user["trial_expires_at"]) <= datetime.now().astimezone()):
        revoke_session(token)
        return None
    users_db[user["email"]]=user
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
            viewer = get_current_user(request)
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

def get_telnyx_numbers():
    """Fetch all account-owned numbers, including paginated results."""
    if not TELNYX_API_KEY:
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

def twilio_enabled():
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)

def _twilio_request(method, path, form=None):
    if not twilio_enabled():
        raise HTTPException(status_code=503, detail="Twilio is not configured (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN)")
    url = path if path.startswith("https://") else f"{TWILIO_API}/Accounts/{TWILIO_ACCOUNT_SID}{path}"
    try:
        status, data = vh.http_json(method, url, form=form, basic_auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
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

def get_twilio_numbers():
    return sorted({r.get("phone_number") for r in _twilio_number_records() if r.get("phone_number")})

def get_provider_numbers(provider):
    if provider == "twilio":
        return get_twilio_numbers()
    return get_telnyx_numbers()

def _twilio_configure_number(number):
    """Point a Twilio number's Voice webhook at this app (best effort)."""
    if not twilio_enabled() or not PUBLIC_BASE_URL:
        return False
    try:
        for record in _twilio_number_records():
            if record.get("phone_number") == number and record.get("sid"):
                _twilio_request("POST", f"/IncomingPhoneNumbers/{record['sid']}.json", {
                    "VoiceUrl": PUBLIC_BASE_URL + "/twilio/incoming",
                    "VoiceMethod": "POST",
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
    key_sid = os.getenv("TWILIO_API_KEY_SID", "").strip() or get_setting("twilio_api_key_sid", "")
    key_secret = os.getenv("TWILIO_API_KEY_SECRET", "").strip() or get_setting("twilio_api_key_secret", "")
    app_sid = os.getenv("TWILIO_TWIML_APP_SID", "").strip() or get_setting("twilio_twiml_app_sid", "")
    if key_sid and key_secret and app_sid:
        return key_sid, key_secret, app_sid
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('voxsbridge-twilio-setup'))")
            key_sid = key_sid or get_setting("twilio_api_key_sid", "")
            key_secret = key_secret or get_setting("twilio_api_key_secret", "")
            app_sid = app_sid or get_setting("twilio_twiml_app_sid", "")
            if not (key_sid and key_secret):
                created = _twilio_request("POST", "/Keys.json", {"FriendlyName": "VoxsBridge Web Dialer"})
                key_sid, key_secret = created.get("sid", ""), created.get("secret", "")
                if not (key_sid and key_secret):
                    raise HTTPException(status_code=502, detail="Twilio API key creation failed")
                set_setting("twilio_api_key_sid", key_sid)
                set_setting("twilio_api_key_secret", key_secret)
            if not app_sid:
                created = _twilio_request("POST", "/Applications.json", {
                    "FriendlyName": "VoxsBridge Web Dialer",
                    "VoiceUrl": PUBLIC_BASE_URL + "/twilio/voice",
                    "VoiceMethod": "POST",
                })
                app_sid = created.get("sid", "")
                if not app_sid:
                    raise HTTPException(status_code=502, detail="Twilio TwiML app creation failed")
                set_setting("twilio_twiml_app_sid", app_sid)
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
            cur.execute("""SELECT DISTINCT u.id,u.email,u.name,a.phone_number
                FROM public.caller_id_assignments a
                JOIN public.voxsbridge_users u ON u.id=a.user_id
                WHERE a.phone_number = ANY(%s)
                UNION
                SELECT DISTINCT u.id,u.email,u.name,u.phone_number
                FROM public.voxsbridge_users u
                WHERE u.phone_number = ANY(%s)""", (numbers, numbers))
            assigned = {}
            for r in cur.fetchall():
                assigned.setdefault(r['phone_number'], []).append({
                    'email': r['email'],
                    'last_name': (r['name'] or '').strip().split()[-1] if (r['name'] or '').strip() else 'Unknown'
                })
    return JSONResponse({'numbers': [
        {'number': n, 'assigned_to': assigned.get(n, [])} for n in numbers
    ]}, headers={'Cache-Control': 'no-store'})


# Admin-only customer management and invoice features.
def require_admin(request):
    actor = get_current_user(request)
    if not actor or actor.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='Admin access required')
    return actor


def _customer_data(email):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT u.id,u.name,u.email,u.phone_number,u.provider,u.plan_name,
                u.plan_start,u.plan_renewal,u.created_at,u.is_active,u.is_demo,
                u.last_paid_amount,u.recording_premium,u.contact_name,u.contact_phone,
                u.contact_address,u.customer_notes,u.sms_enabled,u.sms_limit,u.sms_used,
                (SELECT count(*) FROM public.calls c WHERE c.user_id=u.id AND c.created_at >= date_trunc('month',now())) AS calls_month,
                (SELECT COALESCE(sum(c.duration_seconds),0) FROM public.calls c WHERE c.user_id=u.id AND c.created_at >= date_trunc('month',now())) AS talk_seconds,
                (SELECT COALESCE(sum(p.amount_pkr),0) FROM public.payments p WHERE p.user_id=u.id AND p.status='paid') AS collected,
                (SELECT max(p.paid_at) FROM public.payments p WHERE p.user_id=u.id AND p.status='paid') AS last_payment_at
                FROM public.voxsbridge_users u WHERE lower(u.email)=lower(%s)""", (email,))
            row=cur.fetchone()
            if not row: raise HTTPException(404, 'Customer not found')
            return dict(row)


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
            cur.execute('SELECT amount_pkr,status,paid_at,renewal_date FROM public.payments WHERE user_id=%s ORDER BY paid_at DESC NULLS LAST LIMIT 100',(c['id'],))
            payments=cur.fetchall()
    try:
        costs = cost_summary(c['id'])
    except Exception:
        log.exception("Cost summary failed for customer")
        costs = None
    return templates.TemplateResponse(request=request,name='customer_detail.html',context={
        'user':admin,'customer':c,'payments':payments,'costs':costs,
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
            cur.execute("SELECT id,phone_number FROM public.voxsbridge_users WHERE lower(email)=lower(%s) AND role <> 'admin' FOR UPDATE", (email,))
            target = cur.fetchone()
            if not target:
                raise HTTPException(404, 'Customer not found')
            cur.execute('SELECT user_id FROM public.caller_id_assignments WHERE phone_number=%s AND user_id<>%s', (new_number,target['id']))
            if cur.fetchone():
                raise HTTPException(409, 'This number is already assigned to another user')
            # Remove old assignment and insert new assignment in one transaction.
            cur.execute('DELETE FROM public.caller_id_assignments WHERE user_id=%s', (target['id'],))
            cur.execute('INSERT INTO public.caller_id_assignments(user_id,phone_number) VALUES (%s,%s)', (target['id'],new_number))
            cur.execute('UPDATE public.voxsbridge_users SET phone_number=%s,provider=%s WHERE id=%s',
                        (new_number,provider,target['id']))
    refresh_users()
    if provider == 'twilio':
        _twilio_configure_number(new_number)
    return RedirectResponse('/customers/' + quote(email, safe='') + '?number_updated=1', status_code=303)


@app.post('/customers/{email}/payment')
async def customer_payment(request:Request,email:str,amount:int=Form(...),method:str=Form('Manual'),reference:str=Form('')):
    require_admin(request)
    if amount<=0: raise HTTPException(422,'Amount must be positive')
    c=_customer_data(email)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.payments(user_id,amount_pkr,status,paid_at,renewal_date)
                VALUES(%s,%s,'paid',NOW(),%s)""",(c['id'],amount,c['plan_renewal']))
            cur.execute('UPDATE public.voxsbridge_users SET last_paid_amount=%s WHERE id=%s',(amount,c['id']))
    refresh_users()
    return RedirectResponse('/customers/'+email+'?payment=1',status_code=303)


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
    action:str=Form('save')):
    require_admin(request)
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
                 issue_date,due_date,notes,terms,subtotal_pkr,discount_pkr,tax_percent,tax_pkr,total_pkr,status)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft') RETURNING id""",
                (number,c['id'],c['name'],c['email'],c.get('contact_phone') or '',c.get('contact_address') or '',
                issued,due,notes[:3000],terms[:3000],subtotal,discount,tax_percent,tax_amount,total))
            invoice_id=cur.fetchone()['id']
            for i,(d,q,price) in enumerate(zip(description,qty,unit_price)):
                cur.execute("""INSERT INTO public.voxsbridge_invoice_items(invoice_id,position,description,qty,unit_price_pkr,amount_pkr)
                    VALUES(%s,%s,%s,%s,%s,%s)""",(invoice_id,i,d.strip()[:500],q,price,q*price))
    return RedirectResponse('/invoices/'+str(invoice_id),status_code=303)


@app.get('/invoices/{invoice_id}',response_class=HTMLResponse)
async def invoice_detail(request:Request,invoice_id:uuid.UUID):
    admin=require_admin(request)
    invoice,items=_invoice_row(invoice_id)
    return templates.TemplateResponse(request=request,name='invoice_detail.html',context={'user':admin,'invoice':invoice,'items':items})


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


@app.post('/invoices/{invoice_id}/delete')
async def invoice_delete(request:Request,invoice_id:uuid.UUID):
    require_admin(request)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.voxsbridge_invoices WHERE id=%s AND status IN ('draft','void')",(invoice_id,))
            if cur.rowcount==0: raise HTTPException(409,'Only draft or void invoices can be deleted')
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
                 notes,terms,subtotal_pkr,discount_pkr,tax_percent,tax_pkr,total_pkr,status)
                VALUES(%s,%s,%s,%s,%s,%s,CURRENT_DATE,CURRENT_DATE+INTERVAL '3 days',%s,%s,%s,%s,%s,%s,%s,'draft') RETURNING id""",
                (number,inv['customer_id'],inv['customer_name'],inv['customer_email'],inv['contact_phone'],inv['contact_address'],
                inv['notes'],inv['terms'],inv['subtotal_pkr'],inv['discount_pkr'],inv['tax_percent'],inv['tax_pkr'],inv['total_pkr']))
            new_id=cur.fetchone()['id']
            for item in items:
                cur.execute("""INSERT INTO public.voxsbridge_invoice_items(invoice_id,position,description,qty,unit_price_pkr,amount_pkr)
                    VALUES(%s,%s,%s,%s,%s,%s)""",(new_id,item['position'],item['description'],item['qty'],item['unit_price_pkr'],item['amount_pkr']))
    return RedirectResponse('/invoices/'+str(new_id),status_code=303)


@app.get('/invoices/{invoice_id}/pdf')
async def invoice_pdf(request:Request,invoice_id:uuid.UUID):
    require_admin(request)
    inv,items=_invoice_row(invoice_id)
    buffer=BytesIO()
    doc=SimpleDocTemplate(buffer,pagesize=(595,842),rightMargin=42,leftMargin=42,topMargin=40,bottomMargin=40)
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='DialTitle',parent=styles['Title'],fontSize=22,textColor=colors.HexColor('#111b37'),spaceAfter=16))
    styles.add(ParagraphStyle(name='DialSmall',parent=styles['Normal'],fontSize=9,leading=14))
    def esc(v):
        import html
        return html.escape(str(v or ''))
    story=[Paragraph('VOXSBRIDGE  |  INVOICE',styles['DialTitle']),
        Paragraph('Invoice: '+esc(inv['invoice_number'])+'  |  Status: '+esc(inv['status']).upper(),styles['DialSmall']),Spacer(1,18),
        Paragraph('<b>BILL TO</b><br/>'+esc(inv['customer_name'])+'<br/>'+esc(inv['customer_email'])+'<br/>'+esc(inv['contact_phone']),styles['DialSmall']),Spacer(1,14),
        Paragraph('Issued: '+str(inv['issue_date'])+'    Due: '+str(inv['due_date']),styles['DialSmall']),Spacer(1,18)]
    rows=[['DESCRIPTION','QTY','UNIT PRICE','AMOUNT']]
    for item in items:
        rows.append([Paragraph(esc(item['description']),styles['DialSmall']),str(item['qty']),f"PKR {item['unit_price_pkr']:,.0f}",f"PKR {item['amount_pkr']:,.0f}"])
    table=Table(rows,colWidths=[275,45,95,95],repeatRows=1,hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#111b37')),
        ('TEXTCOLOR',(0,0),(-1,0),colors.white),('BOTTOMPADDING',(0,0),(-1,0),11),('TOPPADDING',(0,0),(-1,0),11),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f2f6fa')]),
        ('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,1),(-1,-1),10),('TOPPADDING',(0,1),(-1,-1),10)]))
    story.extend([table,Spacer(1,20),Paragraph(f"Subtotal: PKR {inv['subtotal_pkr']:,.0f}",styles['DialSmall']),
        Paragraph(f"Discount: PKR {inv['discount_pkr']:,.0f} | Tax ({inv['tax_percent']}%): PKR {inv['tax_pkr']:,.0f}",styles['DialSmall']),
        Spacer(1,10),Paragraph(f"<b>AMOUNT DUE: PKR {inv['total_pkr']:,.0f}</b>",styles['Heading2']),Spacer(1,20),
        Paragraph('<b>HOW TO PAY</b><br/>Contact us for payment.',styles['DialSmall']),Spacer(1,14),
        Paragraph('<b>NOTES</b><br/>'+esc(inv['notes']),styles['DialSmall']),Spacer(1,10),
        Paragraph('<b>TERMS</b><br/>'+esc(inv['terms']),styles['DialSmall'])])
    doc.build(story)
    return Response(buffer.getvalue(),media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="{inv["invoice_number"]}.pdf"','Cache-Control':'private, no-store'})

@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse(request=request, name="users.html", context={
        "user": user,
        "users": list(users_db.values()),
        "companies": _all_companies(),
        "resellers": _all_resellers(),
        "error": request.query_params.get("error"),
        "created": request.query_params.get("created"),
        "deleted": request.query_params.get("deleted"),
    })

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
        return RedirectResponse('/users?' + urlencode({'error': message}), status_code=303)

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

    form = await request.form()
    confirmed_transfer = form.get('confirm_shared_number') == 'yes'
    try:
        amount_paid = int(form.get('amount_paid', '0') or 0)
        if amount_paid < 0:
            raise ValueError()
    except (ValueError, TypeError):
        return fail('Invalid PKR amount')
    payment_status = str(form.get('payment_status', 'paid'))
    if payment_status not in ('paid', 'partial', 'unpaid'):
        payment_status = 'unpaid'
    if is_demo or payment_status == 'unpaid' or user_type == 'reseller':
        amount_paid = 0
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
                    owners = [r['id'] for r in cur.fetchall()]
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
                if amount_paid:
                    cur.execute("""INSERT INTO public.payments(user_id,amount_pkr,status,paid_at,renewal_date)
                        VALUES (%s,%s,%s,%s,%s)""", (new_id,amount_paid,payment_status,
                        datetime.now() if payment_status == 'paid' else None,renewal))
    except UniqueViolation:
        return fail('Email or Caller ID already assigned; refresh and retry')

    refresh_users()
    if phone_number:
        if provider == 'twilio':
            _twilio_configure_number(phone_number)
        else:
            try:
                _ensure_credential(email)
            except Exception:
                log.exception('Telnyx credential creation failed for new user')
    return RedirectResponse('/users?created=1', status_code=303)


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
async def record_repayment(request: Request, email: str = Form(...), amount: int = Form(...), renewal_date: str = Form("")):
    admin = get_current_user(request)
    if not admin or admin.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    target = users_db.get(email.strip().lower())
    if not target: raise HTTPException(status_code=404, detail="User not found")
    if amount <= 0: raise HTTPException(status_code=400, detail="Amount must be positive")
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
            cur.execute("""INSERT INTO public.payments(user_id,amount_pkr,status,paid_at,renewal_date)
                SELECT id,%s,'paid',NOW(),%s::date FROM public.voxsbridge_users WHERE email=%s""",
                (amount,target["plan_renewal"],email.strip().lower()))
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
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT COALESCE(SUM(amount_pkr),0) AS total FROM public.payments
                    WHERE status='paid' AND paid_at >= date_trunc('month',NOW())""")
                revenue = float(cur.fetchone()["total"])
    costs = None
    admin_stats = None
    if user.get("role") == "admin":
        try:
            costs = cost_summary()
        except Exception:
            log.exception("Dashboard cost summary failed")
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
        "period": period, "revenue": revenue, "renewals": renewals, "costs": costs,
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
    if not TELNYX_API_KEY:
        raise HTTPException(status_code=500, detail="TELNYX_API_KEY missing")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        "https://api.telnyx.com" + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TELNYX_API_KEY}",
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

def _ensure_credential(email: str) -> str:
    user = users_db.get(email) or {}
    if user.get("credential_id"):
        return user["credential_id"]
    created = _telnyx_request("POST", "/v2/telephony_credentials", {
        "connection_id": TELNYX_CONNECTION_ID,
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
    cred_id = _ensure_credential(user["email"])
    token_resp = _telnyx_request("POST", f"/v2/telephony_credentials/{cred_id}/token")
    token = None
    if isinstance(token_resp, dict):
        token = token_resp.get("raw") or token_resp.get("data")
        if isinstance(token, dict):
            token = token.get("token")
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
            entry["cost_usd"] = vh.call_cost(entry["duration"], entry["provider"], entry["direction"], rates)
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
        raise HTTPException(status_code=402, detail="Recording is a $25 upgrade")
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
def verify_telnyx_webhook(raw: bytes, signature: str, timestamp: str) -> bool:
    """Validate Telnyx API v2 Ed25519 signature with a 5-minute replay window."""
    public_key = os.getenv("TELNYX_PUBLIC_KEY", "").strip()
    if not public_key or not signature or not timestamp:
        return False
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > 300:
            return False
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        signed_message = timestamp.encode("ascii") + b"|" + raw
        key.verify(base64.b64decode(signature, validate=True), signed_message)
        return True
    except (ValueError, TypeError, InvalidSignature):
        return False

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
    try:
        _telnyx_request("POST", f"/v2/calls/{call_control_id}/actions/answer",
                        {"client_state": _cs_encode(f"vm:{group_id}")})
        return True
    except Exception:
        log.exception("Voicemail answer failed for group %s", group_id)
        _telnyx_hangup_quietly(call_control_id)
        return False

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
        _store_recording(row["user_id"], "voicemail", data, "audio/mpeg", call_id=row["call_id"],
                         provider="telnyx", from_number=row["caller_number"] or "",
                         to_number=row["destination_number"] or "", duration=duration,
                         provider_ref=provider_ref)
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
            background.add_task(_telnyx_save_voicemail, group_id, mp3, duration, ref, call_id)

def _handle_ring_leg_event(event, group_id, background):
    """Events from the SIP leg that rings the agent's browser."""
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
        background.add_task(_telnyx_start_voicemail, group["telnyx_call_id"], group_id)
    elif group["status"] == "answered":
        # Agent hung up first: do not leave the caller in silence.
        background.add_task(_telnyx_hangup_quietly, group["telnyx_call_id"])

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
            _handle_sms_event(event, payload)
        except Exception:
            log.exception("SMS webhook handling failed")
        return JSONResponse({"ok": True})
    client_state = _cs_decode(payload.get("client_state"))
    if client_state.startswith("vm:"):
        _handle_voicemail_event(event, payload, client_state[3:], background)
        if event != "call.hangup":
            return JSONResponse({"ok": True})
    elif client_state.startswith("ring:"):
        _handle_ring_leg_event(event, client_state[5:], background)
        return JSONResponse({"ok": True})
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
                        cur.execute("""SELECT 1 FROM public.incoming_call_groups
                            WHERE caller_number=%s AND destination_number=%s AND status='declined'
                              AND COALESCE(ended_at, created_at) > NOW() - INTERVAL '20 seconds'
                            LIMIT 1""", (origin, destination))
                        if cur.fetchone():
                            retry_of_declined = True
                            cur.execute("""UPDATE public.incoming_call_groups
                                SET status='declined', ended_at=NOW() WHERE id=%s""", (inserted["id"],))
                    if inserted and not retry_of_declined:
                        group_id = inserted["id"]
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
                # Network retry of a call the user just declined: decline it again.
                background.add_task(_telnyx_reject_declined, call_id)
            if group_id:
                # Do not create new SIP credentials in a webhook: the user must
                # have logged in once and obtained a WebRTC credential already.
                sip_username = (ring_user or {}).get("sip_username", "")
                if sip_username and TELNYX_API_KEY and os.getenv("TELNYX_VOICE_APP_ID"):
                    try:
                        ring_resp = _telnyx_request("POST", "/v2/calls", {
                            "connection_id": os.environ["TELNYX_VOICE_APP_ID"],
                            "to": f"sip:{sip_username}@sip.telnyx.com",
                            "from": origin or destination,
                            "link_to": call_id,
                            "bridge_intent": True,
                            "bridge_on_answer": True,
                            "prevent_double_bridge": True,
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
                        background.add_task(_telnyx_start_voicemail, call_id, group_id)
                elif voicemail_ok and TELNYX_API_KEY:
                    # Agent is on Do Not Disturb or has never logged in: voicemail.
                    background.add_task(_telnyx_start_voicemail, call_id, group_id)
                else:
                    # Unassigned number or expired plan. Do not leave a caller
                    # ringing indefinitely.
                    with db_connect() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""UPDATE public.incoming_call_groups
                                SET status='missed',ended_at=NOW() WHERE id=%s""", (group_id,))
                    background.add_task(_telnyx_hangup_quietly, call_id)
        if call_id and event in ("call.answered", "call.bridged", "call.hangup"):
            with db_connect() as conn:
                with conn.cursor() as cur:
                    # Linked SIP leg webhooks can have their own call control id.
                    # For now, only update the inbound leg's stored id.
                    if event in ("call.answered", "call.bridged"):
                        cur.execute("""UPDATE public.incoming_call_groups SET status='answered'
                            WHERE telnyx_call_id=%s AND status='ringing'""", (call_id,))
                        cur.execute("""UPDATE public.calls c SET status='answered'
                            FROM public.incoming_call_groups g WHERE g.call_log_id=c.id
                            AND g.telnyx_call_id=%s AND c.status='ringing'""", (call_id,))
                    else:
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
    if not TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=503, detail="Twilio not configured")
    form = await request.form()
    params = {}
    for key, value in form.multi_items():
        params.setdefault(key, []).append(str(value))
    query = request.url.query
    path = request.url.path + ("?" + query if query else "")
    candidates = [PUBLIC_BASE_URL + path, str(request.url),
                  str(request.url).replace("http://", "https://", 1)]
    if not vh.twilio_signature_valid(TWILIO_AUTH_TOKEN, candidates, params,
                                     request.headers.get("x-twilio-signature", "")):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    return {k: v[0] for k, v in params.items()}

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
    if not twilio_enabled():
        raise HTTPException(status_code=503, detail="Twilio is not configured on the server")
    key_sid, key_secret, app_sid = _twilio_voice_config()
    identity = vh.identity_for_user(user["id"])
    token = vh.twilio_access_token(TWILIO_ACCOUNT_SID, key_sid, key_secret, identity, app_sid, ttl=3600)
    return JSONResponse({"token": token, "identity": identity, "from_number": user.get("phone_number") or ""},
                        headers={"Cache-Control": "no-store"})

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
    return _twiml_response(
        f'<Dial callerId="{vh.xml_attr(user["phone_number"])}" answerOnBridge="true" timeLimit="14400">'
        f'<Number>{vh.xml_attr(to_number)}</Number></Dial>')

@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Voice webhook of each Twilio number: ring the assigned user's browser."""
    params = await _twilio_params(request)
    to_number = params.get("To", "")
    from_number = params.get("From", "") or "Unknown"
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
        data, _ = vh.http_bytes(recording_url + ".mp3", basic_auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                                max_bytes=MAX_RECORDING_BYTES)
        _store_recording(row["user_id"], "voicemail", data, "audio/mpeg", call_id=row["id"],
                         provider="twilio", from_number=row["from_number"] or "",
                         to_number=row["to_number"] or "", duration=duration,
                         provider_ref="twilio:" + recording_sid)
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
        background.add_task(_twilio_save_voicemail, log_id, url, sid, duration)
    return JSONResponse({"ok": True})


@app.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request=request, name="campaigns.html", context={
        "user": user,
        "campaigns": campaigns_db.get(user["email"], []),
        "premium": has_recording(user),
    })

@app.post("/api/campaigns")
async def create_campaign(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not has_recording(user):
        raise HTTPException(status_code=402, detail="Campaigns are part of the $25 upgrade")
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
    _save_scripts(data)
    return RedirectResponse(url="/scripts?edit=" + script_id, status_code=303)

@app.post("/scripts/delete")
async def delete_script(request: Request, script_id: str = Form(...)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    data = _load_scripts()
    data[user["email"]] = [item for item in data.get(user["email"], []) if item["id"] != script_id]
    _save_scripts(data)
    return RedirectResponse(url="/scripts", status_code=303)

# ==================== SMS (admin-enabled add-on) ====================
# Only users the admin enables can text. Each user has a message allowance
# (sms_limit); every SMS segment sent OR received counts against it (sms_used).
# One message = up to 160 characters (70 if it contains Urdu/emoji etc.).
SMS_PROFILE_ID = os.getenv("TELNYX_SMS_PROFILE_ID", "").strip()
SMS_MAX_CHARS = 1600
_GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_GSM7_EXT = set("^{}\\[~]|€")


_SMART_CHARS = str.maketrans({"\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'",
                              "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u00a0": " ",
                              "\u2022": "-", "\u00b4": "'", "\u02bc": "'"})


def sms_clean(text):
    """Swap 'smart' punctuation for plain characters so a text stays at 160 chars per message."""
    return str(text or "").translate(_SMART_CHARS)


def sms_segments(text):
    """How many SMS this text uses: 160 chars = 1 (153 per part when longer);
    texts with Urdu/emoji/other symbols: 70 chars = 1 (67 per part)."""
    text = text or ""
    if not text:
        return 1
    if all(ch in _GSM7 or ch in _GSM7_EXT for ch in text):
        n = sum(2 if ch in _GSM7_EXT else 1 for ch in text)
        return 1 if n <= 160 else -(-n // 153)
    n = len(text.encode("utf-16-le")) // 2
    return 1 if n <= 70 else -(-n // 67)


def _sms_state(user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT sms_enabled, sms_limit, sms_used, phone_number,
                COALESCE(provider,'telnyx') AS provider
                FROM public.voxsbridge_users WHERE id=%s""", (user_id,))
            row = cur.fetchone() or {}
    limit = int(row.get("sms_limit") or 0)
    used = int(row.get("sms_used") or 0)
    return {
        "enabled": bool(row.get("sms_enabled")),
        "limit": limit, "used": used, "remaining": max(0, limit - used),
        "number": row.get("phone_number") or "",
        "line_ok": (row.get("provider") or "telnyx") == "telnyx",
        "configured": bool(TELNYX_API_KEY),
    }


def _sms_user(request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if user.get("role") == "admin":
        raise HTTPException(status_code=403, detail="Admins manage SMS from the customer page")
    return user


def _clean_peer(value):
    peer = re.sub(r"[^\d+]", "", str(value or ""))
    if not re.fullmatch(r"\+[1-9]\d{6,14}", peer):
        raise HTTPException(status_code=422, detail="Enter the number with its country code")
    return peer


def _provider_error_text(exc):
    """Readable reason without naming the carrier (white-label)."""
    raw = str(getattr(exc, "detail", "") or exc)
    try:
        body = json.loads(raw[raw.index("{"):])
        err = (body.get("errors") or [{}])[0]
        msg = err.get("detail") or err.get("title") or ""
    except (ValueError, TypeError, AttributeError):
        msg = ""
    msg = re.sub(r"(?i)telnyx", "carrier", msg)[:200]
    return msg or "The carrier did not accept this message"


@app.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    sms = _sms_state(user["id"])
    return templates.TemplateResponse(request=request, name="messages.html",
                                      context={"user": user, "sms": sms})


@app.get("/api/sms/summary")
async def sms_summary(request: Request):
    user = _sms_user(request)
    sms = _sms_state(user["id"])
    unread = 0
    if sms["enabled"]:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT count(*) AS n FROM public.voxsbridge_sms
                    WHERE user_id=%s AND direction='in' AND NOT is_read""", (user["id"],))
                unread = cur.fetchone()["n"]
    return JSONResponse({**sms, "unread": unread}, headers={"Cache-Control": "no-store"})


@app.get("/api/sms/threads")
async def sms_threads(request: Request):
    user = _sms_user(request)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT ON (peer) peer, body, direction, status, created_at
                FROM (SELECT CASE WHEN direction='out' THEN to_number ELSE from_number END AS peer,
                             body, direction, status, created_at
                      FROM public.voxsbridge_sms WHERE user_id=%s) t
                ORDER BY peer, created_at DESC""", (user["id"],))
            last = cur.fetchall()
            cur.execute("""SELECT from_number AS peer, count(*) AS n FROM public.voxsbridge_sms
                WHERE user_id=%s AND direction='in' AND NOT is_read GROUP BY from_number""", (user["id"],))
            unread = {r["peer"]: r["n"] for r in cur.fetchall()}
    threads = [{"peer": r["peer"], "last": (r["body"] or "")[:120], "direction": r["direction"],
                "status": r["status"], "at": r["created_at"].isoformat(),
                "unread": unread.get(r["peer"], 0)} for r in last]
    threads.sort(key=lambda t: t["at"], reverse=True)
    return JSONResponse({"threads": threads[:300]}, headers={"Cache-Control": "no-store"})


@app.get("/api/sms/thread")
async def sms_thread(request: Request):
    user = _sms_user(request)
    peer = _clean_peer(request.query_params.get("peer"))
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, direction, body, status, error, segments, created_at
                FROM public.voxsbridge_sms
                WHERE user_id=%s AND (CASE WHEN direction='out' THEN to_number ELSE from_number END)=%s
                ORDER BY created_at DESC LIMIT 300""", (user["id"], peer))
            rows = cur.fetchall()
            cur.execute("""UPDATE public.voxsbridge_sms SET is_read=TRUE
                WHERE user_id=%s AND direction='in' AND from_number=%s AND NOT is_read""", (user["id"], peer))
    messages = [{"id": str(r["id"]), "direction": r["direction"], "body": r["body"], "status": r["status"],
                 "error": r["error"] or "", "segments": r["segments"], "at": r["created_at"].isoformat()}
                for r in reversed(rows)]
    return JSONResponse({"peer": peer, "messages": messages}, headers={"Cache-Control": "no-store"})


@app.post("/api/sms/send")
async def sms_send(request: Request):
    user = _sms_user(request)
    data = await request.json()
    to = _clean_peer(data.get("to"))
    text = sms_clean(str(data.get("text") or "").replace("\r\n", "\n")).strip()
    if not text:
        raise HTTPException(status_code=422, detail="Type a message first")
    if len(text) > SMS_MAX_CHARS:
        raise HTTPException(status_code=422, detail=f"Message is too long (max {SMS_MAX_CHARS} characters)")
    sms = _sms_state(user["id"])
    if not sms["enabled"]:
        raise HTTPException(status_code=403, detail="Texting is not enabled for your account")
    if not sms["number"] or not sms["line_ok"]:
        raise HTTPException(status_code=403, detail="Your line cannot send text messages yet. Contact support.")
    if not sms["configured"]:
        raise HTTPException(status_code=503, detail="Texting is not configured yet. Contact support.")
    if to == sms["number"]:
        raise HTTPException(status_code=422, detail="You cannot text your own number")
    parts = sms_segments(text)
    # Reserve the allowance atomically so two tabs cannot go over the limit.
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.voxsbridge_users SET sms_used = sms_used + %s
                WHERE id=%s AND sms_enabled AND sms_used + %s <= sms_limit
                RETURNING sms_used, sms_limit""", (parts, user["id"], parts))
            reserved = cur.fetchone()
    if not reserved:
        left = sms["remaining"]
        raise HTTPException(status_code=402, detail=(
            f"Message limit reached. This text needs {parts} message{'s' if parts > 1 else ''} "
            f"and you have {left} left. Contact support to add more."))
    body = {"from": sms["number"], "to": to, "text": text}
    if SMS_PROFILE_ID:
        body["messaging_profile_id"] = SMS_PROFILE_ID
    try:
        resp = _telnyx_request("POST", "/v2/messages", body)
    except Exception as exc:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE public.voxsbridge_users SET sms_used = GREATEST(0, sms_used - %s) WHERE id=%s",
                            (parts, user["id"]))
        log.warning("SMS send failed for user %s: %s", user["id"], exc)
        raise HTTPException(status_code=502, detail="Message not sent: " + _provider_error_text(exc))
    msg = (resp or {}).get("data") or {}
    status = ((msg.get("to") or [{}])[0] or {}).get("status") or "queued"
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.voxsbridge_sms
                (user_id, direction, from_number, to_number, body, segments, status, provider_message_id, is_read)
                VALUES (%s,'out',%s,%s,%s,%s,%s,%s,TRUE)
                ON CONFLICT (provider_message_id) DO NOTHING
                RETURNING id, created_at""",
                (user["id"], sms["number"], to, text, parts, status, msg.get("id")))
            row = cur.fetchone()
    return {"ok": True, "segments": parts, "remaining": max(0, reserved["sms_limit"] - reserved["sms_used"]),
            "message": {"id": str(row["id"]) if row else "", "direction": "out", "body": text,
                        "status": status, "error": "", "segments": parts,
                        "at": (row["created_at"] if row else datetime.now(timezone.utc)).isoformat()}}


def _handle_sms_event(event, payload):
    """Telnyx messaging webhooks: inbound texts and delivery updates."""
    msg_id = payload.get("id")
    if event == "message.received":
        sender = ((payload.get("from") or {}).get("phone_number") or "").strip()
        to_list = payload.get("to") or []
        dest = ((to_list[0] if to_list else {}) or {}).get("phone_number", "").strip()
        text = str(payload.get("text") or "")[:SMS_MAX_CHARS * 2]
        media = payload.get("media") or []
        if media and not text:
            text = "[Picture/MMS message]"
        try:
            parts = int(payload.get("parts") or 0) or sms_segments(text)
        except (TypeError, ValueError):
            parts = sms_segments(text)
        if not dest or not msg_id:
            return
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT u.id FROM public.voxsbridge_users u
                    LEFT JOIN public.caller_id_assignments a ON a.user_id=u.id
                    WHERE (a.phone_number=%s OR u.phone_number=%s) AND u.is_active AND u.sms_enabled
                    ORDER BY (u.phone_number=%s) DESC LIMIT 1""", (dest, dest, dest))
                owner = cur.fetchone()
                if not owner:
                    log.info("Inbound SMS to %s ignored: no SMS-enabled user", dest)
                    return
                cur.execute("""INSERT INTO public.voxsbridge_sms
                    (user_id, direction, from_number, to_number, body, segments, status, provider_message_id)
                    VALUES (%s,'in',%s,%s,%s,%s,'received',%s)
                    ON CONFLICT (provider_message_id) DO NOTHING RETURNING id""",
                    (owner["id"], sender or "Unknown", dest, text, parts, msg_id))
                if cur.fetchone():
                    # Incoming texts count against the allowance too.
                    cur.execute("UPDATE public.voxsbridge_users SET sms_used = sms_used + %s WHERE id=%s",
                                (parts, owner["id"]))
        return
    if event in ("message.sent", "message.finalized") and msg_id:
        to_list = payload.get("to") or []
        status = ((to_list[0] if to_list else {}) or {}).get("status") or event.split(".")[1]
        errors = payload.get("errors") or []
        err = ""
        if errors:
            err = re.sub(r"(?i)telnyx", "carrier", str(errors[0].get("detail") or errors[0].get("title") or ""))[:200]
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE public.voxsbridge_sms SET status=%s, error=%s
                    WHERE provider_message_id=%s""", (status, err, msg_id))


@app.post("/webhooks/telnyx-sms")
async def telnyx_sms_webhook(request: Request):
    raw = await request.body()
    if not verify_telnyx_webhook(raw, request.headers.get("telnyx-signature-ed25519", ""),
                                 request.headers.get("telnyx-timestamp", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        data = (json.loads(raw).get("data") or {})
    except (ValueError, UnicodeDecodeError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    try:
        _handle_sms_event(data.get("event_type") or "", data.get("payload") or {})
    except Exception:
        log.exception("SMS webhook handling failed")
    return JSONResponse({"ok": True})


@app.post("/customers/{email}/sms")
async def customer_sms_update(request: Request, email: str, sms_enabled: str = Form("false"),
                              sms_limit: int = Form(0), add_messages: int = Form(0),
                              reset_used: str = Form("")):
    require_admin(request)
    if sms_limit < 0 or add_messages < 0 or sms_limit > 1000000 or add_messages > 1000000:
        raise HTTPException(422, "Enter a valid number of messages")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.voxsbridge_users
                SET sms_enabled=%s, sms_limit=%s + %s,
                    sms_used = CASE WHEN %s THEN 0 ELSE sms_used END
                WHERE lower(email)=lower(%s) AND role<>'admin' RETURNING id""",
                (sms_enabled == "true", sms_limit, add_messages, reset_used == "1", email))
            if not cur.fetchone():
                raise HTTPException(404, "Customer not found")
    return RedirectResponse('/customers/' + email + '?sms_saved=1', status_code=303)

# CRM and power dialer: user-scoped, opt-in only, PostgreSQL persistence.
CRM_STATUSES = {"new", "interested", "callback", "converted", "not_interested"}

def _crm_number(raw):
    value = re.sub(r"[\s().-]", "", str(raw or ""))
    if not re.fullmatch(r"\+[1-9]\d{7,14}", value):
        raise HTTPException(status_code=422, detail="Phone must be E.164, e.g. +12025550123")
    return value

def _crm_row(row):
    return {k: (str(v) if isinstance(v, uuid.UUID) else v.isoformat() if isinstance(v, (datetime, date)) else v)
            for k, v in dict(row).items()}

@app.get("/crm", response_class=HTMLResponse)
async def crm_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
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

@app.delete("/api/crm/leads/{lead_id}")
async def crm_delete_lead(lead_id: uuid.UUID, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    with db_connect() as conn:
        with conn.cursor() as cur:
            # Preserve queue history: a queued lead cannot be deleted until its
            # queue references have been cleared through the queue workflow.
            cur.execute("""SELECT 1 FROM public.power_queue_items i
                JOIN public.crm_leads l ON l.id=i.lead_id
                WHERE l.id=%s AND l.user_id=%s LIMIT 1""", (lead_id, user["id"]))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="Lead belongs to a power dialer queue; cannot delete while queue history references it")
            cur.execute("DELETE FROM public.crm_leads WHERE id=%s AND user_id=%s RETURNING id", (lead_id, user["id"]))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Lead not found")
    return {"deleted": True}

@app.post("/api/crm/import")
async def crm_import(request: Request, file: UploadFile = File(...)):
    user = get_current_user(request)
    if not user: raise HTTPException(status_code=401,detail="Login required")
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=422,detail="Upload CSV file (export Excel as CSV)")
    content = await file.read(250001)
    if len(content)>250000: raise HTTPException(status_code=413,detail="CSV exceeds 250 KB")
    try: reader = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    except (UnicodeError,csv.Error): raise HTTPException(status_code=422,detail="Invalid UTF-8 CSV")
    if len(reader)>500: raise HTTPException(status_code=422,detail="Maximum 500 rows per import")
    added=0; skipped=0
    with db_connect() as conn:
        with conn.cursor() as cur:
            for r in reader:
                # Import only leads with an explicit affirmative consent field.
                if str(r.get("consent","")).strip().lower() not in ("yes","true","1"):
                    skipped+=1; continue
                if str(r.get("do_not_call","")).strip().lower() in ("yes","true","1"):
                    skipped+=1; continue
                try: phone=_crm_number(r.get("phone"))
                except HTTPException: skipped+=1; continue
                name=str(r.get("name") or "").strip()[:150]
                if not name: skipped+=1; continue
                cur.execute("""INSERT INTO public.crm_leads(user_id,name,phone,email,consent)
                    VALUES(%s,%s,%s,%s,TRUE) ON CONFLICT(user_id,phone) DO NOTHING RETURNING id""",
                    (user["id"],name,phone,str(r.get("email") or "")[:254]))
                if cur.fetchone(): added+=1
                else: skipped+=1
    return {"added":added,"skipped":skipped}

@app.get("/power-dialer", response_class=HTMLResponse)
async def power_dialer_page(request: Request):
    user=get_current_user(request)
    if not user: return RedirectResponse(url="/login",status_code=302)
    return templates.TemplateResponse(request=request,name="power_dialer.html",context={"user":user})

@app.post("/api/power/queue")
async def power_queue_create(request: Request):
    user=get_current_user(request)
    if not user: raise HTTPException(status_code=401,detail="Login required")
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
            cur.execute("""SELECT i.id,i.position,i.result,i.finished_at,i.call_notes,l.name,l.phone,l.status,l.consent,l.do_not_call
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
    return templates.TemplateResponse(request=request, name="followups.html", context={"user": user, "items": []})

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    context = {"user": user, "plan": plan_status(user)}
    if user.get("role") == "admin":
        context.update({
            "rates": get_rates(),
            "twilio_ready": twilio_enabled(),
            "storage_mode": "Supabase Storage (private bucket)" if storage.enabled else "Supabase database",
            "public_base_url": PUBLIC_BASE_URL,
            "saved": request.query_params.get("saved"),
        })
    return templates.TemplateResponse(request=request, name="settings.html", context=context)

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
            cur.execute("""SELECT g.telnyx_call_id, g.status, g.ring_call_id
                FROM public.incoming_call_groups g
                JOIN public.call_participants p ON p.group_id=g.id
                JOIN public.voxsbridge_users u ON u.id=p.user_id
                WHERE g.id=%s AND u.email=%s""", (group_id, user["email"]))
            group = cur.fetchone()
    if not group:
        raise HTTPException(status_code=403, detail="Not a participant")
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
                WHERE u.email=%s AND g.status='ringing'
                  AND g.created_at > NOW() - INTERVAL '3 minutes'
                ORDER BY g.created_at DESC LIMIT 5""", (user["email"],))
            rows = cur.fetchall()
    return {"pending": [{"id":str(r["id"]),"from":r["caller_number"],
                         "to":r["destination_number"],"created_at":r["created_at"].isoformat()}
                        for r in rows]}


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
