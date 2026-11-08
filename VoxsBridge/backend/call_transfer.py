"""Call transfer between company agents (plus Add Call / conference support).

How it works (Telnyx Call Control):

* Every live customer call that the server controls is kept in ``live_legs``:
  one row per agent leg (the leg ringing/bridged to an agent's browser) with
  the customer's leg next to it.
    - incoming calls: the customer leg is the inbound PSTN leg, the agent leg
      is the "ring" leg that was dialed to the agent's browser.
    - transferred calls: the new agent's leg.

* Transfer = dial the new agent (or a phone number) while the customer stays
  connected to the first agent and hears hold music. Only when the new agent
  answers is the customer re-bridged to them, so a "no answer", a "decline"
  or the 3-minute wait running out simply leaves the customer with the first
  agent (nothing to recover).

* If the new agent is busy on another call the transfer waits in a queue and
  rings them as soon as their call ends (max ``TRANSFER_WAIT_SECONDS``).
"""
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

log = logging.getLogger("voxsbridge.transfer")

TRANSFER_WAIT_SECONDS = int(os.getenv("TRANSFER_WAIT_SECONDS", "180"))   # busy agent: wait max 3 min
TRANSFER_RING_SECONDS = int(os.getenv("TRANSFER_RING_SECONDS", "30"))

SCHEMA_SQL = """
-- 10: call transfer between agents
CREATE TABLE IF NOT EXISTS public.live_legs (
    agent_leg text PRIMARY KEY,
    user_id uuid REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    customer_leg text NOT NULL,
    customer_number text NOT NULL DEFAULT '',
    kind text NOT NULL DEFAULT 'in',
    ref_id uuid,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz
);
CREATE INDEX IF NOT EXISTS live_legs_customer_idx ON public.live_legs(customer_leg) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS live_legs_user_idx ON public.live_legs(user_id) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS public.call_transfers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_leg text NOT NULL,
    customer_number text NOT NULL DEFAULT '',
    from_user uuid REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    from_leg text NOT NULL,
    to_user uuid REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    to_number text NOT NULL DEFAULT '',
    to_leg text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'queued',
    reason text NOT NULL DEFAULT '',
    group_id uuid,
    log_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    deadline timestamptz NOT NULL DEFAULT now() + interval '3 minutes',
    tx_account text NOT NULL DEFAULT 't1',
    ring_started_at timestamptz,
    answered_at timestamptz,
    ended_at timestamptz
);
ALTER TABLE public.call_transfers ADD COLUMN IF NOT EXISTS tx_account text NOT NULL DEFAULT 't1';
CREATE INDEX IF NOT EXISTS call_transfers_open_idx ON public.call_transfers(status) WHERE status IN ('queued','ringing');
CREATE INDEX IF NOT EXISTS call_transfers_to_idx ON public.call_transfers(to_user, status);
ALTER TABLE public.live_legs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.call_transfers ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.live_legs FROM anon, authenticated;
REVOKE ALL ON public.call_transfers FROM anon, authenticated;
"""

# Filled by setup(): db_connect, telnyx(method, path, body), cs_encode, base_url,
# presence_timeout.
D = {}


def setup(**deps):
    D.update(deps)
    if not D.get("no_worker"):
        threading.Thread(target=_worker, name="transfer-queue", daemon=True).start()


def _db():
    return D["db_connect"]()


def _tx(method, path, body=None):
    return D["telnyx"](method, path, body)


def _quiet(method, path, body=None):
    try:
        _tx(method, path, body or {})
        return True
    except Exception:
        log.info("Telnyx %s %s not accepted (call probably ended)", method, path)
        return False


def _hangup(leg):
    if leg:
        _quiet("POST", f"/v2/calls/{leg}/actions/hangup")


def _conf():
    if "conf" in D:
        return D["conf"]()
    return {"voice_app_id": os.getenv("TELNYX_VOICE_APP_ID", "").strip()}


def _acct():
    return D["current_account"]() if "current_account" in D else "t1"


def _in_acct(acct):
    """Run Telnyx calls on the given account (used by the background worker)."""
    import contextlib
    return D["account_ctx"](acct or "t1") if "account_ctx" in D else contextlib.nullcontext()


def voice_app():
    return _conf()["voice_app_id"]


def pstn_connection():
    """Connection used to dial phone numbers from the server (POST /v2/calls).
    Telnyx only accepts a Call Control App (Voice API app) here, never a SIP
    credential connection, and the app needs an Outbound Voice Profile."""
    return voice_app()


def hold_music_url():
    return os.getenv("HOLD_MUSIC_URL", "").strip() or (D.get("base_url", "") + "/static/audio/hold-music.mp3")


def e164(value):
    value = str(value or "")
    m = re.search(r"\+?\d{7,15}", value.replace(" ", "").replace("-", ""))
    if not m:
        return ""
    digits = m.group(0).lstrip("+")
    return "+" + digits if 7 <= len(digits) <= 15 and digits[0] != "0" else ""


def _same_number(a, b):
    da, db_ = re.sub(r"\D", "", a or ""), re.sub(r"\D", "", b or "")
    if not da or not db_:
        return False
    return da == db_ or (len(da) >= 7 and len(db_) >= 7 and (da.endswith(db_[-9:]) or db_.endswith(da[-9:])))


# ---------------------------------------------------------------- live legs
def add_live_leg(cur, agent_leg, user_id, customer_leg, number, kind, ref_id=None):
    if not agent_leg or not customer_leg or not user_id:
        return
    cur.execute("""INSERT INTO public.live_legs(agent_leg,user_id,customer_leg,customer_number,kind,ref_id)
        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (agent_leg) DO NOTHING""",
        (agent_leg, user_id, customer_leg, number or "", kind, ref_id))


def on_ring_answered(group_id, agent_leg):
    """Incoming call answered by an agent: remember who holds the customer."""
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT g.telnyx_call_id,g.caller_number,
                        COALESCE(g.answered_by,(SELECT p.user_id FROM public.call_participants p
                            WHERE p.group_id=g.id ORDER BY (p.status='answered') DESC LIMIT 1)) AS uid
                    FROM public.incoming_call_groups g WHERE g.id=%s""", (group_id,))
                g = cur.fetchone()
                if g and g["uid"]:
                    add_live_leg(cur, agent_leg, g["uid"], g["telnyx_call_id"], g["caller_number"], "in", group_id)
    except Exception:
        log.exception("Could not store live leg for group %s", group_id)


def agent_leg_gone(agent_leg):
    """An agent leg ended. Returns True when another agent now holds the
    customer (after a transfer), so the customer must NOT be hung up."""
    if not agent_leg:
        return False
    ringing = []
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.live_legs SET ended_at=now()
                WHERE agent_leg=%s AND ended_at IS NULL RETURNING customer_leg""", (agent_leg,))
            row = cur.fetchone()
            cur.execute("SELECT customer_leg FROM public.live_legs WHERE agent_leg=%s", (agent_leg,))
            known = cur.fetchone()
            customer = (row or known or {}).get("customer_leg")
            # The first agent hung up while the transfer was still waiting/ringing.
            cur.execute("""UPDATE public.call_transfers SET status='cancelled', reason='Agent hung up', ended_at=now()
                WHERE from_leg=%s AND status IN ('queued','ringing') RETURNING to_leg""", (agent_leg,))
            ringing = [r["to_leg"] for r in cur.fetchall() if r["to_leg"]]
            other = None
            if customer:
                cur.execute("""SELECT 1 FROM public.live_legs WHERE customer_leg=%s AND ended_at IS NULL
                    AND agent_leg<>%s LIMIT 1""", (customer, agent_leg))
                other = cur.fetchone()
                if not other:
                    # A transfer that was answered a moment ago also counts.
                    cur.execute("""SELECT 1 FROM public.call_transfers WHERE customer_leg=%s AND from_leg=%s
                        AND status='answered' LIMIT 1""", (customer, agent_leg))
                    other = cur.fetchone()
    for leg in ringing:
        _hangup(leg)
    return bool(other)


def customer_gone(customer_leg):
    """The customer hung up: close everything that belongs to that call."""
    if not customer_leg:
        return
    legs = []
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.live_legs SET ended_at=now()
                WHERE customer_leg=%s AND ended_at IS NULL RETURNING agent_leg""", (customer_leg,))
            legs = [r["agent_leg"] for r in cur.fetchall()]
            cur.execute("""UPDATE public.call_transfers SET status='cancelled', reason='Customer hung up', ended_at=now()
                WHERE customer_leg=%s AND status IN ('queued','ringing') RETURNING to_leg""", (customer_leg,))
            legs += [r["to_leg"] for r in cur.fetchall() if r["to_leg"]]
    for leg in legs:
        _hangup(leg)


def on_plain_hangup(call_id):
    """call.hangup for a leg without our client_state (usually an inbound customer leg)."""
    if not call_id:
        return
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public.live_legs WHERE agent_leg=%s AND ended_at IS NULL", (call_id,))
            is_agent = cur.fetchone()
    if is_agent:
        cur_customer = None
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT customer_leg FROM public.live_legs WHERE agent_leg=%s", (call_id,))
                r = cur.fetchone()
                cur_customer = r["customer_leg"] if r else None
        if not agent_leg_gone(call_id) and cur_customer:
            _hangup(cur_customer)
            customer_gone(cur_customer)
    customer_gone(call_id)


# ---------------------------------------------------------------- presence
def _free(cur, user_id):
    """Agent is online in the dialer and not on (or ringing for) a call."""
    cur.execute("""SELECT p.status, EXTRACT(EPOCH FROM (now()-p.updated_at))::int AS ago
        FROM public.user_presence p WHERE p.user_id=%s""", (user_id,))
    p = cur.fetchone()
    if not p or (p["ago"] or 0) > D.get("presence_timeout", 40) or p["status"] == "offline":
        return "offline"
    cur.execute("""SELECT 1 FROM public.live_legs WHERE user_id=%s AND ended_at IS NULL
        AND kind<>'ext' LIMIT 1""", (user_id,))
    if cur.fetchone() or p["status"] in ("on_call", "ringing", "dialing"):
        return "busy"
    cur.execute("""SELECT 1 FROM public.call_transfers WHERE to_user=%s AND status='ringing' LIMIT 1""", (user_id,))
    if cur.fetchone():
        return "busy"
    return "free"


def targets(user):
    """Company agents this user can transfer to, with live status."""
    if not user.get("account_id"):
        return []
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT u.id,u.name,u.email,u.phone_number,u.sip_username,u.account_role
                FROM public.voxsbridge_users u
                WHERE u.account_id=%s AND u.is_active AND u.id<>%s
                  AND COALESCE(u.provider,'telnyx')='telnyx'
                  AND COALESCE(u.telnyx_account,'t1')=%s
                ORDER BY lower(u.name)""", (user["account_id"], user["id"], user.get("telnyx_account") or "t1"))
            rows = cur.fetchall()
            out = []
            for r in rows:
                status = _free(cur, r["id"]) if r["sip_username"] else "offline"
                out.append({"id": str(r["id"]), "name": r["name"] or r["email"], "email": r["email"],
                            "role": r["account_role"], "status": status})
    order = {"free": 0, "busy": 1, "offline": 2}
    out.sort(key=lambda a: (order.get(a["status"], 3), a["name"].lower()))
    return out


# ---------------------------------------------------------------- hold music
def _hold_on(customer_leg):
    _quiet("POST", f"/v2/calls/{customer_leg}/actions/playback_start",
           {"audio_url": hold_music_url(), "loop": "infinity", "target_legs": "self"})


def _hold_off(customer_leg):
    _quiet("POST", f"/v2/calls/{customer_leg}/actions/playback_stop", {"stop": "all"})


# ---------------------------------------------------------------- transfer
class TransferError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def _find_live_leg(cur, user, number):
    cur.execute("""SELECT * FROM public.live_legs WHERE user_id=%s AND ended_at IS NULL
        AND kind<>'ext' ORDER BY started_at DESC""", (user["id"],))
    rows = cur.fetchall()
    if not rows:
        return None
    if number:
        for r in rows:
            if _same_number(r["customer_number"], number):
                return r
    return rows[0] if len(rows) == 1 else None


def start(user, number, to_user=None, to_number=None):
    if (user.get("provider") or "telnyx") != "telnyx":
        raise TransferError("Transfer is not available on this line yet")
    if not voice_app():
        raise TransferError("Transfer is not set up on the server (TELNYX_VOICE_APP_ID missing)", 503)
    to_number = e164(to_number) if to_number else ""
    with _db() as conn:
        with conn.cursor() as cur:
            leg = _find_live_leg(cur, user, number)
            if not leg:
                raise TransferError("Transfer works on incoming calls only.")
            cur.execute("""SELECT 1 FROM public.call_transfers WHERE customer_leg=%s
                AND status IN ('queued','ringing') LIMIT 1""", (leg["customer_leg"],))
            if cur.fetchone():
                raise TransferError("A transfer for this call is already in progress")
            target = None
            state = "ringing"
            if to_user:
                try:
                    to_user = str(uuid.UUID(str(to_user)))
                except ValueError:
                    raise TransferError("Agent not found", 404)
                cur.execute("""SELECT id,name,email,sip_username,phone_number FROM public.voxsbridge_users
                    WHERE id=%s AND account_id=%s AND is_active AND COALESCE(provider,'telnyx')='telnyx'
                      AND COALESCE(telnyx_account,'t1')=%s""",
                    (to_user, user.get("account_id") or None, user.get("telnyx_account") or "t1"))
                target = cur.fetchone()
                if not target or str(target["id"]) == user["id"]:
                    raise TransferError("Agent not found", 404)
                if not target["sip_username"]:
                    raise TransferError(f"{target['name']} has never opened the dialer yet")
                status = _free(cur, target["id"])
                if status == "offline":
                    raise TransferError(f"{target['name']} is offline right now")
                state = "ringing" if status == "free" else "queued"
            elif not to_number:
                raise TransferError("Pick an agent or enter a valid number", 422)
            elif D.get("call_allowed") and not D["call_allowed"](user, to_number):
                raise TransferError(D["call_block_message"](user, to_number), 403)
            elif _same_number(to_number, leg["customer_number"]):
                raise TransferError("That is the same number as the caller", 422)
            group_id = leg["ref_id"] if leg["kind"] == "in" else None
            cur.execute("""INSERT INTO public.call_transfers
                (customer_leg,customer_number,from_user,from_leg,to_user,to_number,status,group_id,deadline,tx_account)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now() + make_interval(secs => %s), %s) RETURNING id""",
                (leg["customer_leg"], leg["customer_number"], user["id"], leg["agent_leg"],
                 target["id"] if target else None, to_number, state, group_id, TRANSFER_WAIT_SECONDS, _acct()))
            tid = str(cur.fetchone()["id"])
    _hold_on(leg["customer_leg"])
    if state == "ringing":
        _ring(tid)
    return status_of(tid, user)


def _ring(tid):
    """Dial the new agent / number. The transfer row must be 'ringing'."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.call_transfers SET ring_started_at=now()
                WHERE id=%s AND status='ringing' RETURNING *""", (tid,))
            t = cur.fetchone()
            target = None
            if t and t["to_user"]:
                cur.execute("SELECT id,sip_username,phone_number FROM public.voxsbridge_users WHERE id=%s", (t["to_user"],))
                target = cur.fetchone()
            caller_id = ""
            if t:
                cur.execute("SELECT phone_number FROM public.voxsbridge_users WHERE id=%s", (t["from_user"],))
                fu = cur.fetchone()
                caller_id = (fu or {}).get("phone_number") or ""
    if not t:
        return
    if target:
        body = {"connection_id": voice_app(), "to": f"sip:{target['sip_username']}@sip.telnyx.com",
                "from": t["customer_number"] or caller_id}
    else:
        if not caller_id:
            return fail(tid, "No caller ID on this account")
        body = {"connection_id": pstn_connection(), "to": t["to_number"], "from": caller_id}
    body.update({"link_to": t["customer_leg"], "bridge_intent": True, "timeout_secs": TRANSFER_RING_SECONDS,
                 "command_id": "xfer-" + tid, "client_state": D["cs_encode"]("xfer:" + tid)})
    try:
        resp = _tx("POST", "/v2/calls", body)
        leg = ((resp or {}).get("data") or {}).get("call_control_id") or ""
    except Exception:
        log.exception("Transfer dial failed for %s", tid)
        return fail(tid, "Could not reach the agent")
    with _db() as conn:
        with conn.cursor() as cur:
            log_id = None
            if target:
                cur.execute("""INSERT INTO public.calls
                    (user_id,from_number,to_number,direction,status,provider,provider_call_id,created_at)
                    VALUES (%s,%s,%s,'incoming','ringing','telnyx',%s,NOW()) RETURNING id""",
                    (target["id"], t["customer_number"] or "Unknown", target["phone_number"] or "", leg or None))
                log_id = cur.fetchone()["id"]
            cur.execute("UPDATE public.call_transfers SET to_leg=%s, log_id=%s WHERE id=%s RETURNING status",
                        (leg, log_id, tid))
            still = cur.fetchone()
    if still and still["status"] != "ringing":      # cancelled while dialing
        _hangup(leg)


def fail(tid, reason):
    """Transfer did not happen: the customer stays with the first agent."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.call_transfers SET status='failed', reason=%s, ended_at=now()
                WHERE id=%s AND status IN ('queued','ringing') RETURNING customer_leg,to_leg,log_id""", (reason, tid))
            t = cur.fetchone()
            if t and t["log_id"]:
                cur.execute("UPDATE public.calls SET status='missed' WHERE id=%s AND status='ringing'", (t["log_id"],))
    if not t:
        return False
    _hold_off(t["customer_leg"])
    _hangup(t["to_leg"])
    return True


def cancel(tid, user):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.call_transfers SET status='cancelled', reason='Cancelled', ended_at=now()
                WHERE id=%s AND from_user=%s AND status IN ('queued','ringing')
                RETURNING customer_leg,to_leg,log_id""", (tid, user["id"]))
            t = cur.fetchone()
            if t and t["log_id"]:
                cur.execute("UPDATE public.calls SET status='missed' WHERE id=%s AND status='ringing'", (t["log_id"],))
    if not t:
        return False
    _hold_off(t["customer_leg"])
    _hangup(t["to_leg"])
    return True


def decline(tid, user):
    """The new agent pressed Decline: only their leg ends."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public.call_transfers WHERE id=%s AND to_user=%s", (tid, user["id"]))
            mine = cur.fetchone()
    if not mine:
        return False
    return fail(tid, "Declined")


def status_of(tid, user):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT t.*, u.name AS to_name,
                    GREATEST(0, EXTRACT(EPOCH FROM (t.deadline-now()))::int) AS left_secs
                FROM public.call_transfers t LEFT JOIN public.voxsbridge_users u ON u.id=t.to_user
                WHERE t.id=%s AND (t.from_user=%s OR t.to_user=%s)""", (tid, user["id"], user["id"]))
            t = cur.fetchone()
    if not t:
        return None
    return {"id": str(t["id"]), "status": t["status"], "reason": t["reason"],
            "to_name": t["to_name"] or t["to_number"], "to_number": t["to_number"],
            "seconds_left": int(t["left_secs"] or 0) if t["status"] == "queued" else 0}


def pending_for(user):
    """Transfers ringing this agent right now (shown as an incoming call)."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT t.id,t.customer_number,t.created_at,f.name AS from_name
                FROM public.call_transfers t JOIN public.voxsbridge_users f ON f.id=t.from_user
                WHERE t.to_user=%s AND t.status='ringing'
                  AND t.ring_started_at > now() - interval '2 minutes'
                ORDER BY t.created_at DESC LIMIT 3""", (user["id"],))
            return [{"id": "x-" + str(r["id"]), "from": r["customer_number"], "to": "",
                     "transfer_from": r["from_name"] or "", "created_at": r["created_at"].isoformat()}
                    for r in cur.fetchall()]


def _on_xfer_answered(tid, payload):
    leg = payload.get("call_control_id") or ""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.call_transfers SET status='answered', answered_at=now(),
                    to_leg=COALESCE(NULLIF(to_leg,''),%s)
                WHERE id=%s AND status='ringing' RETURNING *""", (leg, tid))
            t = cur.fetchone()
            if t and t["to_user"]:
                add_live_leg(cur, t["to_leg"], t["to_user"], t["customer_leg"], t["customer_number"], "xfer", t["id"])
    if not t:
        _hangup(leg)       # cancelled/failed meanwhile
        return
    _hold_off(t["customer_leg"])
    try:
        _tx("POST", f"/v2/calls/{t['to_leg']}/actions/bridge",
            {"call_control_id": t["customer_leg"], "park_after_unbridge": "self"})
    except Exception:
        log.exception("Transfer bridge failed for %s", tid)
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE public.live_legs SET ended_at=now() WHERE agent_leg=%s", (t["to_leg"],))
                cur.execute("""UPDATE public.call_transfers SET status='failed', reason='Could not connect', ended_at=now()
                    WHERE id=%s""", (tid,))
        _hangup(t["to_leg"])
        return
    with _db() as conn:
        with conn.cursor() as cur:
            if t["to_user"] is None:
                # Outside number now holds the customer: keep a live row so the
                # first agent's hangup does not end the call.
                add_live_leg(cur, t["to_leg"], t["from_user"], t["customer_leg"], t["customer_number"], "ext", t["id"])
            if t["log_id"]:
                cur.execute("UPDATE public.calls SET status='answered' WHERE id=%s", (t["log_id"],))
            if t["group_id"]:
                cur.execute("""UPDATE public.incoming_call_groups SET status='transferred'
                    WHERE id=%s AND status='answered' RETURNING call_log_id""", (t["group_id"],))
                g = cur.fetchone()
                if g and g["call_log_id"]:
                    cur.execute("""UPDATE public.calls SET status='completed',
                        duration_seconds=GREATEST(0,EXTRACT(EPOCH FROM (NOW()-created_at))::integer)
                        WHERE id=%s AND status IN ('ringing','answered')""", (g["call_log_id"],))
            cur.execute("UPDATE public.live_legs SET ended_at=now() WHERE agent_leg=%s AND ended_at IS NULL",
                        (t["from_leg"],))
    _hangup(t["from_leg"])     # the first agent is released


def _on_xfer_hangup(tid, payload):
    cause = str(payload.get("hangup_cause") or "").lower()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM public.call_transfers WHERE id=%s", (tid,))
            t = cur.fetchone()
    if not t:
        return
    if t["status"] == "ringing":
        reason = "Busy" if "busy" in cause else ("Declined" if "reject" in cause or "decline" in cause else "No answer")
        fail(tid, reason)
        return
    if t["status"] == "answered":
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE public.call_transfers SET status='done', ended_at=now() WHERE id=%s", (tid,))
                if t["log_id"]:
                    cur.execute("""UPDATE public.calls SET status='completed',
                        duration_seconds=GREATEST(0,EXTRACT(EPOCH FROM (NOW()-%s))::integer)
                        WHERE id=%s""", (t["answered_at"] or datetime.now(timezone.utc), t["log_id"]))
        if not agent_leg_gone(t["to_leg"]):
            _hangup(t["customer_leg"])
            customer_gone(t["customer_leg"])


# ---------------------------------------------------------------- webhook router
def handle_webhook(event, payload, client_state):
    """Returns True when the event was fully handled here."""
    if client_state.startswith("xfer:"):
        tid = client_state[5:]
        try:
            uuid.UUID(tid)
        except ValueError:
            return True
        if event == "call.answered":
            _on_xfer_answered(tid, payload)
        elif event == "call.hangup":
            _on_xfer_hangup(tid, payload)
        return True
    if event == "call.hangup" and not client_state:
        try:
            on_plain_hangup(payload.get("call_control_id"))
        except Exception:
            log.exception("Hangup bookkeeping failed")
    return False


# ---------------------------------------------------------------- queue worker
def tick():
    """Ring waiting transfers whose agent became free; give up after 3 min."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,to_user,tx_account,deadline < now() AS late FROM public.call_transfers
                WHERE status='queued' ORDER BY created_at LIMIT 50""")
            queued = cur.fetchall()
            cur.execute("""SELECT id,tx_account FROM public.call_transfers WHERE status='ringing'
                AND COALESCE(ring_started_at, created_at) < now() - make_interval(secs => %s)""",
                (TRANSFER_RING_SECONDS + 20,))
            stuck = cur.fetchall()
            ready = []
            for q in queued:
                if q["late"]:
                    continue
                if _free(cur, q["to_user"]) == "free":
                    cur.execute("""UPDATE public.call_transfers SET status='ringing'
                        WHERE id=%s AND status='queued' RETURNING id""", (q["id"],))
                    if cur.fetchone():
                        ready.append((str(q["id"]), q["tx_account"]))
    for q in queued:
        if q["late"]:
            with _in_acct(q["tx_account"]):
                fail(str(q["id"]), "Agent stayed busy for 3 minutes")
    for r in stuck:
        with _in_acct(r["tx_account"]):
            fail(str(r["id"]), "No answer")
    for tid, acct in ready:
        with _in_acct(acct):
            _ring(tid)
    return len(ready)


def _worker():
    time.sleep(5)
    while True:
        try:
            tick()
        except Exception:
            log.exception("Transfer queue tick failed")
        time.sleep(2)
