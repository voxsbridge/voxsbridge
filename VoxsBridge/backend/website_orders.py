"""Website orders (marketing site -> dialer).

The marketing website (voxsbridge.uk) never holds provider keys or touches the dialer
database. It calls the secured endpoints below with a shared secret
(env MARKETING_API_KEY, sent as the X-Marketing-Key header):

  GET  /webhooks/marketing/numbers            numbers that are free to sell
  POST /webhooks/marketing/hold               hold the numbers a buyer picked (10 min)
  POST /webhooks/marketing/orders             create PENDING accounts (inactive) for an order
  GET  /webhooks/marketing/orders/{ref}       order status
  POST /webhooks/marketing/orders/{ref}/paid  activate: 30-day plan, add-ons, numbers
  POST /webhooks/marketing/orders/{ref}/renew +30 days (card renewal)
  POST /webhooks/marketing/orders/{ref}/cancel delete a pending order, release numbers

Everything lives under /webhooks/ because that prefix is already exempt from the
browser CSRF check (these calls are server-to-server and use the shared secret instead).

Passwords are hashed here with the dialer's own hash function and are never stored
anywhere else. Pending accounts are inactive (is_active=FALSE, signup_status='pending')
so nobody can log in until the order is paid.

Admin pages: /users/pending (pending orders + website accounts that still need a number).
"""
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import List, Optional

from fastapi import Request, Form, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel

log = logging.getLogger("voxsbridge.website_orders")

MARKETING_API_KEY = os.getenv("MARKETING_API_KEY", "").strip()
MARKETING_URL = os.getenv("MARKETING_URL", "").strip().rstrip("/")   # e.g. https://voxsbridge-web.onrender.com
SELECTION_HOLD_MINUTES = 10      # while the buyer is choosing / paying by card
OFFLINE_HOLD_MINUTES = 60        # bank transfer / crypto, after the WhatsApp hand-off
PLAN_DAYS = 30

SCHEMA_SQL = """
-- 19: website orders (marketing site -> dialer)
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS signup_status text NOT NULL DEFAULT '';
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS signup_ref text;
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS signup_source text NOT NULL DEFAULT '';
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS signup_number text NOT NULL DEFAULT '';
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS signup_expires_at timestamptz;
-- Existing users keep Power Dialer / Campaigns (default TRUE); website orders set it from the add-on.
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS power_enabled boolean NOT NULL DEFAULT true;
CREATE INDEX IF NOT EXISTS voxsbridge_users_signup_ref_idx ON public.voxsbridge_users(signup_ref) WHERE signup_ref IS NOT NULL;
CREATE TABLE IF NOT EXISTS public.website_orders (
    ref text PRIMARY KEY,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','expired','cancelled')),
    payment_method text NOT NULL DEFAULT '',
    plan_slug text NOT NULL DEFAULT '',
    plan_name text NOT NULL DEFAULT '',
    company_name text NOT NULL DEFAULT '',
    account_id uuid,
    owner_email text NOT NULL DEFAULT '',
    whatsapp text NOT NULL DEFAULT '',
    addons jsonb NOT NULL DEFAULT '[]'::jsonb,
    toll_free boolean NOT NULL DEFAULT false,
    amount_usd numeric(12,2) NOT NULL DEFAULT 0,
    token text NOT NULL DEFAULT '',
    hold_expires_at timestamptz,
    paid_at timestamptz,
    paid_method text NOT NULL DEFAULT '',
    note text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS website_orders_status_idx ON public.website_orders(status, hold_expires_at);
CREATE TABLE IF NOT EXISTS public.number_holds (
    phone_number text PRIMARY KEY,
    token text NOT NULL,
    order_ref text,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS number_holds_token_idx ON public.number_holds(token);
ALTER TABLE public.website_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.number_holds ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.website_orders FROM anon, authenticated;
REVOKE ALL ON public.number_holds FROM anon, authenticated;
-- Payments can now carry a USD amount, the method and a reference (bank ref, Stripe id ...)
ALTER TABLE public.payments ADD COLUMN IF NOT EXISTS amount_usd numeric(12,2);
ALTER TABLE public.payments ADD COLUMN IF NOT EXISTS method text;
ALTER TABLE public.payments ADD COLUMN IF NOT EXISTS reference text;
-- SMS packs (100 messages each). NULL sms_credits = unlimited (existing users keep working as before).
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS sms_credits integer;
ALTER TABLE public.website_orders ADD COLUMN IF NOT EXISTS sms_packs integer NOT NULL DEFAULT 0;
-- A disabled user who buys again on the website is re-activated in place (history kept).
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS signup_reactivate boolean NOT NULL DEFAULT false;
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS signup_prev_account uuid;
-- Invoices: payment method (bank shows the UBL account; card shows the card name + renewal date)
DO $$ BEGIN
  IF to_regclass('public.voxsbridge_invoices') IS NOT NULL THEN
    ALTER TABLE public.voxsbridge_invoices ADD COLUMN IF NOT EXISTS payment_method text NOT NULL DEFAULT 'bank';
    ALTER TABLE public.voxsbridge_invoices ADD COLUMN IF NOT EXISTS card_name text NOT NULL DEFAULT '';
    ALTER TABLE public.voxsbridge_invoices ADD COLUMN IF NOT EXISTS renewal_date date;
    ALTER TABLE public.voxsbridge_invoices ADD COLUMN IF NOT EXISTS paid_pkr numeric NOT NULL DEFAULT 0;
  END IF;
END $$;
"""

# ------------------------------------------------------------------ number classes
TOLL_FREE_RE = re.compile(r"^\+(1(800|833|844|855|866|877|888)|44(800|808))")
NUMBER_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
REF_RE = re.compile(r"^DNY-[0-9]{6}-[0-9A-F]{12}$")
PLAN_PREFIX = {"usa-canada": "+1", "uk": "+44", "uk-to-europe": "+44", "australia": "+61", "japan": "+81"}


def is_toll_free(number):
    return bool(TOLL_FREE_RE.match(number or ""))


def region_match(plan, number):
    if plan == "global":
        return True
    prefix = PLAN_PREFIX.get(plan)
    return bool(prefix and (number or "").startswith(prefix))


def register(app, d):
    """d: helpers from main.py (kept as a dict so this module never imports main)."""
    db_connect = d["db_connect"]
    templates = d["templates"]
    get_current_user = d["get_current_user"]
    refresh_users = d["refresh_users"]
    hash_password = d["hash_password"]
    get_setting = d["get_setting"]

    # ------------------------------------------------------------- auth
    def need_key(request: Request):
        sent = request.headers.get("x-marketing-key", "")
        if not MARKETING_API_KEY or not sent or not hmac.compare_digest(sent, MARKETING_API_KEY):
            raise HTTPException(status_code=404, detail="Not found")

    def need_admin(request: Request):
        admin = get_current_user(request)
        if not admin or admin.get("role") != "admin":
            return None
        return admin

    # ------------------------------------------------------------- inventory
    def owned_numbers():
        """{number: 'telnyx'|'twilio'} for every number in the connected provider accounts."""
        owned, failures = {}, 0
        try:
            for n in d["telnyx_number_accounts"]():
                owned[n] = "telnyx"
        except HTTPException:
            failures += 1
        try:
            for n in d["twilio_number_accounts"]():
                owned.setdefault(n, "twilio")
        except HTTPException:
            failures += 1
        if failures >= 2 and not owned:
            raise HTTPException(status_code=503, detail="Number inventory is temporarily unavailable.")
        return owned

    def taken_numbers(cur):
        cur.execute("""SELECT phone_number AS n FROM public.caller_id_assignments WHERE phone_number IS NOT NULL
            UNION SELECT phone_number FROM public.voxsbridge_users WHERE COALESCE(phone_number,'')<>''
            UNION SELECT signup_number FROM public.voxsbridge_users
                  WHERE signup_status='pending' AND COALESCE(signup_number,'')<>''""")
        return {r["n"] for r in cur.fetchall()}

    def active_holds(cur):
        cur.execute("SELECT phone_number, token, order_ref FROM public.number_holds WHERE expires_at > now()")
        return {r["phone_number"]: r for r in cur.fetchall()}

    def available(cur, owned, plan, toll_free, token=""):
        taken = taken_numbers(cur)
        holds = active_holds(cur)
        out = []
        for n in owned:
            if n in taken or not region_match(plan, n) or is_toll_free(n) != bool(toll_free):
                continue
            h = holds.get(n)
            if h and h["token"] != token:
                continue
            out.append(n)
        return sorted(out)

    @app.get("/webhooks/marketing/numbers")
    def mk_numbers(request: Request, plan: str = "", toll_free: int = 0, token: str = ""):
        need_key(request)
        if toll_free and plan != "usa-canada":
            return JSONResponse({"numbers": []})
        owned = owned_numbers()
        with db_connect() as conn:
            with conn.cursor() as cur:
                nums = available(cur, owned, plan, bool(toll_free), token[:64])
        return JSONResponse({"numbers": nums}, headers={"Cache-Control": "no-store"})

    class HoldIn(BaseModel):
        token: str
        plan: str
        numbers: List[str] = []
        toll_free: bool = False

    @app.post("/webhooks/marketing/hold")
    def mk_hold(request: Request, body: HoldIn):
        need_key(request)
        token = body.token.strip()
        if not 8 <= len(token) <= 64:
            raise HTTPException(status_code=422, detail="Invalid hold token.")
        wanted = sorted({n for n in body.numbers if NUMBER_RE.match(n)})[:100]
        owned = owned_numbers()
        held, unavailable = [], []
        expires = None
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('web-number-holds'))")
                cur.execute("DELETE FROM public.number_holds WHERE expires_at <= now()")
                # Numbers the buyer no longer wants go back on sale immediately.
                cur.execute("""DELETE FROM public.number_holds
                    WHERE token=%s AND order_ref IS NULL AND NOT (phone_number = ANY(%s))""", (token, wanted))
                taken = taken_numbers(cur)
                holds = active_holds(cur)
                for n in wanted:
                    h = holds.get(n)
                    ok = (n in owned and n not in taken and region_match(body.plan, n)
                          and is_toll_free(n) == bool(body.toll_free)
                          and (not h or h["token"] == token))
                    if not ok:
                        unavailable.append(n)
                        continue
                    if not h:
                        cur.execute("""INSERT INTO public.number_holds(phone_number,token,expires_at)
                            VALUES (%s,%s, now() + make_interval(mins => %s))""", (n, token, SELECTION_HOLD_MINUTES))
                    held.append(n)
                if held:
                    cur.execute("SELECT min(expires_at) AS e FROM public.number_holds WHERE token=%s AND phone_number=ANY(%s)",
                                (token, held))
                    row = cur.fetchone()
                    expires = row["e"].isoformat() if row and row["e"] else None
        return JSONResponse({"held": held, "unavailable": unavailable, "expires_at": expires,
                             "minutes": SELECTION_HOLD_MINUTES})

    # ------------------------------------------------------------- order creation
    class MemberIn(BaseModel):
        name: str
        email: str
        password: str
        number: str = ""

    class OrderIn(BaseModel):
        ref: str
        token: str
        payment_method: str
        plan_slug: str
        plan_name: str
        company_name: str = ""
        whatsapp: str = ""
        addons: List[str] = []
        toll_free: bool = False
        sms_packs: int = 0
        amount_usd: float = 0
        hold_minutes: int = SELECTION_HOLD_MINUTES
        members: List[MemberIn]

    def order_summary(cur, ref):
        cur.execute("""SELECT name,email,account_role,signup_status,signup_number,phone_number,is_active
            FROM public.voxsbridge_users WHERE signup_ref=%s ORDER BY (account_role='owner') DESC, created_at""", (ref,))
        return [dict(r) for r in cur.fetchall()]

    @app.post("/webhooks/marketing/orders")
    def mk_create_order(request: Request, body: OrderIn):
        need_key(request)
        if not REF_RE.match(body.ref):
            raise HTTPException(status_code=422, detail="Invalid order reference.")
        if body.payment_method not in ("stripe", "bank", "usdt"):
            raise HTTPException(status_code=422, detail="Invalid payment method.")
        if not 1 <= len(body.members) <= 100:
            raise HTTPException(status_code=422, detail="An order needs between 1 and 100 users.")
        token = body.token.strip()
        if not 8 <= len(token) <= 64:
            raise HTTPException(status_code=422, detail="Invalid hold token.")
        minutes = max(5, min(int(body.hold_minutes), 120))
        emails, numbers = [], set()
        for m in body.members:
            m.name, m.email, m.number = m.name.strip(), m.email.strip().lower(), m.number.strip()
            if len(m.name) < 2 or len(m.name) > 150:
                raise HTTPException(status_code=422, detail="Every user needs a name.")
            if not EMAIL_RE.match(m.email) or len(m.email) > 254:
                raise HTTPException(status_code=422, detail="Enter a valid email address for every user.")
            if not 12 <= len(m.password) <= 128 or any(ord(c) < 32 for c in m.password):
                raise HTTPException(status_code=422, detail="Every password needs at least 12 characters.")
            if m.number and not NUMBER_RE.match(m.number):
                raise HTTPException(status_code=422, detail="Invalid number selected.")
            if m.number:
                if not region_match(body.plan_slug, m.number) or is_toll_free(m.number) != bool(body.toll_free):
                    raise HTTPException(status_code=422, detail="A selected number does not match this plan.")
                numbers.add(m.number)
            emails.append(m.email)
        if len(set(emails)) != len(emails):
            raise HTTPException(status_code=422, detail="Each user needs a different email address.")

        owned = owned_numbers() if numbers else {}
        passwords = {m.email: hash_password(m.password) for m in body.members}   # slow on purpose, outside the transaction

        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('web-number-holds'))")
                cur.execute("SELECT ref, status, hold_expires_at FROM public.website_orders WHERE ref=%s", (body.ref,))
                existing = cur.fetchone()
                if existing:   # idempotent retry
                    return JSONResponse({"ok": True, "ref": body.ref, "status": existing["status"], "duplicate": True,
                                         "expires_at": existing["hold_expires_at"].isoformat() if existing["hold_expires_at"] else None,
                                         "users": [{"email": u["email"], "number": u["signup_number"]} for u in order_summary(cur, body.ref)]})
                cur.execute("""SELECT lower(email) AS e, id, is_active, role, signup_status, account_id
                    FROM public.voxsbridge_users WHERE lower(email)=ANY(%s) FOR UPDATE""", (emails,))
                found = {r["e"]: r for r in cur.fetchall()}
                # A DISABLED customer account (switched off by admin) may buy again and is re-activated.
                used = [e for e, r in found.items()
                        if r["is_active"] or r["role"] == "admin" or r["signup_status"] == "pending"]
                if used:
                    raise HTTPException(status_code=409, detail="This email already has a VoxsBridge account: "
                                        + ", ".join(used) + ". Use a different email.")
                if numbers:
                    cur.execute("DELETE FROM public.number_holds WHERE expires_at <= now()")
                    taken = taken_numbers(cur)
                    holds = active_holds(cur)
                    bad = [n for n in sorted(numbers)
                           if n not in owned or n in taken or (holds.get(n) and holds[n]["token"] != token)]
                    if bad:
                        raise HTTPException(status_code=409, detail="UNAVAILABLE:" + ",".join(bad))
                    for n in sorted(numbers):
                        cur.execute("""INSERT INTO public.number_holds(phone_number,token,order_ref,expires_at)
                            VALUES (%s,%s,%s, now() + make_interval(mins => %s))
                            ON CONFLICT (phone_number) DO UPDATE
                              SET order_ref=EXCLUDED.order_ref, token=EXCLUDED.token, expires_at=EXCLUDED.expires_at""",
                                    (n, token, body.ref, minutes))
                    # Drop any other holds this buyer had that are not part of the order.
                    cur.execute("DELETE FROM public.number_holds WHERE token=%s AND order_ref IS NULL", (token,))
                owner = body.members[0]
                company = (body.company_name.strip() or owner.name)[:200]
                cur.execute("INSERT INTO public.voxsbridge_accounts(name, notes) VALUES (%s,%s) RETURNING id",
                            (company, "Website order " + body.ref))
                account_id = cur.fetchone()["id"]
                cur.execute("""INSERT INTO public.website_orders
                    (ref,status,payment_method,plan_slug,plan_name,company_name,account_id,owner_email,whatsapp,
                     addons,toll_free,amount_usd,token,sms_packs,hold_expires_at)
                    VALUES (%s,'pending',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now() + make_interval(mins => %s))
                    RETURNING hold_expires_at""",
                            (body.ref, body.payment_method, body.plan_slug, body.plan_name[:120], company, account_id,
                             owner.email, body.whatsapp[:40], Jsonb(sorted(set(body.addons))), bool(body.toll_free),
                             round(max(0.0, body.amount_usd), 2), token, max(0, min(int(body.sms_packs), 100)), minutes))
                expires = cur.fetchone()["hold_expires_at"]
                for i, m in enumerate(body.members):
                    provider = "twilio" if owned.get(m.number) == "twilio" else "telnyx"
                    old = found.get(m.email)
                    if old:   # re-activate the disabled account in place; its history stays
                        cur.execute("""UPDATE public.voxsbridge_users SET name=%s, password_hash=%s, plan_name=%s,
                            provider=%s, signup_prev_account=account_id, account_id=%s, account_role=%s,
                            signup_status='pending', signup_ref=%s, signup_source='website', signup_number=%s,
                            signup_expires_at=%s, signup_reactivate=TRUE, power_enabled=FALSE, sms_enabled=FALSE,
                            recording_premium=FALSE, phone_number=NULL WHERE id=%s""",
                                    (m.name, passwords[m.email], body.plan_name[:120], provider, account_id,
                                     "owner" if i == 0 else "agent", body.ref, m.number, expires, old["id"]))
                        continue
                    cur.execute("""INSERT INTO public.voxsbridge_users
                        (name,email,password_hash,role,phone_number,credential_id,sip_username,plan_name,plan_start,plan_renewal,
                         last_paid_amount,recording_premium,dnd_enabled,is_demo,provider,account_id,account_role,user_type,
                         is_active,signup_status,signup_ref,signup_source,signup_number,signup_expires_at,power_enabled)
                        VALUES (%s,%s,%s,'agent',NULL,NULL,NULL,%s,CURRENT_DATE,CURRENT_DATE,0,FALSE,FALSE,FALSE,%s,%s,%s,'customer',
                                FALSE,'pending',%s,'website',%s,%s,FALSE)""",
                                (m.name, m.email, passwords[m.email], body.plan_name[:120], provider, account_id,
                                 "owner" if i == 0 else "agent", body.ref, m.number, expires))
                return JSONResponse({"ok": True, "ref": body.ref, "status": "pending", "expires_at": expires.isoformat(),
                                     "users": [{"email": m.email, "number": m.number} for m in body.members]})

    # ------------------------------------------------------------- activation
    def usd_to_pkr(amount):
        """USD -> PKR with the rate from Settings (1 USD = PKR). Falls back to 280 so it is never 0."""
        try:
            rate = float(d["get_rates"]().get("usd_to_pkr") or 0)
        except Exception:
            rate = 0
        return int(round(float(amount or 0) * (rate if rate > 0 else 280.0)))

    def activate_order(ref, method="manual", amount_usd=None, reference="", actor="system"):
        """Pending -> active. Safe to call twice. Returns a dict for the caller."""
        owned = owned_numbers()           # network call, before the transaction
        results, post, owner_email = [], [], ""
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM public.website_orders WHERE ref=%s FOR UPDATE", (ref,))
                o = cur.fetchone()
                if not o:
                    raise LookupError("Order not found.")
                if o["status"] == "paid":
                    users = order_summary(cur, ref)
                    return {"status": "paid", "already": True,
                            "users": [{"email": u["email"], "number": u["phone_number"] or "", "numberless": not u["phone_number"]} for u in users]}
                if o["status"] != "pending":
                    raise ValueError("This order has expired or was cancelled.")
                addons = set(o["addons"] or [])
                packs = int(o["sms_packs"] or 0)
                amount = float(amount_usd) if amount_usd not in (None, "") else float(o["amount_usd"] or 0)
                pkr = usd_to_pkr(amount)
                today = date.today()
                renewal = today + timedelta(days=PLAN_DAYS)
                cur.execute("""SELECT id,email,account_role,signup_number,provider FROM public.voxsbridge_users
                    WHERE signup_ref=%s AND signup_status='pending' FOR UPDATE""", (ref,))
                members = cur.fetchall()
                if not members:
                    raise ValueError("This order has no pending users.")
                owner_id = next((m["id"] for m in members if m["account_role"] == "owner"), members[0]["id"])
                owner_email = next((m["email"] for m in members if m["id"] == owner_id), "")
                for m in sorted(members, key=lambda r: r["account_role"] != "owner"):
                    number, provider, numberless = m["signup_number"] or "", m["provider"] or "telnyx", False
                    if number:
                        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("caller-id:" + number,))
                        cur.execute("""SELECT 1 FROM public.voxsbridge_users u
                            LEFT JOIN public.caller_id_assignments a ON a.user_id=u.id
                            WHERE (a.phone_number=%s OR u.phone_number=%s) AND u.signup_ref IS DISTINCT FROM %s LIMIT 1""",
                                    (number, number, ref))
                        if number not in owned or cur.fetchone():
                            number, numberless = "", True
                        else:
                            provider = "twilio" if owned[number] == "twilio" else "telnyx"
                    else:
                        numberless = True
                    cur.execute("""UPDATE public.voxsbridge_users SET
                        is_active=TRUE, signup_status='active', signup_expires_at=NULL,
                        phone_number=%s, provider=%s, plan_start=%s, plan_renewal=%s,
                        recording_premium=%s, sms_enabled=%s, power_enabled=%s,
                        sms_credits=%s, signup_reactivate=FALSE, signup_prev_account=NULL,
                        last_paid_amount=CASE WHEN id=%s THEN %s ELSE last_paid_amount END
                        WHERE id=%s""",
                                (number or None, provider, today, renewal,
                                 "recording" in addons,
                                 "sms" in addons or (packs > 0 and m["id"] == owner_id),
                                 "campaigns" in addons,
                                 (packs * 100) if (packs > 0 and m["id"] == owner_id) else None,
                                 owner_id, pkr, m["id"]))
                    if number:
                        cur.execute("""INSERT INTO public.caller_id_assignments(user_id,phone_number)
                            VALUES (%s,%s) ON CONFLICT (user_id,phone_number) DO NOTHING""", (m["id"], number))
                        post.append((m["id"], m["email"], number, provider))
                    results.append({"email": m["email"], "number": number, "numberless": numberless})
                cur.execute("""INSERT INTO public.payments(user_id,amount_pkr,status,paid_at,renewal_date,amount_usd,method,reference)
                    VALUES (%s,%s,'paid',now(),%s,%s,%s,%s)""",
                            (owner_id, pkr, renewal, round(amount, 2), method, (reference or ref)[:120]))
                cur.execute("DELETE FROM public.number_holds WHERE order_ref=%s", (ref,))
                missing = [r["email"] for r in results if r["numberless"]]
                cur.execute("""UPDATE public.website_orders SET status='paid', paid_at=now(), paid_method=%s,
                    hold_expires_at=NULL, note=%s WHERE ref=%s""",
                            (method, ("No number for: " + ", ".join(missing)) if missing else "", ref))
        refresh_users()
        # Provider set-up (best effort: a failure here never undoes the payment).
        seen_twilio = set()
        for uid, email, number, provider in post:
            try:
                if provider == "twilio":
                    d["_set_user_twilio_account"](uid, number)
                    if number not in seen_twilio:
                        seen_twilio.add(number)
                        d["_twilio_configure_number"](number)
                else:
                    acct = d["telnyx_number_accounts"]().get(number, "t1")
                    d["_set_user_telnyx_account"](uid, acct)
                    refresh_users()
                    with d["tx_ctx"](acct):
                        d["_ensure_credential"](email)
            except Exception:
                log.exception("Provider set-up failed for %s (order %s)", email, ref)
        refresh_users()
        log.info("Website order %s activated by %s (%s)", ref, actor, method)
        return {"status": "paid", "already": False, "owner": owner_email, "users": results}

    def notify_marketing(ref, event, extra=None):
        """Tell the marketing site an order changed here (best effort, background)."""
        if not (MARKETING_URL and MARKETING_API_KEY):
            return

        def run():
            try:
                req = urllib.request.Request(
                    f"{MARKETING_URL}/api/internal/orders/{ref}/{event}",
                    data=json.dumps(extra or {}).encode(), method="POST",
                    headers={"Content-Type": "application/json", "X-Marketing-Key": MARKETING_API_KEY})
                urllib.request.urlopen(req, timeout=15).read()
            except Exception as exc:
                log.warning("Could not tell the marketing site about %s/%s: %s", ref, event, type(exc).__name__)
        threading.Thread(target=run, daemon=True).start()

    class PaidIn(BaseModel):
        method: str = "stripe"
        amount_usd: Optional[float] = None
        reference: str = ""

    @app.post("/webhooks/marketing/orders/{ref}/paid")
    def mk_paid(ref: str, request: Request, body: PaidIn):
        need_key(request)
        try:
            return JSONResponse(activate_order(ref, body.method[:30], body.amount_usd, body.reference, "marketing"))
        except LookupError:
            raise HTTPException(status_code=404, detail="Order not found.")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    class RenewIn(BaseModel):
        amount_usd: float = 0
        reference: str = ""

    @app.post("/webhooks/marketing/orders/{ref}/renew")
    def mk_renew(ref: str, request: Request, body: RenewIn):
        need_key(request)
        pkr = usd_to_pkr(body.amount_usd)
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM public.website_orders WHERE ref=%s FOR UPDATE", (ref,))
                o = cur.fetchone()
                if not o or o["status"] != "paid":
                    raise HTTPException(status_code=409, detail="Order is not active.")
                cur.execute("""UPDATE public.voxsbridge_users
                    SET plan_renewal = GREATEST(COALESCE(plan_renewal, CURRENT_DATE), CURRENT_DATE) + %s
                    WHERE signup_ref=%s AND is_active RETURNING id, account_role, plan_renewal""", (PLAN_DAYS, ref))
                rows = cur.fetchall()
                owner = next((r for r in rows if r["account_role"] == "owner"), rows[0] if rows else None)
                if owner:
                    cur.execute("""INSERT INTO public.payments(user_id,amount_pkr,status,paid_at,renewal_date,amount_usd,method,reference)
                        VALUES (%s,%s,'paid',now(),%s,%s,'stripe',%s)""",
                                (owner["id"], pkr, owner["plan_renewal"], round(body.amount_usd, 2), body.reference[:120]))
        refresh_users()
        return {"ok": True, "users": len(rows)}

    def expire_or_cancel(ref, status):
        """Delete a pending order's accounts and release its numbers. Returns the final status."""
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, account_id FROM public.website_orders WHERE ref=%s FOR UPDATE", (ref,))
                o = cur.fetchone()
                if not o:
                    return "unknown"
                if o["status"] != "pending":
                    return o["status"]
                cur.execute("""UPDATE public.voxsbridge_users SET signup_status='', signup_ref=NULL, signup_number='',
                    signup_expires_at=NULL, signup_reactivate=FALSE, account_id=signup_prev_account, signup_prev_account=NULL
                    WHERE signup_ref=%s AND signup_status='pending' AND signup_reactivate""", (ref,))
                cur.execute("DELETE FROM public.voxsbridge_users WHERE signup_ref=%s AND signup_status='pending'", (ref,))
                if o["account_id"]:
                    cur.execute("""DELETE FROM public.voxsbridge_accounts a WHERE a.id=%s
                        AND NOT EXISTS (SELECT 1 FROM public.voxsbridge_users u WHERE u.account_id=a.id)""", (o["account_id"],))
                cur.execute("DELETE FROM public.number_holds WHERE order_ref=%s", (ref,))
                cur.execute("UPDATE public.website_orders SET status=%s, hold_expires_at=NULL WHERE ref=%s", (status, ref))
                return status

    @app.post("/webhooks/marketing/orders/{ref}/cancel")
    def mk_cancel(ref: str, request: Request):
        need_key(request)
        return {"status": expire_or_cancel(ref, "cancelled")}

    @app.get("/webhooks/marketing/orders/{ref}")
    def mk_status(ref: str, request: Request):
        need_key(request)
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, hold_expires_at, note FROM public.website_orders WHERE ref=%s", (ref,))
                o = cur.fetchone()
                if not o:
                    return {"status": "unknown"}
                users = order_summary(cur, ref)
        return {"status": o["status"], "note": o["note"],
                "expires_at": o["hold_expires_at"].isoformat() if o["hold_expires_at"] else None,
                "users": [{"email": u["email"], "number": u["phone_number"] or u["signup_number"] or "",
                           "numberless": u["is_active"] and not u["phone_number"]} for u in users]}

    # ------------------------------------------------------------- expiry sweeper
    def sweep():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.number_holds WHERE expires_at <= now() AND order_ref IS NULL")
                cur.execute("""SELECT ref FROM public.website_orders
                    WHERE status='pending' AND hold_expires_at IS NOT NULL AND hold_expires_at < now()""")
                refs = [r["ref"] for r in cur.fetchall()]
        for ref in refs:
            expire_or_cancel(ref, "expired")
            log.info("Website order %s expired; pending accounts and number hold removed", ref)

    def sweeper_loop():
        while True:
            time.sleep(45)
            try:
                if d["worker_should_run"]():
                    sweep()
            except Exception:
                log.exception("Website order sweeper failed")

    threading.Thread(target=sweeper_loop, daemon=True, name="website-order-sweeper").start()

    # ------------------------------------------------------------- admin pages
    def pending_orders():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT o.*, EXTRACT(EPOCH FROM (o.hold_expires_at - now())) AS secs_left
                    FROM public.website_orders o WHERE o.status='pending' ORDER BY o.created_at DESC""")
                orders = [dict(r) for r in cur.fetchall()]
                for o in orders:
                    o["users"] = order_summary(cur, o["ref"])
                    o["minutes_left"] = max(0, int((o["secs_left"] or 0) // 60))
                cur.execute("""SELECT u.name,u.email,u.signup_ref,u.created_at FROM public.voxsbridge_users u
                    WHERE u.is_active AND u.signup_source='website' AND COALESCE(u.phone_number,'')=''
                      AND u.user_type='customer' ORDER BY u.created_at DESC""")
                numberless = [dict(r) for r in cur.fetchall()]
        return orders, numberless

    @app.get("/users/pending")
    def pending_page(request: Request):
        admin = need_admin(request)
        if not admin:
            return RedirectResponse("/login", status_code=302)
        orders, numberless = pending_orders()
        return templates.TemplateResponse(request=request, name="pending_orders.html", context={
            "user": admin, "orders": orders, "numberless": numberless,
            "error": request.query_params.get("error"), "done": request.query_params.get("done")})

    @app.get("/users/website-orders")
    def website_orders_page(request: Request):
        """Everyone who bought from the website (paid orders) with their users, numbers and plan dates."""
        admin = need_admin(request)
        if not admin:
            return RedirectResponse("/login", status_code=302)
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT * FROM public.website_orders WHERE status='paid'
                    ORDER BY paid_at DESC NULLS LAST, created_at DESC LIMIT 500""")
                orders = [dict(r) for r in cur.fetchall()]
                for o in orders:
                    cur.execute("""SELECT name,email,account_role,phone_number,is_active,plan_renewal,sms_credits
                        FROM public.voxsbridge_users WHERE signup_ref=%s ORDER BY (account_role='owner') DESC, created_at""", (o["ref"],))
                    o["users"] = [dict(r) for r in cur.fetchall()]
        total = sum(float(o["amount_usd"] or 0) for o in orders)
        return templates.TemplateResponse(request=request, name="website_orders.html", context={
            "user": admin, "orders": orders, "total_usd": total})

    @app.post("/users/pending/{ref}/paid")
    def pending_mark_paid(ref: str, request: Request, method: str = Form("bank"), amount_usd: str = Form(""),
                          reference: str = Form("")):
        admin = need_admin(request)
        if not admin:
            raise HTTPException(status_code=403, detail="Admin only")
        try:
            amount = float(amount_usd) if amount_usd.strip() else None
        except ValueError:
            return RedirectResponse("/users/pending?error=Enter+a+valid+USD+amount", status_code=303)
        if method not in ("bank", "usdt", "stripe", "other"):
            method = "other"
        try:
            result = activate_order(ref, method, amount, reference.strip(), admin["email"])
        except LookupError:
            return RedirectResponse("/users/pending?error=Order+not+found", status_code=303)
        except ValueError as exc:
            return RedirectResponse("/users/pending?error=" + urllib.parse.quote(str(exc)), status_code=303)
        except HTTPException as exc:
            return RedirectResponse("/users/pending?error=" + urllib.parse.quote(str(exc.detail)), status_code=303)
        notify_marketing(ref, "paid", {"method": method, "amount_usd": amount, "reference": reference.strip(),
                                       "users": result.get("users", [])})
        return RedirectResponse("/users/pending?done=paid", status_code=303)

    @app.post("/users/pending/{ref}/cancel")
    def pending_cancel(ref: str, request: Request):
        admin = need_admin(request)
        if not admin:
            raise HTTPException(status_code=403, detail="Admin only")
        status = expire_or_cancel(ref, "cancelled")
        if status == "cancelled":
            notify_marketing(ref, "cancelled")
        return RedirectResponse("/users/pending?done=cancelled", status_code=303)

    def hold_info(numbers):
        """Used by the manual create-user page / API: which numbers are reserved for website buyers."""
        if not numbers:
            return {}
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT phone_number, order_ref FROM public.number_holds
                    WHERE expires_at > now() AND phone_number = ANY(%s)""", (list(numbers),))
                return {r["phone_number"]: r["order_ref"] for r in cur.fetchall()}

    def backfill_pkr():
        """One-off repair: card payments saved with PKR 0 get the converted amount."""
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    rate = usd_to_pkr(1)
                    cur.execute("""UPDATE public.payments SET amount_pkr=ROUND(amount_usd*%s)
                        WHERE COALESCE(amount_pkr,0)=0 AND COALESCE(amount_usd,0)>0""", (rate,))
                    cur.execute("""UPDATE public.voxsbridge_users u SET last_paid_amount=(
                            SELECT p.amount_pkr FROM public.payments p WHERE p.user_id=u.id AND p.status='paid'
                            ORDER BY p.paid_at DESC NULLS LAST LIMIT 1)
                        WHERE COALESCE(u.last_paid_amount,0)=0 AND EXISTS (
                            SELECT 1 FROM public.payments p WHERE p.user_id=u.id AND p.status='paid' AND p.amount_pkr>0)""")
        except Exception:
            log.exception("PKR backfill failed")

    threading.Thread(target=lambda: (time.sleep(20), backfill_pkr(), d["refresh_users"]()), daemon=True).start()

    return {"hold_info": hold_info, "activate_order": activate_order, "sweep": sweep}
