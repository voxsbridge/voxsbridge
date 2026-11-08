# VoxsBridge — Global Calling Solutions

Browser dialer + CRM (FastAPI + Jinja). Built from the Dialnytics code, with a new name, logo, colors and layout.

## Is update mein kya naya hai (Dialnytics ke features)

VoxsBridge ka design, naam, logo, colors, sidebar aur admin console pehle jaise hain. Neeche wale features Dialnytics se add hue hain:

**Calling**
- **Call transfer:** teammate ko ya kisi phone number par transfer. Transfer incoming calls par hota hai. Agar teammate busy ho to caller hold music sunta hai (max 3 min), aur jawab na mile to call wapas pehle agent ke paas aa jati hai.
- **Add Call (4 lines tak), Swap, Merge / conference.**
- **Shared number (ring group):** ek number ek hi company ke kai users ko diya ja sakta hai. Jo free hoga uska dialer bajega, aur jo pehle uthayega call usi ko milegi.
- **Calling countries per user:** UK number sirf UK, US number sirf US/Canada call kar sakta hai. Admin Users → Edit mein mazeed mulk allow kar sakta hai. Emergency/short numbers (999, 112, 911…) hamesha block hain.
- **Floating phone:** chhoti always-on-top window (Chrome/Edge).
- **Twilio par Hold + Transfer.**
- **2nd Telnyx account (TELNYX2_\*) aur 2nd Twilio account (TWILIO2_\*).**
- Call end hone par provider ka SIP code bhi dikhta hai (diagnosis ke liye).

**Admin**
- **Admin call recordings (fraud monitoring):** har call admin ke liye record hoti hai. Customer Details → 🎙 Recordings. 14 din baad khud delete hoti hai. Ye baat apne Terms & Conditions mein zaroor likhein (UK aur kai mulkon mein qanooni zaroorat hai).
- **Dashboard:** Lifetime earnings (PKR + USD), cost / revenue / margin (Today, 7 din, 30 din, Lifetime), aur har calling account ka alag cost.
- **Cost by country:** rates provider, direction aur zone ke hisaab se (US/Canada, UK landline, UK mobile, other). Settings → Provider Rates.
- **Global search:** header search se users, numbers, contacts aur call logs dhundein.
- **Create User page, Disabled users** (enable ya permanently delete), aur payment history mein edit/delete.
- **Website orders** (optional, sirf MARKETING_API_KEY set ho to): Pending Orders, Website Orders, number hold, aur self-signup accounts.
- **Add-on switches per user:** Call recording, Power Dialer/CRM/Campaigns/Callbacks, SMS allowance (credits).
- **Invoices:** payment method (Bank / Card / USDT / Other), PAID stamp, aur VoxsBridge letterhead wala PDF.

**CRM / Power Dialer / SMS**
- CRM import: CSV ya Excel, columns naam se match hote hain (Name, Company, Website, Phone, Notes). Bulk select / delete.
- Power Dialer: lead picker mein delete, aur outcome foran save hota hai.
- SMS inbox: conversations, search, delivered status, MMS links, unread badge, aur shared number ka ek inbox.

**Jo cheezein jaan boojh kar NAHI dali gayin:** Advanced calling, Leave call, Live call listening/whisper, aur glassy look.

## Purane VoxsBridge se upgrade

1. Pehle Supabase ka **backup** lein.
2. Files GitHub par replace karein aur Render par deploy karein. Database ke naye tables/columns app start hote hi khud ban jate hain.
3. **SMS data khud move hota hai (ek dafa):** purana `sms_limit − sms_used` naye `sms_credits` mein chala jata hai, aur `voxsbridge_sms` ki history naye inbox (`sms_messages`) mein copy hoti hai. Purana table delete nahi hota.
4. Purana SMS webhook URL `/webhooks/telnyx-sms` chalta rahega. Naya URL `/webhooks/telnyx/sms` hai.
5. Deploy ke baad sab users ek dafa logout/login karein, aur browser mein hard refresh (Ctrl+Shift+R) karein.

## Naye setup ke steps

1. **Supabase:** naya project banayen, SQL Editor mein `SUPABASE_SETUP_VOXSBRIDGE.sql` run karein.
2. **Render:** Web Service.
   - Build: `pip install -r requirements.txt`
   - Start: `cd backend && uvicorn main:app --host 0.0.0.0 --port $PORT`
3. **Environment variables:**

| Key | Value |
|---|---|
| `DATABASE_URL` | Supabase connection string (Transaction pooler) |
| `ADMIN_PASSWORD`, `AGENT_PASSWORD` | Default logins ke passwords |
| `PUBLIC_BASE_URL` | Jaise `https://app.voxsbridge.com` |
| `TRUSTED_HOSTS` | Wahi domain (bina https ke) |
| `SUPPORT_WHATSAPP` | WhatsApp number, sirf digits |
| `TELNYX_API_KEY`, `TELNYX_CONNECTION_ID`, `TELNYX_FROM_NUMBER`, `TELNYX_VOICE_APP_ID`, `TELNYX_PUBLIC_KEY` | Telnyx 1 |
| `TELNYX_SMS_PROFILE_ID` | Telnyx Messaging profile (SMS ke liye) |
| `TELNYX2_*` (optional) | Doosra Telnyx account: `TELNYX2_API_KEY`, `TELNYX2_PUBLIC_KEY`, `TELNYX2_CONNECTION_ID`, `TELNYX2_VOICE_APP_ID`, `TELNYX2_SMS_PROFILE_ID` |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` (optional) | Twilio 1 |
| `TWILIO2_ACCOUNT_SID`, `TWILIO2_AUTH_TOKEN` (optional) | Twilio 2 |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_RECORDINGS_BUCKET` (optional) | Recordings ke liye private bucket |
| `INVOICE_BANK_NAME`, `INVOICE_BANK_ACCOUNT_NAME`, `INVOICE_BANK_ACCOUNT_NUMBER` | Invoice par bank details. Khali chhoren to invoice par "WhatsApp par details" likha aata hai |
| `INVOICE_PHONE` (optional) | Invoice par phone (default SUPPORT_WHATSAPP) |
| `MARKETING_API_KEY`, `MARKETING_URL` (optional) | Sirf agar website se orders aate hon |
| `MONITOR_RECORDING_DAYS` (optional) | Admin recordings kitne din rakhni hain (default 14) |
| `TRANSFER_WAIT_SECONDS`, `TRANSFER_RING_SECONDS`, `HOLD_MUSIC_URL`, `RING_GROUP_RING_SECONDS`, `RING_GROUP_QUEUE_SECONDS`, `SMS_DAILY_LIMIT` (optional) | Defaults theek hain |

4. **Telnyx webhooks:**
   - Voice API Application: `https://<domain>/webhooks/telnyx`. **Transfer, hold music aur shared number ke liye ye zaroori hai.**
   - Messaging profile: `https://<domain>/webhooks/telnyx/sms`
   - Outbound Voice Profile mein sirf woh mulk allow karein jin ko aap bechte hain, aur spending limit lagayein.

## Default logins
- Admin: `admin@voxsbridge.com` / `ADMIN_PASSWORD`
- Demo agent: `agent@voxsbridge.com` / `AGENT_PASSWORD`

Logos `frontend/static/img/` mein hain. Invoice ka letterhead `letterhead.png` hai.
