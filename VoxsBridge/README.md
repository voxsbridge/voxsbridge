# VoxsBridge — Global Calling Solutions

Browser dialer + CRM (FastAPI + Jinja). Built from the Dialnytics code, with a new name, logo, colors and layout.

## Setup (step by step)

1. **Supabase:** Naya project banayen. **SQL Editor** kholen, `SUPABASE_SETUP_VOXSBRIDGE.sql` paste karen aur **Run** dabayen.
2. **GitHub:** Naya repo banayen (jaise `VoxsBridge`) aur ye saari files upload karen.
3. **Render:** New **Web Service** banayen.
   - Build command: `pip install -r requirements.txt`
   - Start command: `cd backend && uvicorn main:app --host 0.0.0.0 --port $PORT`
4. **Environment variables (Render):**

| Key | Value |
|---|---|
| `DATABASE_URL` | Naye Supabase project ka connection string (Transaction pooler) |
| `ADMIN_PASSWORD` | Admin login password |
| `AGENT_PASSWORD` | Demo agent password |
| `PUBLIC_BASE_URL` | Apka dialer domain, jaise `https://app.voxsbridge.com` |
| `TRUSTED_HOSTS` | Wahi domain (bina https ke) |
| `SUPPORT_WHATSAPP` | WhatsApp support number, sirf digits (default `447476629105`) |
| `TELNYX_API_KEY`, `TELNYX_CONNECTION_ID`, `TELNYX_FROM_NUMBER`, `TELNYX_VOICE_APP_ID`, `TELNYX_PUBLIC_KEY` | Telnyx se |
| `TWILIO_*` (optional) | Agar Twilio use karna ho |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_RECORDINGS_BUCKET` (optional) | Recordings ke liye. Bucket ka default naam `voxsbridge-recordings` hai |

5. Telnyx webhook URLs ko naye domain pe set karen (Dialnytics wale paths hi hain, sirf domain naya hai).

## Default logins
- Admin: `admin@voxsbridge.com` / `ADMIN_PASSWORD`
- Demo agent: `agent@voxsbridge.com` / `AGENT_PASSWORD`

## What changed from Dialnytics
- **Branding:** naam, logo (dark aur light versions), favicon aur header logo naye hain. Theme cyan aur royal blue hai, font Plus Jakarta Sans hai.
- **Sidebar:** sections mein grouped hai aur line icons hain. Naye labels: Call History, Callbacks, Call Scripts, CRM Pipeline aur Invite & Earn.
- **Dashboard:** greeting, quick actions aur stat cards pe colored top line hai.
- **Login:** split screen hai.
- **Admin:** sirf management dikhata hai.
  - Menu mein Dashboard, Customers, Companies, User Accounts, Resellers, Invoices aur Settings hain.
  - Dialer, Call History, Contacts, CRM, Power Dialer, Billing, Messages, Voicemail, Recordings, Campaigns, Callbacks aur Call Scripts admin ko nahi dikhte. Direct URL kholne pe admin dashboard pe wapas chala jata hai.
  - Admin dashboard pe Customers, Active Plans, Companies, Phone Numbers, Platform Calls, Earnings, Calling Cost aur Repayments hain.
- **Database:** tables ke naam `voxsbridge_*` hain. Ye alag Supabase project ke liye hai, is liye Dialnytics ka data is mein nahi aata.

Logos `frontend/static/img/` mein hain. Inhein badalne ke liye files replace karen, naam same rakhen.
