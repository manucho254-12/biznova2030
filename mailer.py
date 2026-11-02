"""
SMTP email helper — sends login OTPs, referral/withdrawal notifications, and password
reset links. Configured for Brevo (formerly Sendinblue) by default.

SETUP STEPS (Brevo):
1. Create a free account at https://www.brevo.com (free tier includes 300 emails/day).
2. Go to Settings (top right) -> SMTP & API -> SMTP tab.
3. You'll see your SMTP login (usually your Brevo account email) and can generate an
   "SMTP key" (a long password-like string) — click "Generate a new SMTP key".
4. Set these as environment variables (see README) rather than editing this file directly:
     SMTP_HOST = smtp-relay.brevo.com
     SMTP_PORT = 587
     SMTP_USER = the SMTP login shown on that page (your Brevo account email)
     SMTP_PASSWORD = the SMTP key you generated
     SMTP_FROM = the "from" email address you want recipients to see — for Brevo's free
                 tier this should be an email address you've verified as a sender under
                 Senders & IP -> Senders in your Brevo dashboard.
5. Any other SMTP provider also works the same way (Gmail with an App Password, Outlook,
   Zoho, SendGrid, Mailgun, etc.) — just point the host/port/user/password at theirs.

NOTE FOR PYTHONANYWHERE: free accounts restrict outbound network access to an allowlist,
same as with the M-Pesa API. Brevo's SMTP server (smtp-relay.brevo.com) is usually NOT on
that allowlist by default — request it be added (Account -> "Request another site be
whitelisted"), or upgrade to a paid plan, or use Brevo's HTTP API instead if this becomes
a blocker.
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


class MailConfig:
    SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp-relay.brevo.com')
    SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
    SMTP_USER = os.environ.get('SMTP_USER', 'YOUR_BREVO_LOGIN_EMAIL')
    SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', 'YOUR_BREVO_SMTP_KEY')
    SMTP_FROM = os.environ.get('SMTP_FROM', os.environ.get('SMTP_USER', 'YOUR_VERIFIED_SENDER_EMAIL'))
    FROM_NAME = os.environ.get('SMTP_FROM_NAME', 'BizNova')


class MailError(Exception):
    pass


def send_email(config: MailConfig, to_address, subject, body_text):
    """Sends a plain-text email. Raises MailError on any failure so callers can decide
    how to handle it (e.g. fall back to logging the user in without the code)."""
    msg = MIMEMultipart()
    msg['From'] = f'{config.FROM_NAME} <{config.SMTP_FROM}>'
    msg['To'] = to_address
    msg['Subject'] = subject
    msg.attach(MIMEText(body_text, 'plain'))

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.SMTP_FROM, [to_address], msg.as_string())
    except Exception as e:
        raise MailError(f'Could not send email: {e}')
