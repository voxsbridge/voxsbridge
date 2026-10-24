-- =====================================================================
-- VoxsBridge — full database setup for a NEW Supabase project
-- Supabase > SQL Editor > New query > paste all of this > Run.
-- Safe to run more than once (everything is IF NOT EXISTS).
-- =====================================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------- Users ----------
CREATE TABLE IF NOT EXISTS public.voxsbridge_users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL DEFAULT '',
    email text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role text NOT NULL DEFAULT 'agent',
    phone_number text,
    credential_id text,
    sip_username text,
    plan_name text,
    plan_start date,
    plan_renewal date,
    last_paid_amount numeric(14,2) NOT NULL DEFAULT 0,
    recording_premium boolean NOT NULL DEFAULT false,
    dnd_enabled boolean NOT NULL DEFAULT false,
    is_active boolean NOT NULL DEFAULT true,
    is_demo boolean NOT NULL DEFAULT false,
    trial_started_at timestamptz,
    trial_expires_at timestamptz,
    provider text NOT NULL DEFAULT 'telnyx',
    contact_name text NOT NULL DEFAULT '',
    contact_phone text NOT NULL DEFAULT '',
    contact_address text NOT NULL DEFAULT '',
    customer_notes text NOT NULL DEFAULT '',
    user_type text NOT NULL DEFAULT 'customer',
    account_role text NOT NULL DEFAULT 'agent',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_voxsbridge_demo_expiry
    ON public.voxsbridge_users(trial_expires_at) WHERE is_demo = TRUE;

-- ---------- Companies (accounts) ----------
CREATE TABLE IF NOT EXISTS public.voxsbridge_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    reseller_id uuid REFERENCES public.voxsbridge_users(id) ON DELETE SET NULL,
    notes text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.voxsbridge_users ADD COLUMN IF NOT EXISTS account_id uuid
    REFERENCES public.voxsbridge_accounts(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS voxsbridge_users_account_idx ON public.voxsbridge_users(account_id);
CREATE INDEX IF NOT EXISTS voxsbridge_accounts_reseller_idx ON public.voxsbridge_accounts(reseller_id);

-- ---------- Sessions ----------
CREATE TABLE IF NOT EXISTS public.user_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_sessions_user_idx ON public.user_sessions(user_id);

-- ---------- Phone numbers assigned to users ----------
CREATE TABLE IF NOT EXISTS public.caller_id_assignments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    phone_number text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, phone_number)
);
CREATE INDEX IF NOT EXISTS caller_id_assignments_number_idx ON public.caller_id_assignments(phone_number);

-- ---------- Call logs ----------
CREATE TABLE IF NOT EXISTS public.calls (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    from_number text,
    to_number text,
    direction text NOT NULL DEFAULT 'outgoing',
    status text NOT NULL DEFAULT 'initiated',
    duration_seconds integer NOT NULL DEFAULT 0,
    provider text NOT NULL DEFAULT 'telnyx',
    provider_call_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS calls_user_created_idx ON public.calls(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS calls_created_at_idx ON public.calls(created_at);

-- ---------- Incoming call ringing groups ----------
CREATE TABLE IF NOT EXISTS public.incoming_call_groups (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    telnyx_call_id text UNIQUE,
    caller_number text,
    destination_number text,
    status text NOT NULL DEFAULT 'ringing',
    answered_by uuid REFERENCES public.voxsbridge_users(id) ON DELETE SET NULL,
    call_log_id uuid REFERENCES public.calls(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz
);
CREATE INDEX IF NOT EXISTS incoming_call_groups_call_log_idx ON public.incoming_call_groups(call_log_id);
CREATE TABLE IF NOT EXISTS public.call_participants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id uuid NOT NULL REFERENCES public.incoming_call_groups(id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'ringing',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (group_id, user_id)
);

-- ---------- Payments ----------
CREATE TABLE IF NOT EXISTS public.payments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    amount_pkr numeric(14,2) NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'paid',
    paid_at timestamptz,
    renewal_date date,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payments_user_idx ON public.payments(user_id);

-- ---------- Contacts ----------
CREATE TABLE IF NOT EXISTS public.voxsbridge_contacts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    name text NOT NULL,
    number text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS voxsbridge_contacts_user_idx ON public.voxsbridge_contacts(user_id);

-- ---------- CRM + Power Dialer ----------
CREATE TABLE IF NOT EXISTS public.crm_leads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    name text NOT NULL DEFAULT '',
    phone text NOT NULL,
    email text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'new',
    notes text NOT NULL DEFAULT '',
    followup_at timestamptz,
    consent boolean NOT NULL DEFAULT false,
    do_not_call boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, phone)
);
CREATE TABLE IF NOT EXISTS public.power_queues (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    mode text NOT NULL DEFAULT 'manual',
    delay_seconds integer NOT NULL DEFAULT 5,
    status text NOT NULL DEFAULT 'paused',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.power_queue_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_id uuid NOT NULL REFERENCES public.power_queues(id) ON DELETE CASCADE,
    lead_id uuid NOT NULL REFERENCES public.crm_leads(id) ON DELETE RESTRICT,
    position integer NOT NULL DEFAULT 0,
    result text,
    finished_at timestamptz,
    call_notes text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS power_queue_items_queue_idx ON public.power_queue_items(queue_id, position);

-- ---------- Call scripts ----------
CREATE TABLE IF NOT EXISTS public.scripts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    title text NOT NULL DEFAULT '',
    category text NOT NULL DEFAULT '',
    content text NOT NULL DEFAULT '',
    is_default boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---------- Invoices ----------
CREATE SEQUENCE IF NOT EXISTS public.voxsbridge_invoice_seq;
CREATE TABLE IF NOT EXISTS public.voxsbridge_invoices (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), invoice_number text NOT NULL UNIQUE,
    customer_id uuid NOT NULL REFERENCES public.voxsbridge_users(id),
    customer_name text NOT NULL, customer_email text NOT NULL, contact_phone text NOT NULL DEFAULT '',
    contact_address text NOT NULL DEFAULT '', issue_date date NOT NULL, due_date date NOT NULL,
    notes text NOT NULL DEFAULT '', terms text NOT NULL DEFAULT '',
    subtotal_pkr numeric(14,2) NOT NULL DEFAULT 0, discount_pkr numeric(14,2) NOT NULL DEFAULT 0,
    tax_percent numeric(5,2) NOT NULL DEFAULT 0, tax_pkr numeric(14,2) NOT NULL DEFAULT 0,
    total_pkr numeric(14,2) NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','sent','paid','void')),
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.voxsbridge_invoice_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id uuid NOT NULL REFERENCES public.voxsbridge_invoices(id) ON DELETE CASCADE,
    position int NOT NULL, description text NOT NULL, qty int NOT NULL CHECK (qty > 0),
    unit_price_pkr numeric(14,2) NOT NULL CHECK (unit_price_pkr >= 0),
    amount_pkr numeric(14,2) NOT NULL CHECK (amount_pkr >= 0)
);
CREATE INDEX IF NOT EXISTS voxsbridge_invoice_customer_idx ON public.voxsbridge_invoices(customer_id);

-- ---------- Settings, recordings, presence ----------
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
CREATE TABLE IF NOT EXISTS public.user_presence (
    user_id uuid PRIMARY KEY REFERENCES public.voxsbridge_users(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'online',
    call_number text NOT NULL DEFAULT '',
    call_direction text NOT NULL DEFAULT '',
    call_started_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---------- Lock everything down (server uses DATABASE_URL, browser never talks to DB) ----------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['voxsbridge_users','voxsbridge_accounts','user_sessions','caller_id_assignments',
    'calls','incoming_call_groups','call_participants','payments','voxsbridge_contacts','crm_leads',
    'power_queues','power_queue_items','scripts','voxsbridge_invoices','voxsbridge_invoice_items',
    'voxsbridge_settings','voxsbridge_recordings','user_presence']
  LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('REVOKE ALL ON public.%I FROM anon, authenticated', t);
  END LOOP;
END $$;
