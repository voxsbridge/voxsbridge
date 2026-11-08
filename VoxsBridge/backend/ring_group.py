"""Shared numbers (ring groups).

One number can be assigned to several users of the SAME company. An incoming
call then rings every member who is online and free at the same time; the
first one to answer gets the call and the others stop ringing.

  * some members free           -> ring all free members for 30 s
  * nobody free, someone busy   -> caller hears hold music; the first member
                                   who becomes free is rung (max 3 minutes)
  * everybody offline / no answer / 3 minutes passed -> voicemail

Call logs: every member gets a log row while the call rings. When somebody
answers, only that member keeps the row; missed calls and voicemails stay in
every member's log.

Numbers with only one user keep the old single-user behaviour (main.py).
"""
import logging
import os
import secrets
import threading
import time

log = logging.getLogger("voxsbridge.ringgroup")

RING_SECONDS = int(os.getenv("RING_GROUP_RING_SECONDS", "30"))
QUEUE_SECONDS = int(os.getenv("RING_GROUP_QUEUE_SECONDS", "180"))

D = {}

SCHEMA_SQL = """
-- 14: shared numbers / ring groups
-- The ring group adds the 'waiting' participant status. Replace any old CHECK
-- constraint that forbids it (data is untouched).
ALTER TABLE public.call_participants DROP CONSTRAINT IF EXISTS call_participants_status_check;
ALTER TABLE public.call_participants ADD CONSTRAINT call_participants_status_check
    CHECK (status IN ('waiting','ringing','answered','cancelled','declined','missed'));
ALTER TABLE public.call_participants ADD COLUMN IF NOT EXISTS call_log_id uuid;
ALTER TABLE public.call_participants ADD COLUMN IF NOT EXISTS ring_call_id text;
ALTER TABLE public.call_participants ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now();
-- The ring group adds the 'queued' group status; allow it if a CHECK exists.
ALTER TABLE public.incoming_call_groups DROP CONSTRAINT IF EXISTS incoming_call_groups_status_check;
ALTER TABLE public.incoming_call_groups ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT 'telnyx';
ALTER TABLE public.incoming_call_groups ADD COLUMN IF NOT EXISTS line_account text;
ALTER TABLE public.incoming_call_groups ADD COLUMN IF NOT EXISTS is_shared boolean NOT NULL DEFAULT false;
ALTER TABLE public.incoming_call_groups ADD COLUMN IF NOT EXISTS queue_deadline timestamptz;
CREATE INDEX IF NOT EXISTS incoming_call_groups_shared_idx
    ON public.incoming_call_groups(status) WHERE is_shared;
-- A number may now belong to several users of one company: drop any unique
-- index that covers ONLY the phone number (the data itself is not touched).
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.relname AS tbl, i.relname AS idx, con.conname
    FROM pg_index x
    JOIN pg_class i ON i.oid = x.indexrelid
    JOIN pg_class c ON c.oid = x.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
    LEFT JOIN pg_constraint con ON con.conindid = x.indexrelid
    WHERE x.indisunique AND NOT x.indisprimary
      AND c.relname IN ('caller_id_assignments', 'voxsbridge_users')
      AND x.indnatts = 1
      AND (SELECT attname FROM pg_attribute WHERE attrelid = c.oid AND attnum = x.indkey[0]) = 'phone_number'
  LOOP
    BEGIN
      IF r.conname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT %I', r.tbl, r.conname);
      ELSE
        EXECUTE format('DROP INDEX public.%I', r.idx);
      END IF;
    EXCEPTION WHEN others THEN
      RAISE NOTICE 'Could not drop % on %', r.idx, r.tbl;
    END;
  END LOOP;
END $$;
"""


def setup(**deps):
    D.update(deps)
    if not D.get("no_worker"):
        threading.Thread(target=_worker, name="ring-group-queue", daemon=True).start()


def _db():
    return D["db_connect"]()


def _cs(text):
    return D["cs_encode"](text)


# ---------------------------------------------------------------- members
def members(cur, number, provider):
    """Active users that share this number, oldest first. A number counts as
    shared only when 2+ users of the SAME company have it."""
    cur.execute("""SELECT DISTINCT u.id,u.email,u.name,u.sip_username,u.dnd_enabled,u.plan_renewal,
            u.account_id,u.created_at,u.twilio_account,u.telnyx_account
        FROM public.caller_id_assignments a
        JOIN public.voxsbridge_users u ON u.id=a.user_id
        WHERE a.phone_number=%s AND u.is_active=TRUE AND COALESCE(u.provider,'telnyx')=%s
        ORDER BY u.created_at""", (number, provider))
    return cur.fetchall()


def is_shared(rows):
    if len(rows) < 2:
        return False
    accounts = {r["account_id"] for r in rows}
    return len(accounts) == 1 and None not in accounts


def _plan_ok(r):
    from datetime import date
    return not r["plan_renewal"] or r["plan_renewal"] >= date.today()


def _status(cur, uid, group_id=None):
    """free / busy / offline. Being rung for another call counts as busy."""
    st = D["free"](cur, uid)
    if st != "free":
        return st
    cur.execute("""SELECT 1 FROM public.call_participants p
        JOIN public.incoming_call_groups g ON g.id=p.group_id
        WHERE p.user_id=%s AND p.status='ringing' AND g.status IN ('ringing','queued')
          AND g.id IS DISTINCT FROM %s AND COALESCE(p.updated_at, g.created_at) > now() - interval '45 seconds'
        LIMIT 1""", (uid, group_id))
    return "busy" if cur.fetchone() else "free"


def classify(cur, rows, group_id=None, provider="telnyx"):
    free, busy = [], []
    for r in rows:
        if not _plan_ok(r) or r["dnd_enabled"]:
            continue
        if provider == "telnyx" and not r["sip_username"]:
            continue
        st = _status(cur, r["id"], group_id)
        if st == "free":
            free.append(r)
        elif st == "busy":
            busy.append(r)
    return free, busy


def create_group_logs(cur, group_id, rows, origin, destination, provider, call_sid):
    """One 'ringing' log row per member (plan valid); returns {user_id: log_id}."""
    logs = {}
    for r in rows:
        if not _plan_ok(r):
            continue
        cur.execute("""INSERT INTO public.calls
            (user_id,from_number,to_number,direction,status,provider,provider_call_id,created_at)
            VALUES (%s,%s,%s,'incoming','ringing',%s,%s,NOW()) RETURNING id""",
            (r["id"], origin or "Unknown", destination, provider, call_sid))
        log_id = cur.fetchone()["id"]
        logs[str(r["id"])] = log_id
        cur.execute("""INSERT INTO public.call_participants(group_id,user_id,status,call_log_id,updated_at)
            VALUES (%s,%s,'waiting',%s,now())
            ON CONFLICT (group_id,user_id) DO UPDATE SET call_log_id=EXCLUDED.call_log_id""",
            (group_id, r["id"], log_id))
    first = next(iter(logs.values()), None)
    cur.execute("""UPDATE public.incoming_call_groups SET is_shared=TRUE, provider=%s, line_account=%s,
        call_log_id=%s WHERE id=%s""", (provider, D["current_account"](provider), first, group_id))
    return logs


def mark_missed(cur, group_id):
    """Every member's log -> missed; nobody is ringing any more."""
    cur.execute("""UPDATE public.calls SET status='missed'
        WHERE id IN (SELECT call_log_id FROM public.call_participants WHERE group_id=%s)
          AND status='ringing'""", (group_id,))
    cur.execute("""UPDATE public.call_participants SET status='cancelled', updated_at=now()
        WHERE group_id=%s AND status IN ('ringing','waiting')""", (group_id,))


def _ringing_legs(cur, group_id, except_uid=None):
    cur.execute("""SELECT ring_call_id FROM public.call_participants
        WHERE group_id=%s AND status='ringing' AND ring_call_id IS NOT NULL
          AND user_id IS DISTINCT FROM %s""", (group_id, except_uid))
    return [r["ring_call_id"] for r in cur.fetchall()]


def claim(cur, group_id, uid):
    """Member `uid` answered. Returns True if they won the call."""
    cur.execute("""UPDATE public.incoming_call_groups SET status='answered', answered_by=%s
        WHERE id=%s AND (status IN ('ringing','queued') OR (status='answered' AND answered_by IS NULL))
        RETURNING id""", (uid, group_id))
    if not cur.fetchone():
        return False
    cur.execute("SELECT user_id,call_log_id FROM public.call_participants WHERE group_id=%s", (group_id,))
    parts = cur.fetchall()
    mine = next((p["call_log_id"] for p in parts if str(p["user_id"]) == str(uid)), None)
    others = [p["call_log_id"] for p in parts if str(p["user_id"]) != str(uid) and p["call_log_id"]]
    if mine:
        cur.execute("UPDATE public.calls SET status='answered' WHERE id=%s", (mine,))
        cur.execute("UPDATE public.incoming_call_groups SET call_log_id=%s WHERE id=%s", (mine, group_id))
    if others:
        # Answered calls are shown only to the member who took them.
        cur.execute("UPDATE public.call_participants SET call_log_id=NULL WHERE group_id=%s AND user_id<>%s",
                    (group_id, uid))
        cur.execute("DELETE FROM public.calls WHERE id = ANY(%s) AND status IN ('ringing','missed','answered')",
                    (others,))
    cur.execute("""UPDATE public.call_participants SET status=CASE WHEN user_id=%s THEN 'answered'
            WHEN status IN ('ringing','waiting') THEN 'cancelled' ELSE status END, updated_at=now()
        WHERE group_id=%s""", (uid, group_id))
    return True


def voicemail_targets(cur, group_id):
    """(user_id, call_log_id) of every member that should get the voicemail."""
    cur.execute("""SELECT user_id, call_log_id FROM public.call_participants
        WHERE group_id=%s AND call_log_id IS NOT NULL""", (group_id,))
    return [(r["user_id"], r["call_log_id"]) for r in cur.fetchall()]


# ---------------------------------------------------------------- Telnyx
def telnyx_start(group_id, call_id, origin, destination, rows):
    """call.initiated on a shared Telnyx number (inbound leg still parked)."""
    with _db() as conn:
        with conn.cursor() as cur:
            create_group_logs(cur, group_id, rows, origin, destination, "telnyx", call_id)
            free, busy = classify(cur, rows, group_id, "telnyx")
    if free:
        if _telnyx_ring(group_id, call_id, origin, free, parked=True):
            return
        D["voicemail_parked"](call_id, group_id)
    elif busy:
        _telnyx_queue(group_id, call_id)
    else:
        D["voicemail_parked"](call_id, group_id)


def _telnyx_ring(group_id, customer_leg, origin, users, parked):
    rang = 0
    for u in users:
        body = {
            "connection_id": D["voice_app"](),
            "to": f"sip:{u['sip_username']}@sip.telnyx.com",
            "from": origin or "",
            "link_to": customer_leg,
            "bridge_intent": True,
            "timeout_secs": RING_SECONDS,
            "command_id": f"rg-{group_id}-{u['id']}-{secrets.token_hex(3)}",
            "client_state": _cs(f"rg:{group_id}:{u['id']}"),
        }
        if parked:
            body.update({"bridge_on_answer": True, "prevent_double_bridge": True,
                         "park_after_unbridge": "self"})   # live listening can move the caller
        if not body["from"]:
            body.pop("from")
        try:
            resp = D["telnyx"]("POST", "/v2/calls", body)
            leg = ((resp or {}).get("data") or {}).get("call_control_id") or ""
        except Exception:
            log.exception("Ring-group dial failed for %s / %s", group_id, u["id"])
            continue
        rang += 1
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE public.call_participants SET status='ringing', ring_call_id=%s, updated_at=now()
                    WHERE group_id=%s AND user_id=%s""", (leg or None, group_id, u["id"]))
    return rang


def _telnyx_queue(group_id, customer_leg):
    """Everybody is busy: answer the caller and play hold music."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.incoming_call_groups SET status='queued',
                queue_deadline=now() + make_interval(secs => %s) WHERE id=%s AND status='ringing'
                RETURNING id""", (QUEUE_SECONDS, group_id))
            ok = cur.fetchone()
    if not ok:
        return
    try:
        D["telnyx"]("POST", f"/v2/calls/{customer_leg}/actions/answer", {"client_state": _cs(f"q:{group_id}")})
    except Exception:
        log.exception("Queue answer failed for %s", group_id)
        D["hangup"](customer_leg)


def _hold_on(leg):
    D["quiet"]("POST", f"/v2/calls/{leg}/actions/playback_start",
               {"audio_url": D["hold_music_url"](), "loop": "infinity", "target_legs": "self"})


def _hold_off(leg):
    D["quiet"]("POST", f"/v2/calls/{leg}/actions/playback_stop", {"stop": "all"})


def on_queue_event(event, group_id, payload):
    """Events of the caller's leg while it waits. Returns True if fully handled."""
    leg = payload.get("call_control_id") or ""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM public.incoming_call_groups WHERE id=%s", (group_id,))
            g = cur.fetchone()
    if not g:
        return True
    if event == "call.answered":
        if g["status"] == "queued":
            _hold_on(leg)
        return True
    if event == "call.hangup" and g["status"] == "queued":
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE public.incoming_call_groups SET status='missed', ended_at=now()
                    WHERE id=%s AND status='queued'""", (group_id,))
                mark_missed(cur, group_id)
                legs = _ringing_legs(cur, group_id)
        for x in legs:
            D["hangup"](x)
        return True
    return g["status"] != "answered" or event != "call.hangup"


def on_ring_event(event, group_id, uid, payload):
    """Events of the leg that rings one member's browser."""
    leg = payload.get("call_control_id") or ""
    if event == "call.answered":
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status,telnyx_call_id FROM public.incoming_call_groups WHERE id=%s FOR UPDATE",
                            (group_id,))
                g = cur.fetchone()
                was = g["status"] if g else ""
                others = _ringing_legs(cur, group_id, uid) if g else []
                won = bool(g) and claim(cur, group_id, uid)
                if won:
                    cur.execute("UPDATE public.call_participants SET ring_call_id=%s WHERE group_id=%s AND user_id=%s",
                                (leg, group_id, uid))
        if not won:
            D["hangup"](leg)
            return
        for x in others:
            D["hangup"](x)
        if was == "queued":
            _hold_off(g["telnyx_call_id"])
            try:
                D["telnyx"]("POST", f"/v2/calls/{leg}/actions/bridge",
                            {"call_control_id": g["telnyx_call_id"], "park_after_unbridge": "self"})
            except Exception:
                log.exception("Queue bridge failed for %s", group_id)
                D["hangup"](leg)
                return
        D["on_ring_answered"](group_id, leg)
        return
    if event != "call.hangup":
        return
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.call_participants SET status=CASE WHEN status='ringing' THEN 'missed' ELSE status END,
                updated_at=now() WHERE group_id=%s AND user_id=%s""", (group_id, uid))
            cur.execute("SELECT status,telnyx_call_id,answered_by FROM public.incoming_call_groups WHERE id=%s",
                        (group_id,))
            g = cur.fetchone()
            still = _ringing_legs(cur, group_id)
    if not g:
        return
    if g["status"] in ("answered", "transferred") and str(g["answered_by"]) == str(uid):
        # The member who had the call hung up.
        try:
            someone_else = D["agent_leg_gone"](leg)
        except Exception:
            log.exception("Transfer check failed for group %s", group_id)
            someone_else = g["status"] == "transferred"
        if not someone_else:
            D["hangup"](g["telnyx_call_id"])
    elif g["status"] == "ringing" and not still:
        D["voicemail_parked"](g["telnyx_call_id"], group_id)        # nobody picked up
    # queued: the worker rings the next free member or gives up after 3 min.


def decline(group_id, uid):
    """A member declined: only their leg stops while others still ring.
    Returns the ring leg to hang up, or None when the whole call should be declined."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT ring_call_id FROM public.call_participants
                WHERE group_id=%s AND user_id=%s""", (group_id, uid))
            mine = cur.fetchone()
            cur.execute("""UPDATE public.call_participants SET status='declined', updated_at=now()
                WHERE group_id=%s AND user_id=%s AND status='ringing'""", (group_id, uid))
    return (mine or {}).get("ring_call_id") or ""


def customer_gone(call_id):
    """The caller hung up before anyone answered (parked shared call)."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id FROM public.incoming_call_groups WHERE telnyx_call_id=%s AND is_shared
                AND status IN ('ringing','queued')""", (call_id,))
            g = cur.fetchone()
            if not g:
                return
            mark_missed_legs = _ringing_legs(cur, g["id"])
            mark_missed(cur, g["id"])
    for x in mark_missed_legs:
        D["hangup"](x)


def _telnyx_queue_to_voicemail(g):
    leg = g["telnyx_call_id"]
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.incoming_call_groups SET status='missed'
                WHERE id=%s AND status='queued' RETURNING id""", (g["id"],))
            if not cur.fetchone():
                return
            legs = _ringing_legs(cur, g["id"])
            mark_missed(cur, g["id"])
    for x in legs:
        D["hangup"](x)
    _hold_off(leg)
    D["voicemail_answered"](leg, str(g["id"]))


# ---------------------------------------------------------------- queue worker
def tick():
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,telnyx_call_id,caller_number,provider,line_account,
                    queue_deadline < now() AS late
                FROM public.incoming_call_groups
                WHERE is_shared AND status='queued' ORDER BY created_at LIMIT 50""")
            queued = cur.fetchall()
            # Twilio calls whose caller left without telling us.
            cur.execute("""SELECT id FROM public.incoming_call_groups WHERE is_shared AND provider='twilio'
                AND ((status='queued' AND queue_deadline < now() - interval '90 seconds')
                  OR (status='ringing' AND created_at < now() - interval '5 minutes'))""")
            stale = [r["id"] for r in cur.fetchall()]
            for gid in stale:
                cur.execute("UPDATE public.incoming_call_groups SET status='missed', ended_at=now() WHERE id=%s", (gid,))
                mark_missed(cur, gid)
    for g in queued:
        if g["provider"] != "telnyx":
            continue
        with D["account_ctx"]("telnyx", g["line_account"]):
            if g["late"]:
                _telnyx_queue_to_voicemail(g)
                continue
            with _db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""SELECT 1 FROM public.call_participants WHERE group_id=%s AND status='ringing'
                        AND updated_at > now() - make_interval(secs => %s)""", (g["id"], RING_SECONDS + 10))
                    if cur.fetchone():
                        continue
                    cur.execute("""SELECT u.id,u.email,u.name,u.sip_username,u.dnd_enabled,u.plan_renewal,
                            u.account_id,u.created_at,u.twilio_account,u.telnyx_account
                        FROM public.call_participants p JOIN public.voxsbridge_users u ON u.id=p.user_id
                        WHERE p.group_id=%s AND u.is_active""", (g["id"],))
                    free, _busy = classify(cur, cur.fetchall(), g["id"], "telnyx")
            if free:
                _telnyx_ring(str(g["id"]), g["telnyx_call_id"], g["caller_number"], free, parked=False)


def _worker():
    time.sleep(6)
    while True:
        try:
            tick()
        except Exception:
            log.exception("Ring-group queue tick failed")
        time.sleep(3)
