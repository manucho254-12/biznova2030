# BizNova

A Flask + MySQL earning platform: users pay a one-time activation fee, then earn
from daily photos (like), videos (watch), and trivia questions (answer correctly),
plus a one-level referral bonus paid only when a referred user's activation payment
is confirmed.

## Features

- **Fun Zone (weekend chat + daily prize)** — a "Fun Zone" tab for activated members. It can only
  be opened on **Fridays, Saturdays and Sundays** (East Africa Time). On those days the admin opens the
  session at `/admin/fun-zone` and either writes the topic themselves, or lets the system randomly
  select a member, who is notified on their dashboard and submits the topic (the admin can pick
  someone else or set the topic at any time). Once a topic exists, members chat in one shared room.
  The admin sees every chat, username and member profile (gender, age, student/employee, description),
  can remove messages, and picks the day's winner, who is credited `FUN_ZONE_PRIZE` (KES 100) in
  their wallet exactly once per session. Prizes are included in the admin "total paid out" figures.
- **Profiles** — members can add a profile picture, description, gender, age (years) and whether they
  are a student or an employee at `/profile`. New database columns are added automatically on startup.
- **Light / dark mode** — a toggle in every header; remembers the choice and follows the device
  setting by default. Theme colours are CSS variables at the top of `static/css/style.css`.

- **Photos / Videos / Trivia** — daily micro-tasks (24-hour expiry), as before.
- **Writing Tasks** — admin posts a paid writing task (title, instructions, minimum word
  count, USD price, application window, and a time limit once assigned). Users apply;
  admin picks who gets it from the applicant list. The assigned user has until the
  deadline to submit; admin reviews and approves/rejects. Approved submissions are
  credited to the user's KES wallet using the `USD_TO_KES_RATE` set in `app.py`.
- **Product Ads** — admin uploads a product image or video directly from their device
  (or links an externally-hosted video instead), with an optional destination link and an
  optional KES reward for viewing it. Users see it under "Product Ads" — if a reward is
  set, they see an "Earn KES X" tag and a claim button (once per user per ad); if a link
  is set, tapping "Visit Site" takes them there. Every new ad triggers a one-time popup
  notification the next time each user logs in, and mentions the reward amount if one's set.
- **Videos (daily earning task)** — admin can upload a video file directly from their
  device, or paste a URL instead (either works, upload takes priority if both are given).
  Each video also has a configurable required watch time (default 5 minutes, or the full
  video if it's shorter) — the reward button stays locked until the user has genuinely
  watched that much, tracked via real playback progress rather than a fixed timer.
- **Email notifications** — users get an email when: they earn a referral bonus, they
  submit a withdrawal request, and when that withdrawal is approved (sent) or rejected
  (refunded). Uses the same SMTP setup as login verification — see the email setup section
  above. If sending fails, the underlying action (referral credit, withdrawal, etc.) still
  goes through — emails are best-effort and never block anything.
- **Withdrawal transaction fees** — admin sets fee tiers by amount range at
  `/admin/withdrawal-fees` (e.g. "KES 200–350 costs KES 15"). The fee is deducted from what
  the user receives, not added on top — their wallet is debited the full requested amount,
  and the fee revenue is tracked separately on the admin dashboard under "Transaction Costs
  Collected". Users see a live fee preview before submitting a withdrawal.
- **Contact Admin (support)** — logged-in users can send a message to admin at `/support`
  (subject + message), and see their own message history and status. Admin reviews all
  messages at `/admin/support-messages`, marks them New/Read/Replied, and can reply
  directly by email with one click. If admin has set their email in Settings, they also
  get notified automatically when a new message comes in.
- **Sell Products (marketplace)** — admin lists items for sale on behalf of sellers: name,
  image, cost, description, owner name/contact, and location — every field is optional, so
  a listing can be posted with just one or two details and filled in later. Users browse
  active listings at `/sell-products` and reach out to the seller directly using the
  contact info shown.
- **Advertise with BizNova** — a public `/advertise` page where companies can submit a
  request to advertise (company name, contact email/phone, product name, message). Requests
  are saved and reviewable at `/admin/advertise-requests`, where you can mark each as
  New/Contacted/Closed as you follow up by email. Update `ADVERTISE_CONTACT_EMAIL` near the
  top of `app.py` to your real inbox — it's shown on the page as a direct-email fallback.
- **Referral Leaderboard** — ranks users by activated referrals (This Week / This Month /
  All-Time) at `/leaderboard` (privacy-friendly, first name + last initial only). Admin sees
  full names at `/admin/leaderboard` with a bar chart. The #1 referrer of the week gets a
  fixed KES 70 bonus, and #1 of the month gets KES 150 (both set via `WEEKLY_LEADERBOARD_BONUS`
  / `MONTHLY_LEADERBOARD_BONUS` in `app.py`) — admin still clicks to pay it manually, but the
  amount is pre-filled and the page shows whether it's already been paid for that period so
  nothing gets paid twice by accident.
- **Self-service password reset** — a "Forgot your password?" link on the login page lets
  users reset their own password by email — no admin involved. They enter their email,
  get a one-time link (expires in 30 minutes, single-use), and set a new password
  themselves. The confirmation message is identical whether or not the email is
  registered, so this can't be used to check who has an account.
- **Admin login protection** — admin can optionally set an email address at
  `/admin/settings`, which then becomes a required second check (on top of username/
  password) at every login. Leave the email blank to skip that extra check.
- **Admin settings** — admin can change their own login password at `/admin/settings`.
  User passwords are never visible to anyone (they're one-way hashed) — if a user is locked
  out, use "Reset Password" next to their name on the Users page to generate a new temporary
  password to share with them directly.
- **Surveys** — admin creates a survey with a KES reward and a list of open-ended
  questions. Users answer and are credited instantly on submission (no approval step).

## How the economics work (read this first)

- Every payout is funded by a real transaction: the **activation fee (KES 100)**.
- Activation is now **fully automatic**: the user enters their phone number, an M-Pesa
  STK prompt is pushed to their phone, and the moment they enter their PIN, Safaricom
  calls our server back and the account activates itself — no admin step needed.
- **Referral bonus (KES 100)** is paid to the referrer automatically, the instant the
  *referred user's own* activation payment is confirmed by M-Pesa. It is one level only
  — you are never paid for people your referrals bring in. This is what keeps it a
  normal referral program rather than a recruitment chain.
- **Task rewards** (photos/videos/trivia) come straight out of your own funds/collected
  fees, so keep an eye on the admin dashboard's "Financial Health" section — it shows
  fees collected vs. total paid out so you always know whether the numbers are healthy
  before approving withdrawals.
- All amounts (`ACTIVATION_FEE`, `REFERRAL_BONUS`, `PHOTO_LIKE_REWARD`,
  `VIDEO_WATCH_REWARD`, `TRIVIA_CORRECT_REWARD`, `MIN_WITHDRAWAL`) are defined near the
  top of `app.py` — tune them so fees collected comfortably cover what you expect to
  pay out.

## Setting up PayHero — payment provider for activations and withdrawals

Activation payments go through [PayHero](https://app.payhero.co.ke), an aggregator
that lets you accept STK Push payments into your own Paybill/Till/Bank account
without applying for Safaricom's own "Go-Live" approval yourself — the faster way
to start accepting real money.

1. Sign up for a merchant account at https://app.payhero.co.ke.
2. Under **Payment Channels**, register or link the Paybill/Till/Bank account you
   want payments to land in, and note its numeric **Channel ID**.
3. Under the API/Developer section, generate an **API Username** and **API Password**
   (these are separate from your portal login — they're combined into a Basic Auth
   header for the API).
4. Set these environment variables:
   - `PAYHERO_API_USERNAME`
   - `PAYHERO_API_PASSWORD`
   - `PAYHERO_CHANNEL_ID`
   - `PAYHERO_PROVIDER` — `m-pesa` (or `sasapay` if that's your linked wallet)
   - `PAYHERO_CALLBACK_URL` — a public HTTPS URL pointing at
     `/payhero/callback/<the same secret as PAYHERO_CALLBACK_SECRET>` on your deployed
     site, e.g. `https://yourdomain.com/payhero/callback/your-long-random-secret`.
     **This won't work on localhost** — PayHero's servers need to reach it, so it only
     works once deployed.
   - `PAYHERO_CALLBACK_SECRET` — a long random string (generate one with
     `python -c "import secrets; print(secrets.token_urlsafe(24))"`). This protects the
     callback endpoint — without it, anyone could POST a fake "payment confirmed"
     webhook and get free activations.
5. Top up your PayHero service wallet — they charge a small float fee per STK push
   they initiate on your behalf.

### If the STK push fails or PayHero's callback doesn't arrive

This happens sometimes (network blips, callback URL not reachable yet, etc.). The
activation page has a **"Prefer to pay manually instead?"** fallback: the user pays to
your paybill directly and submits their M-Pesa code, and you confirm it from
`/admin/payments` — same as before. Nothing is lost if the automatic path hiccups.

## Setting up email (Brevo) — login codes, notifications, and password resets

The app sends email for several things, all through the same SMTP setup:
- Login verification codes (OTP)
- Welcome email when someone registers
- A notification to your admin email when a new user joins
- Referral bonus earned (tells the referrer who joined, by username)
- Withdrawal requested / approved / rejected
- Self-service password reset links

**Setup (Brevo, recommended — free tier includes 300 emails/day):**
1. Create a free account at [brevo.com](https://www.brevo.com).
2. Go to **Settings** (top right) → **SMTP & API** → **SMTP** tab.
3. Note your **SMTP login** (usually your Brevo account email), and click **Generate a new
   SMTP key** — copy that key.
4. Under **Senders & IP → Senders**, verify the email address you want to send *from*
   (Brevo requires this on the free tier).
5. Set these as environment variables (same pattern as the M-Pesa credentials — via your
   WSGI file on PythonAnywhere, or your shell locally):
   - `SMTP_HOST` — `smtp-relay.brevo.com`
   - `SMTP_PORT` — `587`
   - `SMTP_USER` — your Brevo SMTP login
   - `SMTP_PASSWORD` — the SMTP key you generated
   - `SMTP_FROM` — the sender address you verified in step 4

Any other SMTP provider also works the same way (Gmail with an App Password, Outlook,
Zoho, SendGrid, Mailgun, etc.) — just point the host/port/user/password at theirs.

**To receive the "new user joined" notification yourself:** set your email at
`/admin/settings` (the same field used for admin login protection).

**On PythonAnywhere:** free accounts restrict outbound access to an allowlist, same issue
as with the M-Pesa API — Brevo's SMTP server usually isn't on it by default. Request it be
added, upgrade to a paid plan, or use Brevo's HTTP API instead if this becomes a blocker.

**If email sending fails or isn't configured yet:** the underlying action (login,
referral credit, withdrawal, registration) still goes through — emails are always
best-effort and never block anything. For login specifically, users are logged in
directly with a warning shown telling them to contact the admin, rather than being locked
out entirely. Set `ENABLE_LOGIN_OTP = False` in `app.py` to turn off the login-code step
specifically, while keeping all other email notifications active.

## Local setup

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

The app uses **MySQL by default**. Before running it, create an empty `biznova` database
once (e.g. via phpMyAdmin if using XAMPP — Start MySQL in the XAMPP Control Panel, click
**Admin** to open phpMyAdmin, **New** → name it `biznova`, collation `utf8mb4_general_ci`
→ **Create**), and make sure `DATABASE_URL` in your `.env` points at it (see
`.env.example` — the default already matches a typical local XAMPP/MySQL setup on
`root` with no password):

```
DATABASE_URL=mysql+pymysql://root:@localhost:3306/biznova?charset=utf8mb4
```

Then run:

```bash
python app.py
```

The app creates every table inside that empty database automatically the first time it
runs, along with a default admin account:

- **Admin URL:** `/admin/login`
- **Username:** `admin`
- **Password:** `admin123` — change this at `/admin/settings` once logged in.
- **Email:** leave blank the first time — not required until you set one in Settings.

Visit `http://127.0.0.1:5000` for the main site.

## Deploying to PythonAnywhere

1. Upload this folder (or the zip) and extract it.
2. Create a virtualenv and `pip install -r requirements.txt` inside it.
3. Create a MySQL database for the app (PythonAnywhere's free/paid MySQL, or your own
   host) — see the Database section above — and set `DATABASE_URL` accordingly.
4. In your Web app's **WSGI configuration file**, set your PayHero and Brevo environment
   variables before importing the app (see the sections above for the full pattern):
   ```python
   import os
   os.environ['DATABASE_URL'] = 'mysql+pymysql://...'
   os.environ['PAYHERO_API_USERNAME'] = '...'
   # ...etc — see the Database, PayHero, and Brevo setup sections above

   from app import app as application
   ```
5. Set the working directory / source code path to this folder so
   `static/uploads/photos` / `static/uploads/ads` are writable.
6. Reload the web app.

## Automating withdrawals too (optional, future upgrade)

Withdrawals are still manual (admin approves, then sends M-Pesa payment by hand) —
that's intentional so you stay in control of cash going out while you're self-funding.
Once you're comfortable with the volume, PayHero also offers payout/send-money
endpoints that could automate this — check their current docs at
https://docs.payhero.co.ke when you're ready. Happy to help wire that in too.

## Notes

- Photos, videos, and trivia questions automatically "expire" 24 hours after being
  posted (they stop appearing to users and can no longer be liked/watched/answered
  for a reward) — matching the "5 a day" workflow you described.
- Video URLs need to be direct, playable links (e.g. `.mp4` files you host somewhere)
  since the built-in HTML5 player can't play YouTube page links directly.
- Update the paybill/till number placeholder in `templates/activate.html` with your
  real one before launch.
