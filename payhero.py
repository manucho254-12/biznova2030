"""
PayHero API helper for STK Push payments — the only payment provider this app uses.

PayHero (https://payherokenya.com) is a payment aggregator that sits in front of
M-Pesa — it lets you accept STK Push payments into your own Paybill/Till/Bank
account without going through Safaricom's own "Go-Live" approval process
yourself. This is usually the faster path to accepting real payments.

SETUP STEPS (do this before going live):
1. Create a merchant account at https://app.payhero.co.ke (sign up / register).
2. Under Payment Channels, register (or link) the Paybill/Till/Bank account you
   want payments to land in. Note the numeric Channel ID shown next to it
   (Payment Channels -> My Payment Channels).
3. Under API / Developer settings, generate your API Username and API Password —
   these are combined into a Basic Auth header, NOT the same as your portal
   login. Keep them secret.
4. Set CALLBACK_URL to a **public HTTPS URL** that reaches this server's
   /payhero/callback/<secret> route, where <secret> is the value of your
   PAYHERO_CALLBACK_SECRET environment variable, e.g.
   https://yourdomain.com/payhero/callback/your-long-random-secret.
   PayHero cannot reach localhost, so this only works once deployed.
5. Fill in the values below via environment variables (see .env.example).
"""
import base64
import os

import requests


class PayHeroConfig:
    API_USERNAME = os.environ.get('PAYHERO_API_USERNAME', 'YOUR_API_USERNAME')
    API_PASSWORD = os.environ.get('PAYHERO_API_PASSWORD', 'YOUR_API_PASSWORD')
    CHANNEL_ID = os.environ.get('PAYHERO_CHANNEL_ID', 'YOUR_CHANNEL_ID')
    PROVIDER = os.environ.get('PAYHERO_PROVIDER', 'm-pesa')  # 'm-pesa' or 'sasapay'
    CALLBACK_URL = os.environ.get(
        'PAYHERO_CALLBACK_URL', 'https://example.com/payhero/callback/CHANGE_ME')
    BASE_URL = 'https://backend.payhero.co.ke/api/v2'

    @property
    def auth_header(self):
        token = base64.b64encode(f'{self.API_USERNAME}:{self.API_PASSWORD}'.encode()).decode()
        return f'Basic {token}'


class PayHeroError(Exception):
    pass


def format_phone(phone):
    """Convert a Kenyan phone number to the 2547XXXXXXXX format PayHero expects."""
    phone = phone.strip().replace(' ', '').replace('+', '').replace('-', '')
    if phone.startswith('0'):
        phone = '254' + phone[1:]
    elif phone.startswith('7') or phone.startswith('1'):
        phone = '254' + phone
    return phone


def stk_push(config: PayHeroConfig, phone, amount, account_reference, transaction_desc,
             customer_name=None):
    """Trigger an STK push prompt to the user's phone via PayHero.

    Returns a dict shaped like {'CheckoutRequestID': ..., 'reference': ...} — the
    CheckoutRequestID matches the field name app.py already stores on
    ActivationPayment, so the rest of the activation flow doesn't need to change
    just because the provider did.
    """
    url = f'{config.BASE_URL}/payments'
    headers = {'Authorization': config.auth_header, 'Content-Type': 'application/json'}
    payload = {
        'amount': int(amount),
        'phone_number': format_phone(phone),
        'channel_id': config.CHANNEL_ID,
        'provider': config.PROVIDER,
        'external_reference': account_reference[:20],
        'customer_name': customer_name or account_reference,
        'callback_url': config.CALLBACK_URL,
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        data = resp.json()
    except Exception as e:
        raise PayHeroError(f'Could not reach PayHero: {e}')

    if resp.status_code not in (200, 201) or not data.get('success'):
        raise PayHeroError(data.get('error') or data.get('message') or 'STK push failed.')

    # PayHero's success response already includes CheckoutRequestID, so we pass it
    # through as-is alongside their own 'reference' tracking code.
    return {
        'CheckoutRequestID': data.get('CheckoutRequestID'),
        'reference': data.get('reference'),
        'MerchantRequestID': data.get('reference'),  # PayHero has no separate merchant ID;
                                                        # reuse their reference so the existing
                                                        # ActivationPayment.merchant_request_id
                                                        # column still has something useful in it
    }


def transaction_status(config: PayHeroConfig, reference):
    """Asks PayHero directly what happened to a payment (by the 'reference' returned when
    the STK push was sent). Used as a safety net when the callback webhook is delayed or
    never arrives, so a paid user still gets activated. Returns a dict like
    {'state': 'success' | 'failed' | 'pending', 'receipt': <M-Pesa code or None>,
    'description': <text>} — or None if PayHero can't be reached or answers unexpectedly
    (callers treat None as 'still unknown' and just try again later)."""
    if not reference:
        return None
    try:
        resp = requests.get(f'{config.BASE_URL}/transaction-status',
                            params={'reference': reference},
                            headers={'Authorization': config.auth_header}, timeout=10)
        data = resp.json()
    except Exception:
        return None
    if resp.status_code != 200 or not isinstance(data, dict):
        return None
    raw = str(data.get('status') or '').strip().upper()
    if raw in ('SUCCESS', 'SUCCESSFUL', 'COMPLETED'):
        state = 'success'
    elif raw in ('FAILED', 'CANCELLED', 'CANCELED', 'ERROR', 'TIMEOUT'):
        state = 'failed'
    else:
        state = 'pending'
    receipt = data.get('provider_reference') or data.get('MpesaReceiptNumber') or None
    return {'state': state, 'receipt': receipt,
            'description': data.get('error_message') or data.get('message') or raw}
