import re
import os
import secrets
import string
import math
import time
import io
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from functools import wraps


try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

import payhero
import mailer

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def now_utc():
    """Returns the current time in UTC. Every timestamp in this app — stored in the
    database, shown on the dashboard, printed on PDF reports, used for OTP/task/
    rate-limit expiry — goes through this single function rather than calling
    datetime.utcnow() directly in different places, so all of it stays consistent."""
    return datetime.utcnow()


UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'photos')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# LOGGING — server-side only. Things like "email failed to send" should never
# be shown to a visitor (it can leak SMTP config, or just look unprofessional),
# but the admin still needs to know it happened, so it goes here instead:
# logs/biznova.log (rotates at 1MB, keeps 5 old files) plus the console.
# ---------------------------------------------------------------------------
logger = logging.getLogger('biznova')
logger.setLevel(logging.INFO)
_log_formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
_file_handler = RotatingFileHandler(os.path.join(LOG_DIR, 'biznova.log'), maxBytes=1_000_000, backupCount=5)
_file_handler.setFormatter(_log_formatter)
logger.addHandler(_file_handler)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)
logger.addHandler(_console_handler)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'biznova-change-this-secret-key-in-production')

# ---------------------------------------------------------------------------
# DATABASE CONFIG — MySQL by default. Set these via environment variables
# (or a local .env file — see .env.example) to point at your MySQL server:
#   DATABASE_URL=mysql+pymysql://root:@localhost:3306/biznova?charset=utf8mb4
# The database itself (e.g. "biznova") must already exist on the MySQL
# server — this app creates the tables inside it, but not the database.
#   mysql -u root -e "CREATE DATABASE biznova CHARACTER SET utf8mb4;"
# ---------------------------------------------------------------------------
default_mysql_uri = 'mysql+pymysql://root:@localhost:3306/biznova?charset=utf8mb4'
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', default_mysql_uri)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8MB uploads

db = SQLAlchemy(app)
payhero_config = payhero.PayHeroConfig()
mail_config = mailer.MailConfig()

# The PayHero callback URL includes this secret as part of its path (see
# payhero_callback() below) so that only genuine requests carrying it are processed —
# otherwise anyone who discovers the callback endpoint could POST a fake "payment
# confirmed" webhook and get free account activations (plus trigger real referral
# payouts). Set this permanently via the PAYHERO_CALLBACK_SECRET env var and include
# it in the PAYHERO_CALLBACK_URL you register with PayHero, e.g.
# https://yourdomain.com/payhero/callback/<this-secret>.
PAYHERO_CALLBACK_SECRET = os.environ.get('PAYHERO_CALLBACK_SECRET')
if not PAYHERO_CALLBACK_SECRET:
    PAYHERO_CALLBACK_SECRET = secrets.token_urlsafe(24)
    print(f'WARNING: PAYHERO_CALLBACK_SECRET is not set. Generated a temporary one for this run: '
          f'{PAYHERO_CALLBACK_SECRET}\n'
          f'Set PAYHERO_CALLBACK_SECRET permanently in your environment and update the callback '
          f'URL registered with PayHero to match — otherwise this value changes on every '
          f'restart and payments will stop confirming automatically.')

# ---------------------------------------------------------------------------
# SETTINGS (change these to configure the business economics of the site)
# ---------------------------------------------------------------------------
ACTIVATION_FEE = 100           # KES paid once by every user to activate their account.
                               # Collected automatically via PayHero STK Push (see payhero.py) —
                               # the account activates itself the moment PayHero confirms
                               # payment via the /payhero/callback webhook, no admin step needed.
REFERRAL_BONUS = 50           # KES paid to the direct (level 1) referrer automatically, the
                               # moment the referred user's own activation payment is confirmed.
LEVEL2_REFERRAL_BONUS = 10    # KES paid to the level 2 referrer — i.e. whoever referred the
                               # level 1 referrer — on that same activation. Both bonuses are
                               # funded out of the new user's own KES 100 activation fee.
PHOTO_LIKE_REWARD = 5          # KES per photo like (once per user per photo, 24hr window)
VIDEO_WATCH_REWARD = 5         # KES per video watched (once per user per video)
TRIVIA_CORRECT_REWARD = 3      # KES per correctly answered trivia question
MIN_WITHDRAWAL = 200           # KES minimum withdrawal amount
TASK_UNLOCK_FEE = 20          # KES paid via PayHero STK Push to claim a writing task — the
                               # first user whose payment is confirmed gets it assigned to them
                               # automatically; this is separate from the price_usd reward paid
                               # out later once their submission is approved.

WEEKLY_LEADERBOARD_BONUS = 20   # KES extra for the #1 referrer of the week (admin credits manually)
MONTHLY_LEADERBOARD_BONUS = 100  # KES extra for the #1 referrer of the month (admin credits manually)

USD_TO_KES_RATE = 130          # Used to convert content writing task prices (set in USD)
                               # into KES when crediting the wallet. Update this to the
                               # current exchange rate whenever you like.
DEFAULT_APPLICATION_WINDOW_HOURS = 2   # How long a writing task stays open for applications by default.

GIG_POPUPS_PER_DAY = 2          # how many times a day a new gig pops up on a user's dashboard
GIG_POPUP_MIN_GAP_HOURS = 3     # minimum time between those popups, so a quick refresh can't use both up
GIG_POPUP_DAYS = 7              # a gig keeps popping up for this many days after the admin adds it
SIGNUP_OTP_EXPIRY_MINUTES = 5   # email verification code sent right after registration, before
                                # the user is allowed through to the activation payment step


# ---- Fun Zone (weekend community chat with a daily prize) ----
FUN_ZONE_PRIZE = 100             # KES credited to the day's winner
FUN_ZONE_WEEKDAYS = (4, 5, 6)    # Friday, Saturday, Sunday (Monday = 0). Sessions can only be opened on these days.
FUN_ZONE_UTC_OFFSET_HOURS = 3    # East Africa Time (Nairobi) — "today" for Fun Zone is the EAT calendar day
FUN_ZONE_MAX_MESSAGE_LEN = 500   # characters per chat message
FUN_ZONE_MESSAGES_PER_WINDOW = 8     # flood protection: at most this many messages...
FUN_ZONE_RATE_WINDOW_SECONDS = 20    # ...per user in this many seconds
AVATAR_MAX_BYTES = 2 * 1024 * 1024   # profile pictures up to 2MB
GENDER_CHOICES = [('female', 'Female'), ('male', 'Male'), ('other', 'Other'), ('prefer_not', 'Prefer not to say')]
OCCUPATION_CHOICES = [('student', 'Student'), ('employee', 'Employee')]

ALLOWED_IMAGE_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_VIDEO_EXT = {'mp4', 'webm', 'mov', 'ogg'}
AD_UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'ads')
AD_IMAGE_FOLDER = os.path.join(AD_UPLOAD_FOLDER, 'images')
AD_VIDEO_FOLDER = os.path.join(AD_UPLOAD_FOLDER, 'videos')
SELL_PRODUCT_IMAGE_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'sell_products')
GIG_IMAGE_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'gigs')
DAILY_VIDEO_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'videos')
AVATAR_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'avatars')


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXT


def allowed_video_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_VIDEO_EXT


# ---------------------------------------------------------------------------
# MODELS
# ---------------------------------------------------------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    referral_code = db.Column(db.String(12), unique=True, nullable=False)
    referred_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    is_active_account = db.Column(db.Boolean, default=False)  # activation fee confirmed
    is_suspended = db.Column(db.Boolean, default=False)  # admin-suspended (e.g. fraud), blocks login
    suspension_reason = db.Column(db.String(255), nullable=True)
    wallet_balance = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=now_utc)
    last_seen_notification_id = db.Column(db.Integer, default=0)
    otp_code = db.Column(db.String(10), nullable=True)          # current login verification code
    otp_expires_at = db.Column(db.DateTime, nullable=True)
    reset_token = db.Column(db.String(100), nullable=True)      # self-service password reset
    reset_token_expires = db.Column(db.DateTime, nullable=True)

    # Optional profile details — shown to the admin (and, for the picture and
    # student/employee badge, inside the Fun Zone chat).
    profile_photo = db.Column(db.String(255), nullable=True)      # filename inside static/uploads/avatars
    bio = db.Column(db.String(300), nullable=True)                # short description
    gender = db.Column(db.String(20), nullable=True)
    age_years = db.Column(db.Integer, nullable=True)
    occupation_status = db.Column(db.String(20), nullable=True)   # 'student' or 'employee'

    referred_by = db.relationship('User', remote_side=[id])

    def generate_referral_code(self):
        while True:
            code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
            if not User.query.filter_by(referral_code=code).first():
                return code


class ActivationPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, default=ACTIVATION_FEE)
    mpesa_code = db.Column(db.String(50), nullable=True)  # filled in automatically by the callback,
                                                            # or manually if using the fallback flow
    checkout_request_id = db.Column(db.String(60), nullable=True, unique=True)
    merchant_request_id = db.Column(db.String(60), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(20), index=True, default='pending')  # pending / confirmed / failed / rejected
    result_desc = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=now_utc)
    processed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User')


class Photo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    caption = db.Column(db.String(200))
    reward_amount = db.Column(db.Float, default=PHOTO_LIKE_REWARD)
    uploaded_at = db.Column(db.DateTime, default=now_utc)

    def expired(self):
        return now_utc() > self.uploaded_at + timedelta(hours=24)


class PhotoLike(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    photo_id = db.Column(db.Integer, db.ForeignKey('photo.id'), nullable=False)
    liked_at = db.Column(db.DateTime, default=now_utc)
    __table_args__ = (db.UniqueConstraint('user_id', 'photo_id', name='uq_user_photo'),)


class Video(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    url = db.Column(db.String(500), nullable=True)             # external embeddable video URL
    video_filename = db.Column(db.String(255), nullable=True)  # OR uploaded directly from admin's device
    reward_amount = db.Column(db.Float, default=VIDEO_WATCH_REWARD)
    min_watch_seconds = db.Column(db.Integer, default=300)     # how long the user must watch (default 5 min)
    uploaded_at = db.Column(db.DateTime, default=now_utc)

    def expired(self):
        return now_utc() > self.uploaded_at + timedelta(hours=24)

    def video_src(self):
        if self.video_filename:
            return '/static/uploads/videos/' + self.video_filename
        return self.url


class VideoView(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False)
    viewed_at = db.Column(db.DateTime, default=now_utc)
    __table_args__ = (db.UniqueConstraint('user_id', 'video_id', name='uq_user_video'),)


class TriviaQuestion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    question_text = db.Column(db.String(500), nullable=False)
    option_a = db.Column(db.String(200), nullable=False)
    option_b = db.Column(db.String(200), nullable=False)
    option_c = db.Column(db.String(200), nullable=False)
    option_d = db.Column(db.String(200), nullable=False)
    correct_option = db.Column(db.String(1), nullable=False)  # 'a' / 'b' / 'c' / 'd'
    reward_amount = db.Column(db.Float, default=TRIVIA_CORRECT_REWARD)
    uploaded_at = db.Column(db.DateTime, default=now_utc)

    def expired(self):
        return now_utc() > self.uploaded_at + timedelta(hours=24)


class TriviaAnswer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey('trivia_question.id'), nullable=False)
    selected_option = db.Column(db.String(1), nullable=False)
    is_correct = db.Column(db.Boolean, default=False)
    answered_at = db.Column(db.DateTime, default=now_utc)
    __table_args__ = (db.UniqueConstraint('user_id', 'question_id', name='uq_user_question'),)


class Withdrawal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)          # amount deducted from wallet
    fee = db.Column(db.Float, default=0.0)                # transaction cost charged on this withdrawal
    net_amount = db.Column(db.Float, nullable=True)       # amount actually sent to the user (amount - fee)
    mpesa_number = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), index=True, default='pending')  # pending / approved / rejected
    requested_at = db.Column(db.DateTime, default=now_utc)
    processed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User')


class WithdrawalFeeTier(db.Model):
    """A transaction-cost bracket, e.g. 'KES 200-350 costs 15 bob'. Admin manages these;
    the withdrawal form looks up the matching tier for whatever amount the user enters."""
    id = db.Column(db.Integer, primary_key=True)
    min_amount = db.Column(db.Float, nullable=False)
    max_amount = db.Column(db.Float, nullable=False)
    fee = db.Column(db.Float, nullable=False)


class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(200), nullable=True)  # optional extra verification at login,
                                                        # once set
    withdrawal_source = db.Column(db.String(255), nullable=True)  # shown to users on their
                                                                    # dashboard, e.g. "M-Pesa Till 5502617"
    leaderboard_enabled = db.Column(db.Boolean, default=True)  # admin can hide the leaderboard
                                                                 # from users without deleting any data


class LedgerEntry(db.Model):
    """Manual money-in/money-out entries for the admin's own float, kept separate from the
    user-facing wallet system. A 'deposit' is money the admin has physically put into the
    site (e.g. topped up the till/paybill) via Add Value. A 'debit' is money committed the
    moment the admin creates a paid reward item (e.g. a Product with a reward amount) — this
    happens at creation time, not when a user later claims it. Net Position on the dashboard
    factors in the running balance of these entries."""
    id = db.Column(db.Integer, primary_key=True)
    entry_type = db.Column(db.String(10), index=True, nullable=False)   # 'deposit' or 'debit'
    amount = db.Column(db.Float, nullable=False)             # always stored positive
    description = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=now_utc)


def record_ledger_entry(entry_type, amount, description=None):
    """Create a LedgerEntry. Does not commit — caller commits alongside whatever else
    it's already doing in the same request."""
    if amount and amount > 0:
        entry = LedgerEntry(entry_type=entry_type, amount=amount, description=description)
        db.session.add(entry)


class ReferralCommission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    referrer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    referred_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, default=REFERRAL_BONUS)
    level = db.Column(db.Integer, default=1)  # 1 = direct referrer, 2 = that referrer's own referrer
    created_at = db.Column(db.DateTime, default=now_utc)


class Notification(db.Model):
    """A site-wide announcement. Shown as a popup to every user the next time they log in,
    after which it's marked seen for that user (via User.last_seen_notification_id)."""
    id = db.Column(db.Integer, primary_key=True)
    message = db.Column(db.String(300), nullable=False)
    link_url = db.Column(db.String(500), nullable=True)
    product_ad_id = db.Column(db.Integer, db.ForeignKey('product_ad.id'), nullable=True)  # when set,
        # the login popup shows this ad's actual photo/video playing automatically instead of
        # just a text message the user has to click through to see.
    created_at = db.Column(db.DateTime, default=now_utc)

    product_ad = db.relationship('ProductAd')


class PasswordResetRequest(db.Model):
    """A record of every forgot-password reset, so the admin has visibility even when
    the automated email fails to send (SMTP down, misconfigured, etc.). When email_sent
    is False, the temp password is shown here so the admin can share it with the user
    directly — by phone, WhatsApp, or their own personal email — as a manual fallback.
    Once handled, the admin marks it resolved; the password itself already took effect
    on the account the moment it was generated, this table is purely for follow-up."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    temp_password = db.Column(db.String(50), nullable=False)
    email_sent = db.Column(db.Boolean, index=True, default=False)
    resolved = db.Column(db.Boolean, index=True, default=False)
    created_at = db.Column(db.DateTime, default=now_utc)
    resolved_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User')


# ---- Writing Tasks ---------------------------------------------------------
class WritingTask(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    instructions = db.Column(db.Text, nullable=False)
    min_words = db.Column(db.Integer, default=300)
    price_usd = db.Column(db.Float, nullable=False)

    application_window_hours = db.Column(db.Float, default=DEFAULT_APPLICATION_WINDOW_HOURS)
    duration_minutes = db.Column(db.Integer, default=120)  # time allowed once assigned

    posted_at = db.Column(db.DateTime, default=now_utc)
    status = db.Column(db.String(20), index=True, default='open')  # open / assigned / submitted / approved / rejected

    assigned_to_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    assigned_at = db.Column(db.DateTime, nullable=True)
    deadline_at = db.Column(db.DateTime, nullable=True)

    assigned_to = db.relationship('User')

    def applications_open(self):
        return self.status == 'open' and now_utc() < (
            self.posted_at + timedelta(hours=self.application_window_hours))

    def is_past_deadline(self):
        return self.deadline_at is not None and now_utc() > self.deadline_at

    def price_kes(self):
        return self.price_usd * USD_TO_KES_RATE


class TaskUnlockPayment(db.Model):
    """A KES 20 PayHero STK Push payment a user makes to claim a specific open writing
    task. The first user whose payment is confirmed gets the task assigned to them
    automatically (see _finalize_task_unlock_payment). Mirrors ActivationPayment's
    shape so the same STK-push-then-webhook pattern applies."""
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('writing_task.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, default=TASK_UNLOCK_FEE)
    phone = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(20), index=True, default='pending')  # pending / confirmed / failed / superseded
    checkout_request_id = db.Column(db.String(100), unique=True, nullable=True)
    merchant_request_id = db.Column(db.String(100), nullable=True)
    mpesa_code = db.Column(db.String(50), nullable=True)
    result_desc = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=now_utc)
    processed_at = db.Column(db.DateTime, nullable=True)

    task = db.relationship('WritingTask')
    user = db.relationship('User')


class TaskSubmission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('writing_task.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    word_count = db.Column(db.Integer, default=0)
    submitted_at = db.Column(db.DateTime, default=now_utc)
    status = db.Column(db.String(20), index=True, default='pending')  # pending / approved / rejected
    admin_note = db.Column(db.String(300), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    task = db.relationship('WritingTask')
    user = db.relationship('User')


# ---- Product Advertisements -------------------------------------------------
class ProductAd(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.String(400))
    image_filename = db.Column(db.String(255), nullable=True)     # uploaded directly from admin's device
    video_filename = db.Column(db.String(255), nullable=True)     # uploaded directly from admin's device
    video_url = db.Column(db.String(500), nullable=True)          # OR an external direct-playable video link
    link_url = db.Column(db.String(500), nullable=True)           # optional — destination when clicked
    reward_amount = db.Column(db.Float, default=0.0)               # KES earned for viewing this ad (0 = no reward)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now_utc)


class ProductAdView(db.Model):
    """Tracks that a user has already claimed the reward for viewing a given product ad,
    so each user is only paid once per ad."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    ad_id = db.Column(db.Integer, db.ForeignKey('product_ad.id'), nullable=False)
    viewed_at = db.Column(db.DateTime, default=now_utc)
    __table_args__ = (db.UniqueConstraint('user_id', 'ad_id', name='uq_user_ad_view'),)


class SellProduct(db.Model):
    """A marketplace/classifieds-style listing admin posts on behalf of a seller.
    Every field is optional by design — admin can post as much or as little detail as
    they have at the time and fill in the rest later."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=True)
    image_filename = db.Column(db.String(255), nullable=True)
    cost = db.Column(db.Float, nullable=True)  # KES
    description = db.Column(db.Text, nullable=True)
    owner_name = db.Column(db.String(150), nullable=True)
    owner_contact = db.Column(db.String(100), nullable=True)  # phone or email
    location = db.Column(db.String(150), nullable=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now_utc)


class Gig(db.Model):
    """A free gig/opportunity the admin posts for users to browse. Users see the picture
    and title on the list, and tap 'View More' to read the notes/description, open the
    gig link, or message the support number on WhatsApp."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    image_filename = db.Column(db.String(255), nullable=True)
    link_url = db.Column(db.String(500), nullable=True)
    description = db.Column(db.Text, nullable=True)
    whatsapp_number = db.Column(db.String(30), nullable=True)  # digits only, with country code
    active = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=now_utc)

    @property
    def whatsapp_link(self):
        return f'https://wa.me/{self.whatsapp_number}' if self.whatsapp_number else None


class GigPopupLog(db.Model):
    """Tracks how often a user has been shown a gig's dashboard popup on a given day, so
    each new gig pops up at most GIG_POPUPS_PER_DAY times a day per user."""
    __table_args__ = (db.UniqueConstraint('user_id', 'gig_id', 'day', name='uq_gig_popup_user_gig_day'),)
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    gig_id = db.Column(db.Integer, db.ForeignKey('gig.id', ondelete='CASCADE'), nullable=False)
    day = db.Column(db.Date, nullable=False)
    shown_count = db.Column(db.Integer, default=0)
    last_shown_at = db.Column(db.DateTime, default=now_utc)


def normalize_whatsapp(raw):
    """Turn whatever the admin typed into a wa.me-ready number (digits, country code first).
    Kenyan local numbers (07xx / 01xx) get 254 prepended. Returns None if unusable."""
    digits = re.sub(r'\D', '', raw or '')
    if digits.startswith('00'):
        digits = digits[2:]
    elif digits.startswith('0'):
        digits = '254' + digits[1:]
    elif len(digits) == 9 and digits[0] in '71':
        digits = '254' + digits
    return digits if 9 <= len(digits) <= 15 else None


class SupportMessage(db.Model):
    """A message a logged-in user sends to admin via the 'Contact Admin' feature."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), index=True, default='new')  # new / read / replied
    submitted_at = db.Column(db.DateTime, default=now_utc)

    user = db.relationship('User')


class AdvertiseRequest(db.Model):
    """A request from a company wanting to advertise on BizNova. Submitted via the public
    /advertise page; admin reviews it in the admin panel and follows up by email/phone."""
    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(200), nullable=False)
    contact_email = db.Column(db.String(200), nullable=False)
    contact_phone = db.Column(db.String(30), nullable=True)
    product_name = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), index=True, default='new')  # new / contacted / closed
    submitted_at = db.Column(db.DateTime, default=now_utc)


class LeaderboardPayout(db.Model):
    """A cash bonus admin manually awarded to a top referrer for a given period.
    Recorded so history is visible and the same period/user isn't paid twice by accident."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    period_label = db.Column(db.String(50), nullable=False)  # e.g. "Week of 14 Jul 2026"
    paid_at = db.Column(db.DateTime, default=now_utc)

    user = db.relationship('User')


# ---- Surveys ------------------------------------------------------------
class Survey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.String(400))
    reward_amount = db.Column(db.Float, default=10.0)  # KES, credited immediately on completion
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now_utc)

    questions = db.relationship('SurveyQuestion', backref='survey', order_by='SurveyQuestion.id')


class SurveyQuestion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey.id'), nullable=False)
    question_text = db.Column(db.String(400), nullable=False)


class SurveyResponse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    submitted_at = db.Column(db.DateTime, default=now_utc)
    __table_args__ = (db.UniqueConstraint('survey_id', 'user_id', name='uq_survey_user'),)


class SurveyAnswer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    response_id = db.Column(db.Integer, db.ForeignKey('survey_response.id'), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey('survey_question.id'), nullable=False)
    answer_text = db.Column(db.Text, nullable=False)


# ---- Fun Zone ------------------------------------------------------------
class FunZoneSession(db.Model):
    """One Fun Zone chat per day (Friday, Saturday or Sunday, EAT). The admin opens it and
    either sets the topic personally or lets the system pick a member to bring the topic —
    that member is notified on their dashboard. Once a topic exists the session is 'open'
    and active members can chat; the admin then picks the day's winner, who is credited
    FUN_ZONE_PRIZE to their wallet."""
    id = db.Column(db.Integer, primary_key=True)
    session_date = db.Column(db.Date, nullable=False, unique=True)   # EAT calendar day
    topic = db.Column(db.String(300), nullable=True)
    topic_source = db.Column(db.String(10), nullable=True)           # 'admin' or 'member'
    status = db.Column(db.String(20), index=True, default='awaiting_topic')  # awaiting_topic / open / closed
    host_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)  # member picked to bring the topic
    winner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    prize_amount = db.Column(db.Float, default=FUN_ZONE_PRIZE)
    prize_paid = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now_utc)
    opened_at = db.Column(db.DateTime, nullable=True)
    closed_at = db.Column(db.DateTime, nullable=True)

    host = db.relationship('User', foreign_keys=[host_user_id])
    winner = db.relationship('User', foreign_keys=[winner_id])


class FunZoneMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('fun_zone_session.id'), index=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    body = db.Column(db.String(FUN_ZONE_MAX_MESSAGE_LEN), nullable=False)
    created_at = db.Column(db.DateTime, default=now_utc)

    user = db.relationship('User')


# ---------------------------------------------------------------------------
# HELPERS / DECORATORS
# ---------------------------------------------------------------------------
# Simple in-memory rate limiter for auth-sensitive endpoints (login, OTP verification,
# forgot-password) — protects against brute-forcing a password or a 6-digit OTP code
# with unlimited automated attempts. This is intentionally dependency-free and lives in
# process memory, which is fine for a single-process deployment; if you later run
# multiple worker processes behind a load balancer, swap this for Flask-Limiter with a
# shared Redis backend so the counts are consistent across workers.
_rate_limit_buckets = {}


def is_rate_limited(key, max_attempts, window_seconds):
    """Returns True (and does NOT count this attempt) if `key` has already hit
    max_attempts within the last window_seconds. Otherwise records this attempt and
    returns False."""
    now = time.time()
    attempts = _rate_limit_buckets.setdefault(key, [])
    while attempts and attempts[0] < now - window_seconds:
        attempts.pop(0)
    if len(attempts) >= max_attempts:
        return True
    attempts.append(now)
    return False


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to continue.', 'error')
            return redirect(url_for('login'))
        user = User.query.get(session['user_id'])
        if not user or user.is_suspended:
            session.pop('user_id', None)
            flash('Your account has been suspended. Contact support if you believe this is a mistake.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def active_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = User.query.get(session['user_id'])
        if not user or not user.is_active_account:
            flash('Please activate your account (pay the one-time activation fee) to access this.', 'error')
            return redirect(url_for('activate'))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'admin_id' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return wrapper


def current_user():
    if 'user_id' in session:
        return User.query.get(session['user_id'])
    return None


@app.context_processor
def inject_current_user():
    return {'current_user_obj': current_user()}


@app.context_processor
def inject_pending_notification():
    notif = session.pop('pending_notification', None)
    return {'pending_notification': notif}


@app.context_processor
def inject_leaderboard_enabled():
    admin = Admin.query.first()
    return {'leaderboard_enabled': admin.leaderboard_enabled if admin else True}


@app.errorhandler(404)
def handle_404(e):
    return render_template('error.html', code=404,
                            message="That page doesn't exist."), 404


@app.errorhandler(500)
def handle_500(e):
    # Log the full exception server-side; the visitor only ever sees a generic
    # message, never a stack trace, file path, or other internal detail.
    logger.exception('Unhandled server error on %s %s', request.method, request.path)
    return render_template('error.html', code=500,
                            message="Something went wrong on our end. Please try again shortly."), 500


def notify_user_email(user, subject, body):
    """Best-effort email notification — never blocks the calling flow if sending fails
    (e.g. SMTP not configured yet). Silently no-ops if the user has no email on file.
    Returns True if the email was actually sent, False otherwise — most callers can
    ignore this, but flows where losing the email matters (e.g. forgot-password, which
    hands the user a one-time temporary password) use it to fall back to something else."""
    if not user or not user.email:
        return False
    try:
        mailer.send_email(mail_config, user.email, subject, body)
        return True
    except mailer.MailError as e:
        # Never surface this to the visitor — just record it so the admin can spot
        # SMTP problems in the server logs.
        logger.error('notify_user_email failed for user_id=%s subject=%r: %s', user.id, subject, e)
        return False


def notify_admin_email(subject, body):
    """Best-effort notification to the admin's own email (set at /admin/settings).
    No-ops silently if no admin email is on file, or if any admin exists but hasn't set one."""
    admin = Admin.query.first()
    if not admin or not admin.email:
        return
    try:
        mailer.send_email(mail_config, admin.email, subject, body)
    except mailer.MailError as e:
        logger.error('notify_admin_email failed subject=%r: %s', subject, e)


def activate_user_and_pay_referral(user):
    """Marks a user active and pays referral commissions, if any: KES {REFERRAL_BONUS} to
    whoever directly referred them (level 1), and KES {LEVEL2_REFERRAL_BONUS} to whoever
    referred THAT person (level 2), if such a person exists. Called automatically from the
    PayHero callback, or manually by admin as a fallback."""
    user.is_active_account = True
    if not user.referred_by_id:
        return

    level1_referrer = User.query.get(user.referred_by_id)
    already_paid = ReferralCommission.query.filter_by(
        referrer_id=user.referred_by_id, referred_id=user.id, level=1).first()

    if level1_referrer and not already_paid:
        db.session.add(ReferralCommission(referrer_id=level1_referrer.id, referred_id=user.id,
                                           amount=REFERRAL_BONUS, level=1))
        level1_referrer.wallet_balance += REFERRAL_BONUS
        notify_user_email(
            level1_referrer, 'You earned a referral bonus on BizNova',
            f'Hi {level1_referrer.full_name},\n\n'
            f'Great news — @{user.username} just activated their account using your '
            f'referral link, and KES {REFERRAL_BONUS} has been added to your wallet.\n\n'
            f'Keep sharing your link to earn more!\n\n— BizNova'
        )

        # Level 2: whoever referred level1_referrer also earns a smaller bonus, since their
        # network indirectly grew this new activation too.
        if level1_referrer.referred_by_id:
            level2_referrer = User.query.get(level1_referrer.referred_by_id)
            already_paid_l2 = ReferralCommission.query.filter_by(
                referrer_id=level1_referrer.referred_by_id, referred_id=user.id, level=2).first()
            if level2_referrer and not already_paid_l2:
                db.session.add(ReferralCommission(referrer_id=level2_referrer.id, referred_id=user.id,
                                                   amount=LEVEL2_REFERRAL_BONUS, level=2))
                level2_referrer.wallet_balance += LEVEL2_REFERRAL_BONUS
                notify_user_email(
                    level2_referrer, 'You earned a level 2 referral bonus on BizNova',
                    f'Hi {level2_referrer.full_name},\n\n'
                    f'@{user.username} just activated their BizNova account after joining through '
                    f'someone in your referral network, and KES {LEVEL2_REFERRAL_BONUS} has been '
                    f'added to your wallet as a level 2 referral bonus.\n\n— BizNova'
                )


# ---------------------------------------------------------------------------
# PROFILE PICTURES + FUN ZONE HELPERS
# ---------------------------------------------------------------------------
def avatar_url(user):
    """URL of a user's profile picture, or None if they haven't uploaded one."""
    if user and user.profile_photo:
        return url_for('static', filename='uploads/avatars/' + user.profile_photo)
    return None


app.jinja_env.globals['avatar_url'] = avatar_url
app.jinja_env.globals['FUN_ZONE_PRIZE'] = FUN_ZONE_PRIZE


def _sniff_image_extension(data):
    """Identify an image by its first bytes rather than trusting the filename."""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if data.startswith(b'GIF87a') or data.startswith(b'GIF89a'):
        return 'gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    return None


def save_avatar(file_storage):
    """Validates and stores an uploaded profile picture. Returns the new filename.
    Raises ValueError (with a user-friendly message) if the file isn't acceptable."""
    if not allowed_file(file_storage.filename or ''):
        raise ValueError('Profile picture must be a PNG, JPG, GIF or WEBP image.')
    data = file_storage.read(AVATAR_MAX_BYTES + 1)
    if len(data) > AVATAR_MAX_BYTES:
        raise ValueError('Profile picture is too large — please keep it under 2MB.')
    ext = _sniff_image_extension(data)
    if not ext:
        raise ValueError('That file does not look like a valid image.')
    os.makedirs(AVATAR_FOLDER, exist_ok=True)
    filename = f'{secrets.token_hex(12)}.{ext}'
    with open(os.path.join(AVATAR_FOLDER, filename), 'wb') as fh:
        fh.write(data)
    return filename


def delete_avatar_file(filename):
    if not filename:
        return
    safe = os.path.basename(filename)
    path = os.path.join(AVATAR_FOLDER, safe)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as e:
        logger.warning('Could not delete old avatar %s: %s', safe, e)


def eat_now():
    """Current time in East Africa Time — what "today" means for the Fun Zone."""
    return now_utc() + timedelta(hours=FUN_ZONE_UTC_OFFSET_HOURS)


def fun_zone_today():
    return eat_now().date()


def is_fun_zone_day(day=None):
    day = day or fun_zone_today()
    return day.weekday() in FUN_ZONE_WEEKDAYS


def next_fun_zone_day_label():
    """Human-friendly label for when the Fun Zone next runs (used on the closed page)."""
    day = fun_zone_today()
    for _ in range(8):
        if is_fun_zone_day(day):
            return 'today' if day == fun_zone_today() else day.strftime('%A')
        day += timedelta(days=1)
    return 'Friday'


def get_today_fun_zone_session():
    return FunZoneSession.query.filter_by(session_date=fun_zone_today()).first()


def fun_zone_is_live(fz_session):
    """A session accepts chat only while it's open AND it's still the day it was opened for."""
    return bool(fz_session and fz_session.status == 'open'
                and fz_session.session_date == fun_zone_today() and is_fun_zone_day())


def mask_username(name):
    """Hides the middle letters of a username for other members, e.g. 'johndoe' -> 'j*****e'.
    The admin pages still show the full username."""
    name = (name or '').strip()
    if len(name) <= 1:
        return name or 'member'
    if len(name) == 2:
        return name[0] + '*'
    return name[0] + '*' * min(len(name) - 2, 5) + name[-1]


app.jinja_env.filters['mask_username'] = mask_username


def serialize_fun_zone_message(msg, viewer_id):
    return {
        'id': msg.id,
        'user_id': msg.user_id,
        'username': mask_username(msg.user.username) if msg.user else 'member',
        'avatar': avatar_url(msg.user),
        'status': msg.user.occupation_status if msg.user else None,
        'body': msg.body,
        'time': (msg.created_at + timedelta(hours=FUN_ZONE_UTC_OFFSET_HOURS)).strftime('%H:%M'),
        'mine': msg.user_id == viewer_id,
    }


def pick_fun_zone_host(exclude_ids=()):
    """Randomly selects an active, non-suspended member to bring the day's topic. Members
    who have not hosted before are preferred so the role rotates fairly."""
    excluded = set(exclude_ids)
    candidates = User.query.filter(User.is_active_account == True, User.is_suspended == False).all()
    candidates = [u for u in candidates if u.id not in excluded]
    if not candidates:
        return None
    previous_hosts = {row[0] for row in db.session.query(FunZoneSession.host_user_id).filter(
        FunZoneSession.host_user_id.isnot(None)).all()}
    fresh = [u for u in candidates if u.id not in previous_hosts]
    return secrets.choice(fresh or candidates)


# ---------------------------------------------------------------------------
# PUBLIC ROUTES
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/robots.txt')
def robots_txt():
    lines = [
        'User-agent: *',
        'Allow: /$',
        'Allow: /login',
        'Allow: /register',
        'Allow: /advertise',
        'Allow: /forgot-password',
        'Disallow: /dashboard',
        'Disallow: /admin',
        'Disallow: /photos',
        'Disallow: /videos',
        'Disallow: /trivia',
        'Disallow: /writing-tasks',
        'Disallow: /surveys',
        'Disallow: /product-ads',
        'Disallow: /sell-products',
        'Disallow: /leaderboard',
        'Disallow: /wallet',
        'Disallow: /referrals',
        'Disallow: /activate',
        'Disallow: /verify-signup-otp',
        'Disallow: /profile',
        'Disallow: /payhero/',
        f"Sitemap: {url_for('sitemap_xml', _external=True)}",
    ]
    return Response('\n'.join(lines), mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    today = now_utc().strftime('%Y-%m-%d')
    pages = [
        {'loc': url_for('index', _external=True), 'priority': '1.0', 'changefreq': 'daily'},
        {'loc': url_for('register', _external=True), 'priority': '0.9', 'changefreq': 'monthly'},
        {'loc': url_for('login', _external=True), 'priority': '0.4', 'changefreq': 'yearly'},
        {'loc': url_for('advertise', _external=True), 'priority': '0.6', 'changefreq': 'monthly'},
        {'loc': url_for('forgot_password', _external=True), 'priority': '0.2', 'changefreq': 'yearly'},
        {'loc': url_for('terms', _external=True), 'priority': '0.3', 'changefreq': 'yearly'},
        {'loc': url_for('privacy', _external=True), 'priority': '0.3', 'changefreq': 'yearly'},
    ]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for p in pages:
        xml.append(f"<url><loc>{p['loc']}</loc><lastmod>{today}</lastmod>"
                    f"<changefreq>{p['changefreq']}</changefreq><priority>{p['priority']}</priority></url>")
    xml.append('</urlset>')
    return Response('\n'.join(xml), mimetype='application/xml')


ADVERTISE_CONTACT_EMAIL = 'biznova254@gmail.com'  # change to your real inbox


@app.route('/advertise')
def advertise():
    return render_template('advertise.html', contact_email=ADVERTISE_CONTACT_EMAIL)


@app.route('/advertise/submit', methods=['POST'])
def advertise_submit():
    company_name = request.form.get('company_name', '').strip()
    contact_email = request.form.get('contact_email', '').strip()
    contact_phone = request.form.get('contact_phone', '').strip()
    product_name = request.form.get('product_name', '').strip()
    message = request.form.get('message', '').strip()

    if not all([company_name, contact_email, product_name]):
        flash('Please fill in your company name, email, and product name.', 'error')
        return redirect(url_for('advertise'))

    req = AdvertiseRequest(company_name=company_name, contact_email=contact_email,
                            contact_phone=contact_phone, product_name=product_name, message=message)
    db.session.add(req)
    db.session.commit()
    flash("Thanks! Your advertising request has been sent — we'll get back to you by email shortly.",
          'success')
    return redirect(url_for('advertise'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone_digits = request.form.get('phone_digits', '').strip()
        phone = '254' + phone_digits if phone_digits else ''
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        ref_code = request.form.get('referral_code', '').strip().upper()
        agree_terms = request.form.get('agree_terms')

        if not all([full_name, username, email, phone_digits, password, confirm]):
            flash('Please fill in all fields.', 'error')
            return redirect(url_for('register'))

        if not agree_terms:
            flash('Please tick the box to confirm you agree to the Terms of Service and Privacy Policy.', 'error')
            return redirect(url_for('register'))

        if not phone_digits.isdigit() or len(phone_digits) != 9:
            flash('Please enter a valid 9-digit number after 254 (e.g. 712345678).', 'error')
            return redirect(url_for('register'))

        if password != confirm:
            flash('Passwords do not match.', 'error')
            return redirect(url_for('register'))

        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'error')
            return redirect(url_for('register'))

        if User.query.filter_by(username=username).first():
            flash('Username already taken.', 'error')
            return redirect(url_for('register'))

        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
            return redirect(url_for('register'))

        if User.query.filter_by(phone=phone).first():
            flash('Phone number already registered.', 'error')
            return redirect(url_for('register'))

        referred_by_id = None
        if ref_code:
            referrer = User.query.filter_by(referral_code=ref_code).first()
            if referrer:
                referred_by_id = referrer.id
            else:
                flash('Referral code not found — continuing without it.', 'error')

        user = User(
            full_name=full_name,
            username=username,
            email=email,
            phone=phone,
            password_hash=generate_password_hash(password),
            referred_by_id=referred_by_id,
        )
        user.referral_code = user.generate_referral_code()
        db.session.add(user)
        db.session.commit()

        notify_admin_email(
            'New user joined BizNova',
            f'A new user just registered:\n\n'
            f'Name: {user.full_name}\nUsername: {user.username}\nEmail: {user.email}\n'
            f'Phone: {user.phone}\n'
            f'{"Referred by user ID: " + str(referred_by_id) if referred_by_id else "No referral"}'
        )

        sent = send_signup_otp(user)
        session['pending_signup_user_id'] = user.id
        session['pending_signup_email'] = mask_email(user.email)
        if sent:
            flash(f'A verification code was sent to {user.email}. Enter it to continue.', 'success')
            return redirect(url_for('verify_signup_otp'))
        else:
            # Email couldn't be sent (e.g. SMTP not configured yet) — don't block the
            # signup; let them straight through. The real reason is only written to the
            # server-side log — never shown to the visitor.
            logger.error('Signup OTP email failed to send for user_id=%s', user.id)
            session.pop('pending_signup_user_id', None)
            session.pop('pending_signup_email', None)
            session['user_id'] = user.id
            flash('Account created! Pay the one-time activation fee to start earning.', 'success')
            return redirect(url_for('activate'))

    ref_code = request.args.get('ref', '')
    return render_template('register.html', ref_code=ref_code)


def complete_login(user):
    """Finishes logging a user in after their password has been verified."""
    session.pop('pending_signup_user_id', None)
    session.pop('pending_signup_email', None)
    session['user_id'] = user.id
    session.permanent = True  # stay signed in across browser restarts (31 days by default)
    flash(f'Welcome back, {user.full_name}!', 'success')

    latest_notif = Notification.query.order_by(Notification.id.desc()).first()
    if latest_notif and latest_notif.id > (user.last_seen_notification_id or 0):
        notif_data = {'message': latest_notif.message, 'link_url': latest_notif.link_url}
        ad = latest_notif.product_ad
        if ad:
            notif_data['image_filename'] = ad.image_filename
            notif_data['video_filename'] = ad.video_filename
            notif_data['video_url'] = ad.video_url
            notif_data['reward_amount'] = ad.reward_amount
        session['pending_notification'] = notif_data
        user.last_seen_notification_id = latest_notif.id
        db.session.commit()

    return redirect(url_for('dashboard'))


def get_withdrawal_fee(amount):
    """Looks up the transaction cost for a withdrawal amount from the tiers admin has set up.
    Returns 0 if no tier covers this amount (e.g. admin hasn't configured one that high/low)."""
    tier = WithdrawalFeeTier.query.filter(
        WithdrawalFeeTier.min_amount <= amount, WithdrawalFeeTier.max_amount >= amount
    ).first()
    return tier.fee if tier else 0.0


def mask_email(email):
    """Turns 'jane.doe@example.com' into 'ja***@example.com' for display during signup
    email verification."""
    if '@' not in email:
        return email
    local, domain = email.split('@', 1)
    visible = local[:2] if len(local) > 2 else local[:1]
    return f'{visible}{"*" * max(len(local) - len(visible), 3)}@{domain}'


def find_user_for_login(identifier):
    """Looks a user up by username, email or phone — forgiving about how they typed it:
    email is case-insensitive, and a phone number works as 0712345678, 712345678,
    +254712345678 or 254712345678 (phones are stored as 254XXXXXXXXX)."""
    ident = (identifier or '').strip()
    if not ident:
        return None
    user = User.query.filter(User.username == ident).first()
    if user:
        return user
    user = User.query.filter(db.func.lower(User.email) == ident.lower()).first()
    if user:
        return user
    digits = re.sub(r'\D', '', ident)
    if digits and re.fullmatch(r'[+\d\s\-()]+', ident):
        if digits.startswith('00'):
            digits = digits[2:]
        if digits.startswith('0'):
            digits = '254' + digits[1:]
        elif len(digits) == 9:
            digits = '254' + digits
        return User.query.filter(User.phone == digits).first()
    return None


@app.route('/login', methods=['GET', 'POST'])
def login():
    # Already signed in? Skip the form and go straight to the dashboard.
    if 'user_id' in session and User.query.get(session['user_id']):
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        password = request.form.get('password', '')

        rate_key = f'login:{request.remote_addr}:{identifier.lower()}'
        if is_rate_limited(rate_key, max_attempts=8, window_seconds=600):
            flash('Too many login attempts. Please wait a few minutes and try again.', 'error')
            return redirect(url_for('login'))

        user = find_user_for_login(identifier)
        if user and check_password_hash(user.password_hash, password):
            if user.is_suspended:
                flash('Your account has been suspended. Contact support if you believe this is a mistake.', 'error')
                return redirect(url_for('login'))

            return complete_login(user)

        flash('Invalid credentials.', 'error')
        return redirect(url_for('login'))
    return render_template('login.html')


def send_signup_otp(user):
    """Generates a fresh 6-digit code (expires in SIGNUP_OTP_EXPIRY_MINUTES) and emails it to
    a newly-registered user to verify their email address before they can proceed to activation.
    Returns True if the email was sent, False if sending failed."""
    code = f'{secrets.randbelow(1000000):06d}'
    user.otp_code = code
    user.otp_expires_at = now_utc() + timedelta(minutes=SIGNUP_OTP_EXPIRY_MINUTES)
    db.session.commit()

    body = (f'Hi {user.full_name},\n\n'
            f'Welcome to BizNova! Your email verification code is: {code}\n\n'
            f'This code expires in {SIGNUP_OTP_EXPIRY_MINUTES} minutes. Enter it to continue to '
            f'the activation payment step.\n\n— BizNova')
    try:
        mailer.send_email(mail_config, user.email, 'Verify your BizNova email', body)
        return True
    except mailer.MailError:
        return False


@app.route('/verify-signup-otp', methods=['GET', 'POST'])
def verify_signup_otp():
    user_id = session.get('pending_signup_user_id')
    if not user_id:
        return redirect(url_for('register'))
    user = User.query.get(user_id)
    if not user:
        session.pop('pending_signup_user_id', None)
        return redirect(url_for('register'))

    if request.method == 'POST':
        if is_rate_limited(f'otp-verify:{user.id}', max_attempts=6, window_seconds=600):
            flash('Too many incorrect attempts. Please request a new code and try again.', 'error')
            return redirect(url_for('verify_signup_otp'))

        code = request.form.get('code', '').strip()
        if not user.otp_code or not user.otp_expires_at or now_utc() > user.otp_expires_at:
            flash('That code has expired. Please request a new one.', 'error')
        elif code != user.otp_code:
            flash('Incorrect code. Please try again.', 'error')
        else:
            user.otp_code = None
            user.otp_expires_at = None
            db.session.commit()
            session.pop('pending_signup_user_id', None)
            session.pop('pending_signup_email', None)
            session['user_id'] = user.id
            flash('Email verified! Pay the one-time activation fee to start earning.', 'success')
            return redirect(url_for('activate'))
        return redirect(url_for('verify_signup_otp'))

    masked_email = session.get('pending_signup_email', '')
    return render_template('verify_signup_otp.html', masked_email=masked_email)


@app.route('/verify-signup-otp/resend', methods=['POST'])
def resend_signup_otp():
    user_id = session.get('pending_signup_user_id')
    if not user_id:
        return redirect(url_for('register'))
    user = User.query.get(user_id)
    if not user:
        return redirect(url_for('register'))

    if is_rate_limited(f'otp-resend:{user.id}', max_attempts=3, window_seconds=600):
        flash('Please wait a bit before requesting another code.', 'error')
        return redirect(url_for('verify_signup_otp'))

    sent = send_signup_otp(user)
    if sent:
        flash(f'A new code was sent to {user.email}.', 'success')
    else:
        logger.error('Resend signup OTP email failed to send for user_id=%s', user.id)
        flash('Please try again shortly.', 'error')
    return redirect(url_for('verify_signup_otp'))


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()

        # Always show the same message whether or not the email exists (or was rate
        # limited), so this page can't be used to check which addresses have an account.
        generic_message = ('If that email is registered with us, a new password has been '
                            'sent to it — check your inbox.')

        rate_key = f'forgot-password:{request.remote_addr}:{email}'
        if is_rate_limited(rate_key, max_attempts=3, window_seconds=3600):
            flash(generic_message, 'success')
            return redirect(url_for('login'))

        user = User.query.filter_by(email=email).first()

        if user:
            temp_password = secrets.token_urlsafe(6)
            user.password_hash = generate_password_hash(temp_password)
            db.session.commit()

            email_sent = notify_user_email(
                user, 'Your new BizNova password',
                f'Hi {user.full_name},\n\n'
                f'As requested, here is a new password for your BizNova account:\n\n'
                f'{temp_password}\n\n'
                f'Log in with it, then head to your Profile page to set a password of your own.\n\n'
                f"If you didn't request this, please log in and change your password right away.\n\n"
                f'— BizNova'
            )

            db.session.add(PasswordResetRequest(
                user_id=user.id, temp_password=temp_password, email_sent=email_sent))
            db.session.commit()

            if not email_sent:
                # The user's password already changed regardless — this is purely so the
                # admin can see it happened and step in manually if the user can't get in.
                notify_admin_email(
                    'BizNova: a password reset email failed to send',
                    f'{user.full_name} (@{user.username}, {user.email}) requested a password '
                    f'reset, but the confirmation email could not be sent. Check '
                    f'/admin/password-resets to see the temporary password and share it with '
                    f'them directly.'
                )

        flash(generic_message, 'success')
        return redirect(url_for('login'))

    return render_template('forgot_password.html')


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    flash('Logged out successfully.', 'success')
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# ACTIVATION (automatic via M-Pesa STK Push)
# ---------------------------------------------------------------------------
@app.route('/activate')
@login_required
def activate():
    user = current_user()
    if user.is_active_account:
        return redirect(url_for('dashboard'))
    pending = ActivationPayment.query.filter_by(user_id=user.id, status='pending').order_by(
        ActivationPayment.created_at.desc()).first()
    # Only an STK attempt started in the last 3 minutes is worth resuming the wait for.
    resumable = ActivationPayment.query.filter(
        ActivationPayment.user_id == user.id, ActivationPayment.status == 'pending',
        ActivationPayment.checkout_request_id.isnot(None),
        ActivationPayment.created_at >= now_utc() - timedelta(minutes=3)).first()
    return render_template('activate.html', resumable=resumable, user=user, fee=ACTIVATION_FEE, pending=pending)


@app.route('/activate/initiate', methods=['POST'])
@login_required
def activate_initiate():
    user = current_user()
    if user.is_active_account:
        return jsonify({'success': False, 'message': 'Account already active.'}), 400

    phone = request.form.get('phone', '').strip()
    if not phone:
        return jsonify({'success': False, 'message': 'Please enter a phone number.'}), 400

    # Retire any earlier STK attempt that is still pending so the user can retry. It is
    # marked failed rather than deleted: if that earlier prompt WAS paid and its callback
    # arrives late, _finalize_activation_payment still recognises it and activates the
    # user. Manual M-Pesa code submissions (no checkout id) are left untouched.
    for old in ActivationPayment.query.filter(
            ActivationPayment.user_id == user.id, ActivationPayment.status == 'pending',
            ActivationPayment.checkout_request_id.isnot(None)).all():
        old.status = 'failed'
        old.result_desc = 'Replaced by a newer payment attempt'
    db.session.commit()

    try:
        result = payhero.stk_push(
            payhero_config, phone=phone, amount=ACTIVATION_FEE,
            account_reference=user.username, transaction_desc='BizNova Activation',
            customer_name=user.username,
        )
    except payhero.PayHeroError as e:
        # The real cause (connection failures, timeouts, PayHero's own error text — which
        # can include raw URLs, hostnames, or internal detail) goes to the server log only.
        # The user only ever sees a safe, generic message, never that technical detail.
        logger.error('PayHero STK push failed for activation, user_id=%s: %s', user.id, e)
        return jsonify({
            'success': False,
            'message': "We couldn't reach the payment provider right now. Please try again in "
                       "a moment, or use the manual payment option below.",
            'fallback': True,
        }), 502

    payment = ActivationPayment(
        user_id=user.id, amount=ACTIVATION_FEE, phone=phone, status='pending',
        checkout_request_id=result.get('CheckoutRequestID'),
        merchant_request_id=result.get('MerchantRequestID'),
    )
    db.session.add(payment)
    db.session.commit()

    return jsonify({'success': True, 'message': 'STK push sent — check your phone and enter your M-Pesa PIN.',
                     'checkout_request_id': payment.checkout_request_id})


@app.route('/activate/check')
@login_required
def activate_check():
    """Lightweight check used by the Activate page while the user waits: becomes active as
    soon as the payment is confirmed (STK callback, status check, or admin confirming a
    manual M-Pesa code), and the page then sends them straight to the dashboard."""
    user = current_user()
    active = bool(user and user.is_active_account)
    return jsonify({'active': active, 'redirect': url_for('dashboard') if active else None})


@app.route('/activate/status/<checkout_request_id>')
@login_required
def activate_status(checkout_request_id):
    user = current_user()
    if user.is_active_account:
        return jsonify({'status': 'confirmed', 'active': True, 'redirect': url_for('dashboard')})

    payment = ActivationPayment.query.filter_by(
        user_id=user.id, checkout_request_id=checkout_request_id).first()
    if not payment:
        return jsonify({'status': 'unknown'})

    # Safety net: if the callback hasn't arrived yet, ask PayHero directly. This makes
    # activation work even if the webhook is slow, or its URL/secret is misconfigured.
    if payment.status == 'pending' and payment.merchant_request_id:
        if not is_rate_limited(f'payhero-status:{payment.id}', max_attempts=1, window_seconds=4):
            info = payhero.transaction_status(payhero_config, payment.merchant_request_id)
            if info and info['state'] == 'success':
                _finalize_activation_payment(checkout_request_id, True, 'Confirmed via status check',
                                              info.get('receipt'))
            elif info and info['state'] == 'failed':
                _finalize_activation_payment(checkout_request_id, False,
                                              info.get('description') or 'Payment failed', None)
            db.session.refresh(payment)
            db.session.refresh(user)

    active = bool(user.is_active_account)
    return jsonify({'status': 'confirmed' if active else payment.status, 'active': active,
                     'result_desc': payment.result_desc,
                     'redirect': url_for('dashboard') if active else None})


MPESA_CODE_RE = re.compile(r'^[A-Z0-9]{10}$')


def clean_mpesa_code(raw):
    """Upper-cases an M-Pesa transaction code and strips spaces/dashes, so 'qab1 cdefgh'
    and 'QAB1CDEFGH' are treated as the same code when checking for duplicates."""
    return re.sub(r'[\s\-]', '', (raw or '')).upper()


def mpesa_code_in_use(code, exclude_activation_id=None):
    """True if this M-Pesa transaction code has already been used by anyone — in any
    activation payment (pending, confirmed or rejected, automatic or manual) or any
    writing-task unlock payment. One M-Pesa payment can only ever be claimed once."""
    q = ActivationPayment.query.filter(db.func.upper(ActivationPayment.mpesa_code) == code)
    if exclude_activation_id:
        q = q.filter(ActivationPayment.id != exclude_activation_id)
    if q.first():
        return True
    return TaskUnlockPayment.query.filter(db.func.upper(TaskUnlockPayment.mpesa_code) == code).first() is not None


@app.route('/activate/manual', methods=['POST'])
@login_required
def activate_manual():
    """Fallback path if the STK push can't be initiated (e.g. PayHero not yet configured,
    or the API is temporarily unreachable) — user submits the code, admin confirms it."""
    user = current_user()
    if user.is_active_account:
        return redirect(url_for('dashboard'))

    mpesa_code = clean_mpesa_code(request.form.get('mpesa_code'))
    phone = request.form.get('phone', '').strip()
    if not mpesa_code:
        flash('Please enter your M-Pesa transaction code.', 'error')
        return redirect(url_for('activate'))

    if not MPESA_CODE_RE.match(mpesa_code):
        flash('That does not look like a valid M-Pesa code. It is 10 letters and numbers, '
              'e.g. QAB1CDEFGH — copy it from your M-Pesa confirmation SMS.', 'error')
        return redirect(url_for('activate'))

    if is_rate_limited(f'manual-code:{user.id}', max_attempts=10, window_seconds=3600):
        flash('Too many attempts. Please wait a while before trying again.', 'error')
        return redirect(url_for('activate'))

    if ActivationPayment.query.filter_by(user_id=user.id, status='pending').filter(
            ActivationPayment.mpesa_code.isnot(None)).first():
        flash('You already submitted a code — please wait for the admin to confirm it.', 'error')
        return redirect(url_for('activate'))

    if mpesa_code_in_use(mpesa_code):
        logger.warning('Duplicate manual M-Pesa code rejected: user_id=%s code=%s', user.id, mpesa_code)
        flash('This M-Pesa code has already been submitted. Each code can only be used once — '
              'check the code in your M-Pesa SMS, or contact support if you believe this is a mistake.', 'error')
        return redirect(url_for('activate'))

    payment = ActivationPayment(user_id=user.id, mpesa_code=mpesa_code, phone=phone,
                                 amount=ACTIVATION_FEE, status='pending')
    db.session.add(payment)
    db.session.commit()
    flash('Payment submitted! Your account will be activated once confirmed by admin.', 'success')
    return redirect(url_for('activate'))


def _finalize_activation_payment(checkout_request_id, success, result_desc, mpesa_receipt=None):
    """Looks up the ActivationPayment by CheckoutRequestID (set when the STK push was
    initiated) and confirms or fails it once PayHero's callback (or a status check)
    arrives. Safe to call more than once for the same payment."""
    payment = ActivationPayment.query.filter_by(checkout_request_id=checkout_request_id).first()
    if not payment or payment.status == 'confirmed':
        return
    # A payment that was marked failed (e.g. replaced by a retry) can still turn out to
    # have been paid — money received always wins. Anything else non-pending stays as is.
    if payment.status not in ('pending', 'failed') or (payment.status == 'failed' and not success):
        return

    payment.result_desc = result_desc
    payment.processed_at = now_utc()

    if success:
        # If the same receipt is already on another payment, keep the activation (money
        # genuinely arrived through this STK push) but don't store the clashing code.
        if mpesa_receipt and ActivationPayment.query.filter(
                db.func.upper(ActivationPayment.mpesa_code) == mpesa_receipt.upper(),
                ActivationPayment.id != payment.id).first():
            logger.warning('PayHero receipt %s already recorded on another payment (payment_id=%s)',
                           mpesa_receipt, payment.id)
            # A manual submission of this same code by someone else must not also be confirmable.
            ActivationPayment.query.filter(
                db.func.upper(ActivationPayment.mpesa_code) == mpesa_receipt.upper(),
                ActivationPayment.id != payment.id, ActivationPayment.status == 'pending'
            ).update({'status': 'rejected', 'result_desc': 'Duplicate M-Pesa code'},
                     synchronize_session=False)
        payment.mpesa_code = mpesa_receipt
        payment.status = 'confirmed'
        activate_user_and_pay_referral(payment.user)
    else:
        payment.status = 'failed'

    db.session.commit()


def _finalize_task_unlock_payment(checkout_request_id, success, result_desc, mpesa_receipt=None):
    """Looks up the pending TaskUnlockPayment by CheckoutRequestID and confirms or fails it
    once PayHero's callback arrives. On a confirmed payment, the task is assigned to the
    payer immediately — unless someone else's payment for the same task was confirmed first
    (two people paying for the same task at nearly the same moment), in which case the
    unlucky payer's unlock fee is credited straight to their wallet as an automatic refund
    rather than leaving them out of pocket for a task they didn't get."""
    payment = TaskUnlockPayment.query.filter_by(checkout_request_id=checkout_request_id).first()
    if not payment or payment.status != 'pending':
        return

    payment.result_desc = result_desc
    payment.processed_at = now_utc()

    if not success:
        payment.status = 'failed'
        db.session.commit()
        return

    payment.mpesa_code = mpesa_receipt
    task = payment.task

    if task.status == 'open' and task.applications_open():
        payment.status = 'confirmed'
        task.status = 'assigned'
        task.assigned_to_id = payment.user_id
        task.assigned_at = now_utc()
        task.deadline_at = now_utc() + timedelta(minutes=task.duration_minutes)
        notify_user_email(
            payment.user, 'You got the writing task!',
            f'Hi {payment.user.full_name},\n\n'
            f'Your payment of KES {TASK_UNLOCK_FEE} was confirmed and "{task.title}" is now '
            f'assigned to you. You have {task.duration_minutes} minutes to submit it — log in '
            f'to BizNova to get started.\n\n— BizNova'
        )
    else:
        # Someone else's payment for this same task was confirmed first — refund this
        # user's unlock fee straight to their wallet rather than leaving them out of pocket.
        payment.status = 'superseded'
        payment.user.wallet_balance += TASK_UNLOCK_FEE
        notify_user_email(
            payment.user, 'That writing task was already claimed',
            f'Hi {payment.user.full_name},\n\n'
            f'Sorry — "{task.title}" was claimed by another user moments before your payment '
            f'confirmed. Your KES {TASK_UNLOCK_FEE} has been credited back to your BizNova '
            f'wallet balance. Check the Writing Tasks page for other open tasks.\n\n— BizNova'
        )

    db.session.commit()


@app.route('/payhero/callback/<secret>', methods=['POST'])
def payhero_callback(secret):
    """PayHero calls this URL once the STK push completes (or fails/is cancelled).
    No login/session applies here — PayHero's servers are the caller — so the secret
    in the URL path is what proves the request is genuine instead."""
    if not secrets.compare_digest(secret, PAYHERO_CALLBACK_SECRET):
        logger.warning('Rejected PayHero callback with an invalid secret from %s', request.remote_addr)
        return jsonify({'status': 'rejected'}), 403

    data = request.get_json(silent=True) or {}
    payload = data.get('response', {})
    checkout_request_id = payload.get('CheckoutRequestID')
    result_code = payload.get('ResultCode')
    result_desc = payload.get('ResultDesc', '')
    mpesa_receipt = payload.get('MpesaReceiptNumber')
    success = str(result_code) == '0'

    # Sanity check: a "successful" callback for the wrong amount must not activate anyone.
    paid_amount = payload.get('Amount')
    if success and paid_amount is not None:
        try:
            expected = ACTIVATION_FEE if ActivationPayment.query.filter_by(
                checkout_request_id=checkout_request_id).first() else TASK_UNLOCK_FEE
            if float(paid_amount) + 0.001 < expected:
                logger.warning('PayHero callback amount %s below expected %s for %s',
                               paid_amount, expected, checkout_request_id)
                success = False
                result_desc = f'Amount paid ({paid_amount}) was less than required ({expected})'
        except (TypeError, ValueError):
            pass

    # A single callback URL serves every PayHero-initiated payment in the app (activation
    # fees and task-unlock fees so far) — look up which kind this CheckoutRequestID belongs
    # to and route it to the matching finalizer.
    if ActivationPayment.query.filter_by(checkout_request_id=checkout_request_id).first():
        _finalize_activation_payment(checkout_request_id, success, result_desc, mpesa_receipt)
    elif TaskUnlockPayment.query.filter_by(checkout_request_id=checkout_request_id).first():
        _finalize_task_unlock_payment(checkout_request_id, success, result_desc, mpesa_receipt)
    else:
        logger.warning('PayHero callback for unknown CheckoutRequestID=%s', checkout_request_id)

    return jsonify({'status': 'accepted'})




# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
def build_sparkline_points(values, width=280, height=80, pad=8):
    """Turn a list of numbers into an SVG polyline points string."""
    n = len(values)
    if n == 0:
        return ''
    max_val = max(values) or 1
    step = (width - 2 * pad) / (n - 1) if n > 1 else 0
    points = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = height - pad - ((v / max_val) * (height - 2 * pad))
        points.append(f'{x:.1f},{y:.1f}')
    return ' '.join(points)


def build_bar_chart(values, width=560, height=110, pad=8, gap_ratio=0.35):
    """Turn a list of numbers into SVG <rect> params for a real vertical bar chart.
    Returns a list of dicts: {x, y, width, height} ready to drop into an SVG template."""
    n = len(values)
    if n == 0:
        return []
    max_val = max(values) or 1
    usable_width = width - 2 * pad
    slot_width = usable_width / n
    bar_width = slot_width * (1 - gap_ratio)
    bars = []
    for i, v in enumerate(values):
        bar_height = (v / max_val) * (height - 2 * pad) if max_val else 0
        x = pad + i * slot_width + (slot_width - bar_width) / 2
        y = height - pad - bar_height
        bars.append({'x': round(x, 1), 'y': round(y, 1), 'width': round(bar_width, 1),
                     'height': round(max(bar_height, 1.5), 1)})
    return bars


def build_donut_segments(items, radius=54):
    """items: list of (label, value, color). Returns segments with stroke-dasharray/offset
    for an SVG circle-based donut chart, plus each item's percent of the total."""
    circumference = 2 * math.pi * radius
    total = sum(v for _, v, _ in items) or 1
    segments = []
    offset = 0.0
    for label, value, color in items:
        fraction = value / total
        length = fraction * circumference
        segments.append({
            'label': label, 'color': color, 'percent': round(fraction * 100, 1),
            'dasharray': f'{length:.2f} {circumference - length:.2f}',
            'dashoffset': round(-offset, 2),
        })
        offset += length
    return segments


def get_leaderboard(period, limit=10):
    """Returns top referrers ranked by number of ACTIVATED referrals within the period.
    period: 'week' | 'month' | 'all'. Each row: {user, count, period_label}."""
    now = now_utc()
    if period == 'week':
        start = datetime.combine(now.date() - timedelta(days=now.weekday()), datetime.min.time())
        period_label = f"Week of {start.strftime('%d %b %Y')}"
    elif period == 'month':
        start = datetime(now.year, now.month, 1)
        period_label = start.strftime('%B %Y')
    else:
        start = None
        period_label = 'All-Time'

    query = db.session.query(
        User.id, User.full_name, User.username, db.func.count(ReferralCommission.id).label('cnt')
    ).join(ReferralCommission, ReferralCommission.referrer_id == User.id)

    if start:
        query = query.filter(ReferralCommission.created_at >= start)

    rows = query.group_by(User.id).order_by(db.desc('cnt')).limit(limit).all()
    return [{'user_id': r[0], 'full_name': r[1], 'username': r[2], 'count': r[3]} for r in rows], period_label


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/support', methods=['GET', 'POST'])
@login_required
def support():
    user = current_user()
    if request.method == 'POST':
        subject = request.form.get('subject', '').strip()
        message = request.form.get('message', '').strip()
        if not subject or not message:
            flash('Please fill in both the subject and message.', 'error')
            return redirect(url_for('support'))

        msg = SupportMessage(user_id=user.id, subject=subject, message=message)
        db.session.add(msg)
        db.session.commit()

        notify_admin_email(
            f'Support message from @{user.username}',
            f'From: {user.full_name} (@{user.username}, {user.email})\n'
            f'Subject: {subject}\n\n{message}'
        )
        flash('Your message has been sent to the admin. We will get back to you soon.', 'success')
        return redirect(url_for('support'))

    my_messages = SupportMessage.query.filter_by(user_id=user.id).order_by(
        SupportMessage.submitted_at.desc()).all()
    return render_template('support.html', my_messages=my_messages, active_page='support')


def pick_gig_popup(user):
    """Returns the newest active gig that should pop up for this user right now (and records
    that it was shown), or None. Each gig appears at most GIG_POPUPS_PER_DAY times per day
    (days run on East Africa Time), spaced GIG_POPUP_MIN_GAP_HOURS apart, for GIG_POPUP_DAYS
    days after the admin added it."""
    now = now_utc()
    today = (now + timedelta(hours=3)).date()  # EAT calendar day
    candidates = Gig.query.filter(
        Gig.active == True, Gig.created_at >= now - timedelta(days=GIG_POPUP_DAYS)
    ).order_by(Gig.created_at.desc()).all()
    for gig in candidates:
        log = GigPopupLog.query.filter_by(user_id=user.id, gig_id=gig.id, day=today).first()
        if log is None:
            log = GigPopupLog(user_id=user.id, gig_id=gig.id, day=today, shown_count=0, last_shown_at=None)
        elif log.shown_count >= GIG_POPUPS_PER_DAY:
            continue
        elif log.last_shown_at and now - log.last_shown_at < timedelta(hours=GIG_POPUP_MIN_GAP_HOURS):
            continue
        log.shown_count += 1
        log.last_shown_at = now
        db.session.add(log)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()  # e.g. two tabs raced on the same row — just skip this time
            return None
        return gig
    return None


@app.route('/dashboard')
@login_required
def dashboard():
    user = current_user()
    referral_count = User.query.filter_by(referred_by_id=user.id).count()
    activated_referral_count = User.query.filter_by(referred_by_id=user.id, is_active_account=True).count()
    referral_earnings = db.session.query(db.func.sum(ReferralCommission.amount)).filter_by(
        referrer_id=user.id).scalar() or 0

    photo_earnings = db.session.query(db.func.sum(Photo.reward_amount)).join(
        PhotoLike, PhotoLike.photo_id == Photo.id).filter(PhotoLike.user_id == user.id).scalar() or 0
    video_earnings = db.session.query(db.func.sum(Video.reward_amount)).join(
        VideoView, VideoView.video_id == Video.id).filter(VideoView.user_id == user.id).scalar() or 0
    trivia_earnings = db.session.query(db.func.sum(TriviaQuestion.reward_amount)).join(
        TriviaAnswer, (TriviaAnswer.question_id == TriviaQuestion.id) & (TriviaAnswer.is_correct == True)
    ).filter(TriviaAnswer.user_id == user.id).scalar() or 0

    total_earned = referral_earnings + photo_earnings + video_earnings + trivia_earnings
    total_withdrawn = db.session.query(db.func.sum(Withdrawal.amount)).filter_by(
        user_id=user.id, status='approved').scalar() or 0

    # Last 7 days of earnings, for the sparkline chart. Grouped-by-day queries (one query
    # per reward type, each returning all 7 days at once) instead of looping day-by-day
    # and re-querying each time — that loop used to fire ~28 separate queries on every
    # single dashboard load; this does the same job in 4.
    today = now_utc().date()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    start_range = datetime.combine(days[0], datetime.min.time())
    earnings_by_day_map = {d: 0.0 for d in days}

    photo_rows = db.session.query(
        db.func.date(PhotoLike.liked_at), db.func.sum(Photo.reward_amount)
    ).join(Photo, PhotoLike.photo_id == Photo.id).filter(
        PhotoLike.user_id == user.id, PhotoLike.liked_at >= start_range
    ).group_by(db.func.date(PhotoLike.liked_at)).all()

    video_rows = db.session.query(
        db.func.date(VideoView.viewed_at), db.func.sum(Video.reward_amount)
    ).join(Video, VideoView.video_id == Video.id).filter(
        VideoView.user_id == user.id, VideoView.viewed_at >= start_range
    ).group_by(db.func.date(VideoView.viewed_at)).all()

    trivia_rows = db.session.query(
        db.func.date(TriviaAnswer.answered_at), db.func.sum(TriviaQuestion.reward_amount)
    ).join(TriviaQuestion, (TriviaAnswer.question_id == TriviaQuestion.id) & (TriviaAnswer.is_correct == True)
    ).filter(TriviaAnswer.user_id == user.id, TriviaAnswer.answered_at >= start_range
    ).group_by(db.func.date(TriviaAnswer.answered_at)).all()

    referral_earning_rows = db.session.query(
        db.func.date(ReferralCommission.created_at), db.func.sum(ReferralCommission.amount)
    ).filter(ReferralCommission.referrer_id == user.id, ReferralCommission.created_at >= start_range
    ).group_by(db.func.date(ReferralCommission.created_at)).all()

    def _merge_day_rows(rows):
        for day_value, amount in rows:
            # MySQL returns a date object already; be defensive in case a driver returns
            # a string instead, so this can't silently drop a day's earnings.
            day_key = day_value if hasattr(day_value, 'year') else datetime.strptime(str(day_value), '%Y-%m-%d').date()
            if day_key in earnings_by_day_map:
                earnings_by_day_map[day_key] += float(amount or 0)

    _merge_day_rows(photo_rows)
    _merge_day_rows(video_rows)
    _merge_day_rows(trivia_rows)
    _merge_day_rows(referral_earning_rows)
    earnings_by_day = [round(earnings_by_day_map[d], 2) for d in days]

    referral_count_rows = db.session.query(
        db.func.date(User.created_at), db.func.count(User.id)
    ).filter(User.referred_by_id == user.id, User.created_at >= start_range
    ).group_by(db.func.date(User.created_at)).all()
    referrals_by_day_map = {d: 0 for d in days}
    for day_value, count in referral_count_rows:
        day_key = day_value if hasattr(day_value, 'year') else datetime.strptime(str(day_value), '%Y-%m-%d').date()
        if day_key in referrals_by_day_map:
            referrals_by_day_map[day_key] = count
    referrals_by_day = [referrals_by_day_map[d] for d in days]

    day_labels = [d.strftime('%d %b') for d in days]
    earnings_points = build_sparkline_points(earnings_by_day)
    referrals_points = build_sparkline_points(referrals_by_day)

    photos_liked_today = PhotoLike.query.filter_by(user_id=user.id).count()
    videos_watched_today = VideoView.query.filter_by(user_id=user.id).count()
    trivia_answered_today = TriviaAnswer.query.filter_by(user_id=user.id).count()
    referral_link = url_for('register', ref=user.referral_code, _external=True)
    admin = Admin.query.first()
    withdrawal_source = (admin.withdrawal_source if admin and admin.withdrawal_source
                          else 'M-Pesa Till 5502617')

    # How many tasks are actually available to do RIGHT NOW in each category (i.e. still
    # open and not already completed by this user) — shown on the "Ways to Earn" tiles.
    cutoff = now_utc() - timedelta(hours=24)

    active_photo_ids = [p.id for p in Photo.query.filter(Photo.uploaded_at >= cutoff).with_entities(Photo.id).all()]
    liked_photo_ids = {pl.photo_id for pl in PhotoLike.query.filter_by(user_id=user.id).all()}
    available_photos = len([i for i in active_photo_ids if i not in liked_photo_ids])

    active_video_ids = [v.id for v in Video.query.filter(Video.uploaded_at >= cutoff).with_entities(Video.id).all()]
    watched_video_ids = {vv.video_id for vv in VideoView.query.filter_by(user_id=user.id).all()}
    available_videos = len([i for i in active_video_ids if i not in watched_video_ids])

    active_trivia_ids = [t.id for t in TriviaQuestion.query.filter(TriviaQuestion.uploaded_at >= cutoff).with_entities(TriviaQuestion.id).all()]
    answered_trivia_ids = {ta.question_id for ta in TriviaAnswer.query.filter_by(user_id=user.id).all()}
    available_trivia = len([i for i in active_trivia_ids if i not in answered_trivia_ids])

    active_survey_ids = [s.id for s in Survey.query.filter_by(active=True).with_entities(Survey.id).all()]
    completed_survey_ids = {r.survey_id for r in SurveyResponse.query.filter_by(user_id=user.id).all()}
    available_surveys = len([i for i in active_survey_ids if i not in completed_survey_ids])

    open_writing_tasks = WritingTask.query.filter_by(status='open').all()
    available_writing_tasks = len([t for t in open_writing_tasks if t.applications_open()])

    active_ad_ids = [a.id for a in ProductAd.query.filter(
        ProductAd.active == True, ProductAd.reward_amount > 0).with_entities(ProductAd.id).all()]
    claimed_ad_ids = {v.ad_id for v in ProductAdView.query.filter_by(user_id=user.id).all()}
    available_product_ads = len([i for i in active_ad_ids if i not in claimed_ad_ids])

    # New-gig popup — only for activated users (Gigs needs an active account), and not on
    # the same visit as the login announcement popup so two popups never stack.
    gig_popup = None
    if user.is_active_account and 'pending_notification' not in session:
        gig_popup = pick_gig_popup(user)

    # Fun Zone notice: tells a member they've been picked to bring today's topic, or that
    # today's chat is live.
    fun_zone_alert = None
    if user.is_active_account and is_fun_zone_day():
        fz_today = get_today_fun_zone_session()
        if fz_today and fz_today.status == 'awaiting_topic' and fz_today.host_user_id == user.id:
            fun_zone_alert = {'kind': 'host'}
        elif fun_zone_is_live(fz_today):
            fun_zone_alert = {'kind': 'live', 'topic': fz_today.topic}

    return render_template(
        'dashboard.html', user=user, referral_count=referral_count, fun_zone_alert=fun_zone_alert,
        activated_referral_count=activated_referral_count,
        referral_earnings=referral_earnings, total_earned=total_earned,
        total_withdrawn=total_withdrawn,
        earnings_by_day=earnings_by_day, referrals_by_day=referrals_by_day,
        earnings_points=earnings_points, referrals_points=referrals_points,
        day_labels=day_labels,
        photos_liked_today=photos_liked_today,
        videos_watched_today=videos_watched_today,
        trivia_answered_today=trivia_answered_today,
        available_photos=available_photos, available_videos=available_videos,
        available_trivia=available_trivia, available_surveys=available_surveys,
        available_writing_tasks=available_writing_tasks, available_product_ads=available_product_ads,
        active_page='dashboard', referral_bonus=REFERRAL_BONUS, level2_bonus=LEVEL2_REFERRAL_BONUS,
        referral_link=referral_link,
        withdrawal_source=withdrawal_source,
        gig_popup=gig_popup,
    )


@app.route('/profile', methods=['GET'])
@login_required
def profile():
    return render_template('profile.html', user=current_user(), active_page='profile',
                            gender_choices=GENDER_CHOICES, occupation_choices=OCCUPATION_CHOICES)


@app.route('/profile/update', methods=['POST'])
@login_required
def profile_update():
    user = current_user()
    full_name = request.form.get('full_name', '').strip()
    phone_digits = request.form.get('phone_digits', '').strip()

    if not full_name:
        flash('Name cannot be empty.', 'error')
        return redirect(url_for('profile'))

    if phone_digits:
        if not phone_digits.isdigit() or len(phone_digits) != 9:
            flash('Please enter a valid 9-digit number after 254 (e.g. 712345678).', 'error')
            return redirect(url_for('profile'))
        phone = '254' + phone_digits
        existing = User.query.filter(User.phone == phone, User.id != user.id).first()
        if existing:
            flash('That phone number is already registered to another account.', 'error')
            return redirect(url_for('profile'))
        user.phone = phone

    # Optional extras: description, gender, age, student/employee and a profile picture.
    bio = request.form.get('bio', '').strip()
    if len(bio) > 300:
        flash('Description is too long — please keep it under 300 characters.', 'error')
        return redirect(url_for('profile'))

    gender = request.form.get('gender', '').strip()
    if gender and gender not in {value for value, _ in GENDER_CHOICES}:
        flash('Please choose a gender from the list.', 'error')
        return redirect(url_for('profile'))

    occupation = request.form.get('occupation_status', '').strip()
    if occupation and occupation not in {value for value, _ in OCCUPATION_CHOICES}:
        flash('Please choose either Student or Employee.', 'error')
        return redirect(url_for('profile'))

    age_raw = request.form.get('age_years', '').strip()
    age_years = None
    if age_raw:
        if not age_raw.isdigit() or not (13 <= int(age_raw) <= 100):
            flash('Please enter a valid age in years (13–100).', 'error')
            return redirect(url_for('profile'))
        age_years = int(age_raw)

    new_photo = None
    photo_file = request.files.get('profile_photo')
    if photo_file and photo_file.filename:
        try:
            new_photo = save_avatar(photo_file)
        except ValueError as e:
            flash(str(e), 'error')
            return redirect(url_for('profile'))

    if new_photo:
        delete_avatar_file(user.profile_photo)
        user.profile_photo = new_photo
    elif request.form.get('remove_photo') and user.profile_photo:
        delete_avatar_file(user.profile_photo)
        user.profile_photo = None

    user.full_name = full_name
    user.bio = bio or None
    user.gender = gender or None
    user.occupation_status = occupation or None
    user.age_years = age_years
    db.session.commit()
    flash('Profile updated.', 'success')
    return redirect(url_for('profile'))


@app.route('/profile/change-password', methods=['POST'])
@login_required
def profile_change_password():
    user = current_user()
    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')

    if not check_password_hash(user.password_hash, current_password):
        flash('Current password is incorrect.', 'error')
    elif len(new_password) < 6:
        flash('New password must be at least 6 characters.', 'error')
    elif new_password != confirm_password:
        flash('New password and confirmation do not match.', 'error')
    else:
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        flash('Password updated successfully.', 'success')
    return redirect(url_for('profile'))


# ---------------------------------------------------------------------------
# PHOTOS (like to earn)
# ---------------------------------------------------------------------------
@app.route('/photos')
@login_required
@active_required
def photos():
    user = current_user()
    cutoff = now_utc() - timedelta(hours=24)
    active_photos = Photo.query.filter(Photo.uploaded_at >= cutoff).order_by(Photo.uploaded_at.desc()).all()
    liked_ids = {pl.photo_id for pl in PhotoLike.query.filter_by(user_id=user.id).all()}
    return render_template('photos.html', photos=active_photos, liked_ids=liked_ids, active_page='photos')


@app.route('/photos/<int:photo_id>/like', methods=['POST'])
@login_required
@active_required
def like_photo(photo_id):
    user = current_user()
    photo = Photo.query.get_or_404(photo_id)

    if photo.expired():
        return jsonify({'success': False, 'message': 'This photo has expired (older than 24 hours).'}), 400

    already = PhotoLike.query.filter_by(user_id=user.id, photo_id=photo.id).first()
    if already:
        return jsonify({'success': False, 'message': 'You already liked this photo.'}), 400

    like = PhotoLike(user_id=user.id, photo_id=photo.id)
    user.wallet_balance += photo.reward_amount
    db.session.add(like)
    db.session.commit()
    return jsonify({'success': True, 'message': f'You earned KES {photo.reward_amount:.0f}!',
                     'new_balance': user.wallet_balance})


# ---------------------------------------------------------------------------
# VIDEOS (watch to earn)
# ---------------------------------------------------------------------------
@app.route('/videos')
@login_required
@active_required
def videos():
    user = current_user()
    cutoff = now_utc() - timedelta(hours=24)
    active_videos = Video.query.filter(Video.uploaded_at >= cutoff).order_by(Video.uploaded_at.desc()).all()
    watched_ids = {vv.video_id for vv in VideoView.query.filter_by(user_id=user.id).all()}
    return render_template('videos.html', videos=active_videos, watched_ids=watched_ids, active_page='videos')


@app.route('/videos/<int:video_id>/watch', methods=['POST'])
@login_required
@active_required
def watch_video(video_id):
    user = current_user()
    video = Video.query.get_or_404(video_id)

    if video.expired():
        return jsonify({'success': False, 'message': 'This video has expired (older than 24 hours).'}), 400

    already = VideoView.query.filter_by(user_id=user.id, video_id=video.id).first()
    if already:
        return jsonify({'success': False, 'message': 'You already earned from this video.'}), 400

    view = VideoView(user_id=user.id, video_id=video.id)
    user.wallet_balance += video.reward_amount
    db.session.add(view)
    db.session.commit()
    return jsonify({'success': True, 'message': f'You earned KES {video.reward_amount:.0f}!',
                     'new_balance': user.wallet_balance})


# ---------------------------------------------------------------------------
# TRIVIA
# ---------------------------------------------------------------------------
@app.route('/trivia')
@login_required
@active_required
def trivia():
    user = current_user()
    cutoff = now_utc() - timedelta(hours=24)
    active_questions = TriviaQuestion.query.filter(TriviaQuestion.uploaded_at >= cutoff).order_by(
        TriviaQuestion.uploaded_at.desc()).all()
    answered = {ta.question_id: ta for ta in TriviaAnswer.query.filter_by(user_id=user.id).all()}
    return render_template('trivia.html', questions=active_questions, answered=answered, active_page='trivia')


@app.route('/trivia/<int:question_id>/answer', methods=['POST'])
@login_required
@active_required
def answer_trivia(question_id):
    user = current_user()
    question = TriviaQuestion.query.get_or_404(question_id)
    selected = request.form.get('option', '').lower()

    if question.expired():
        flash('This question has expired.', 'error')
        return redirect(url_for('trivia'))

    already = TriviaAnswer.query.filter_by(user_id=user.id, question_id=question.id).first()
    if already:
        flash('You already answered this question.', 'error')
        return redirect(url_for('trivia'))

    is_correct = (selected == question.correct_option)
    answer = TriviaAnswer(user_id=user.id, question_id=question.id, selected_option=selected,
                           is_correct=is_correct)
    if is_correct:
        user.wallet_balance += question.reward_amount
        flash(f'Correct! You earned KES {question.reward_amount:.0f}.', 'success')
    else:
        flash('Not quite right — better luck on the next question!', 'error')

    db.session.add(answer)
    db.session.commit()
    return redirect(url_for('trivia'))


# ---------------------------------------------------------------------------
# WRITING TASKS
# ---------------------------------------------------------------------------
@app.route('/writing-tasks')
@login_required
@active_required
def writing_tasks():
    user = current_user()
    open_tasks = WritingTask.query.filter_by(status='open').order_by(WritingTask.posted_at.desc()).all()
    open_tasks = [t for t in open_tasks if t.applications_open()]
    pending_unlocks = {p.task_id: p for p in TaskUnlockPayment.query.filter_by(
        user_id=user.id, status='pending').all()}

    my_tasks = WritingTask.query.filter_by(assigned_to_id=user.id).order_by(
        WritingTask.assigned_at.desc()).all()
    my_submissions = {s.task_id: s for s in TaskSubmission.query.filter_by(user_id=user.id).all()}

    return render_template('writing_tasks.html', open_tasks=open_tasks, pending_unlocks=pending_unlocks,
                            my_tasks=my_tasks, my_submissions=my_submissions, unlock_fee=TASK_UNLOCK_FEE,
                            usd_rate=USD_TO_KES_RATE, active_page='writing_tasks')


@app.route('/writing-tasks/<int:task_id>/unlock/initiate', methods=['POST'])
@login_required
@active_required
def unlock_writing_task_initiate(task_id):
    user = current_user()
    task = WritingTask.query.get_or_404(task_id)
    phone = request.form.get('phone', '').strip()

    if not phone:
        return jsonify({'success': False, 'message': 'Please enter a phone number.'}), 400
    if not task.applications_open():
        return jsonify({'success': False, 'message': 'This task is no longer available to claim.'}), 400

    # Clear out any stale pending unlock attempt for this user+task so they can retry.
    TaskUnlockPayment.query.filter_by(task_id=task.id, user_id=user.id, status='pending').delete()
    db.session.commit()

    try:
        result = payhero.stk_push(
            payhero_config, phone=phone, amount=TASK_UNLOCK_FEE,
            account_reference=f'{user.username}-task{task.id}',
            transaction_desc='BizNova Task Unlock', customer_name=user.username,
        )
    except payhero.PayHeroError as e:
        logger.error('PayHero STK push failed for task unlock, user_id=%s, task_id=%s: %s',
                     user.id, task.id, e)
        return jsonify({
            'success': False,
            'message': "We couldn't reach the payment provider right now. Please try again in "
                       "a moment.",
        }), 502

    payment = TaskUnlockPayment(
        task_id=task.id, user_id=user.id, amount=TASK_UNLOCK_FEE, phone=phone, status='pending',
        checkout_request_id=result.get('CheckoutRequestID'),
        merchant_request_id=result.get('MerchantRequestID'),
    )
    db.session.add(payment)
    db.session.commit()

    return jsonify({'success': True, 'message': 'STK push sent — check your phone and enter your M-Pesa PIN.',
                     'checkout_request_id': payment.checkout_request_id})


@app.route('/writing-tasks/unlock/status/<checkout_request_id>')
@login_required
def unlock_writing_task_status(checkout_request_id):
    user = current_user()
    payment = TaskUnlockPayment.query.filter_by(
        user_id=user.id, checkout_request_id=checkout_request_id).first()
    if not payment:
        return jsonify({'status': 'unknown'})
    return jsonify({'status': payment.status, 'result_desc': payment.result_desc,
                     'task_id': payment.task_id})


@app.route('/writing-tasks/<int:task_id>/submit', methods=['POST'])
@login_required
@active_required
def submit_writing_task(task_id):
    user = current_user()
    task = WritingTask.query.get_or_404(task_id)

    if task.assigned_to_id != user.id:
        flash('This task is not assigned to you.', 'error')
        return redirect(url_for('writing_tasks'))

    if task.is_past_deadline():
        flash('The deadline for this task has passed.', 'error')
        return redirect(url_for('writing_tasks'))

    existing = TaskSubmission.query.filter_by(task_id=task.id, user_id=user.id).first()
    if existing:
        flash('You already submitted this task.', 'error')
        return redirect(url_for('writing_tasks'))

    content = request.form.get('content', '').strip()
    word_count = len(content.split())
    if word_count < task.min_words:
        flash(f'Your submission has {word_count} words — minimum required is {task.min_words}.', 'error')
        return redirect(url_for('writing_tasks'))

    submission = TaskSubmission(task_id=task.id, user_id=user.id, content=content, word_count=word_count)
    task.status = 'submitted'
    db.session.add(submission)
    db.session.commit()
    flash('Submission sent for review! You will be credited once approved.', 'success')
    return redirect(url_for('writing_tasks'))


# ---------------------------------------------------------------------------
# PRODUCT ADVERTISEMENTS
# ---------------------------------------------------------------------------
@app.route('/product-ads')
@login_required
@active_required
def product_ads():
    user = current_user()
    ads = ProductAd.query.filter_by(active=True).order_by(ProductAd.created_at.desc()).all()
    claimed_ids = {v.ad_id for v in ProductAdView.query.filter_by(user_id=user.id).all()}
    return render_template('product_ads.html', ads=ads, claimed_ids=claimed_ids, active_page='product_ads')


@app.route('/product-ads/<int:ad_id>/claim', methods=['POST'])
@login_required
@active_required
def claim_product_ad(ad_id):
    user = current_user()
    ad = ProductAd.query.get_or_404(ad_id)

    if ad.reward_amount <= 0:
        return jsonify({'success': False, 'message': 'This ad has no reward.'}), 400

    already = ProductAdView.query.filter_by(user_id=user.id, ad_id=ad.id).first()
    if already:
        return jsonify({'success': False, 'message': 'You already earned from this ad.'}), 400

    view = ProductAdView(user_id=user.id, ad_id=ad.id)
    user.wallet_balance += ad.reward_amount
    db.session.add(view)
    db.session.commit()
    return jsonify({'success': True, 'message': f'You earned KES {ad.reward_amount:.0f}!',
                     'new_balance': user.wallet_balance})


@app.route('/sell-products')
@login_required
@active_required
def sell_products():
    listings = SellProduct.query.filter_by(active=True).order_by(SellProduct.created_at.desc()).all()
    return render_template('sell_products.html', listings=listings, active_page='sell_products')


# ---------------------------------------------------------------------------
# SURVEYS
# ---------------------------------------------------------------------------
@app.route('/surveys')
@login_required
@active_required
def surveys():
    user = current_user()
    completed_ids = {r.survey_id for r in SurveyResponse.query.filter_by(user_id=user.id).all()}
    active_surveys = Survey.query.filter_by(active=True).order_by(Survey.created_at.desc()).all()
    return render_template('surveys.html', surveys=active_surveys, completed_ids=completed_ids,
                            active_page='surveys')


@app.route('/surveys/<int:survey_id>')
@login_required
@active_required
def survey_detail(survey_id):
    user = current_user()
    survey = Survey.query.get_or_404(survey_id)
    already_done = SurveyResponse.query.filter_by(survey_id=survey.id, user_id=user.id).first()
    if already_done:
        flash('You already completed this survey.', 'error')
        return redirect(url_for('surveys'))
    return render_template('survey_detail.html', survey=survey, active_page='surveys')


@app.route('/surveys/<int:survey_id>/submit', methods=['POST'])
@login_required
@active_required
def submit_survey(survey_id):
    user = current_user()
    survey = Survey.query.get_or_404(survey_id)

    already_done = SurveyResponse.query.filter_by(survey_id=survey.id, user_id=user.id).first()
    if already_done:
        flash('You already completed this survey.', 'error')
        return redirect(url_for('surveys'))

    response = SurveyResponse(survey_id=survey.id, user_id=user.id)
    db.session.add(response)
    db.session.flush()  # get response.id before commit

    for question in survey.questions:
        answer_text = request.form.get(f'question_{question.id}', '').strip()
        db.session.add(SurveyAnswer(response_id=response.id, question_id=question.id,
                                     answer_text=answer_text))

    user.wallet_balance += survey.reward_amount
    db.session.commit()
    flash(f'Thanks! KES {survey.reward_amount:.0f} has been added to your wallet.', 'success')
    return redirect(url_for('surveys'))


# ---------------------------------------------------------------------------
# LEADERBOARD (read-only for users; privacy-friendly names)
# ---------------------------------------------------------------------------
@app.route('/leaderboard')
@login_required
@active_required
def leaderboard():
    admin = Admin.query.first()
    if admin and not admin.leaderboard_enabled:
        flash('The leaderboard is currently unavailable.', 'error')
        return redirect(url_for('dashboard'))

    user = current_user()
    period = request.args.get('period', 'week')
    if period not in ('week', 'month', 'all'):
        period = 'week'
    rows, period_label = get_leaderboard(period, limit=10)

    def display_name(full_name):
        parts = full_name.strip().split()
        if len(parts) >= 2:
            return f"{parts[0]} {parts[-1][0]}."
        return parts[0] if parts else 'User'

    for r in rows:
        r['display_name'] = display_name(r['full_name'])

    my_rank = None
    my_rows, _ = get_leaderboard(period, limit=10000)
    for i, r in enumerate(my_rows, start=1):
        if r['user_id'] == user.id:
            my_rank = {'rank': i, 'count': r['count']}
            break

    return render_template('leaderboard.html', rows=rows, period=period, period_label=period_label,
                            my_rank=my_rank, active_page='leaderboard', referral_bonus=REFERRAL_BONUS,
                            level2_bonus=LEVEL2_REFERRAL_BONUS)


# ---------------------------------------------------------------------------
# REFERRALS
# ---------------------------------------------------------------------------
@app.route('/referrals')
@login_required
def referrals():
    user = current_user()
    referred_users = User.query.filter_by(referred_by_id=user.id).order_by(User.created_at.desc()).all()
    commissions = ReferralCommission.query.filter_by(referrer_id=user.id).order_by(
        ReferralCommission.created_at.desc()).all()
    total_earned = sum(c.amount for c in commissions)
    level1_earned = sum(c.amount for c in commissions if c.level == 1)
    level2_earned = sum(c.amount for c in commissions if c.level == 2)
    referral_link = url_for('register', ref=user.referral_code, _external=True)
    share_text = f"Join BizNova and start earning online! Sign up with my link: {referral_link}"
    return render_template('referrals.html', user=user, referred_users=referred_users,
                            commissions=commissions, total_earned=total_earned,
                            level1_earned=level1_earned, level2_earned=level2_earned,
                            referral_link=referral_link, share_text=share_text,
                            bonus=REFERRAL_BONUS, level2_bonus=LEVEL2_REFERRAL_BONUS,
                            active_page='referrals')


# ---------------------------------------------------------------------------
# WALLET / WITHDRAWALS
# ---------------------------------------------------------------------------
@app.route('/wallet', methods=['GET', 'POST'])
@login_required
@active_required
def wallet():
    user = current_user()
    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount', 0))
        except ValueError:
            amount = 0
        mpesa_number = user.phone  # withdrawals always go to the phone number the account
                                    # was registered with — never a number typed in on this
                                    # form — so a compromised session can't redirect payouts
                                    # to a different number

        if amount < MIN_WITHDRAWAL:
            flash(f'Minimum withdrawal is KES {MIN_WITHDRAWAL}.', 'error')
        elif amount > user.wallet_balance:
            flash('Insufficient wallet balance.', 'error')
        else:
            fee = get_withdrawal_fee(amount)
            net_amount = amount - fee
            withdrawal = Withdrawal(user_id=user.id, amount=amount, fee=fee, net_amount=net_amount,
                                     mpesa_number=mpesa_number)
            user.wallet_balance -= amount
            db.session.add(withdrawal)
            db.session.commit()
            flash(f'Withdrawal request submitted! A transaction cost of KES {fee:.0f} applies — '
                  f'you will receive KES {net_amount:.0f} within 24-48 hours.', 'success')
            notify_user_email(
                user, 'Withdrawal request received — BizNova',
                f'Hi {user.full_name},\n\n'
                f'We received your withdrawal request for KES {amount:.0f}.\n'
                f'Transaction cost: KES {fee:.0f}\n'
                f'You will receive: KES {net_amount:.0f} to {mpesa_number}\n\n'
                f'This will be processed within 24-48 hours. You will get another email once it is sent.\n\n'
                f'— BizNova'
            )
        return redirect(url_for('wallet'))

    history = Withdrawal.query.filter_by(user_id=user.id).order_by(Withdrawal.requested_at.desc()).all()
    fee_tiers = WithdrawalFeeTier.query.order_by(WithdrawalFeeTier.min_amount).all()
    return render_template('wallet.html', user=user, history=history, min_withdrawal=MIN_WITHDRAWAL,
                            fee_tiers=fee_tiers, active_page='wallet')


# ---------------------------------------------------------------------------
# ADMIN AUTH
# ---------------------------------------------------------------------------
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        email = request.form.get('email', '').strip().lower()

        rate_key = f'admin-login:{request.remote_addr}'
        if is_rate_limited(rate_key, max_attempts=6, window_seconds=600):
            flash('Too many login attempts. Please wait a few minutes and try again.', 'error')
            return render_template('admin/login.html')

        admin = Admin.query.filter_by(username=username).first()
        valid_login = (
            admin
            and check_password_hash(admin.password_hash, password)
            and (not admin.email or email == admin.email.lower())
        )

        if valid_login:
            session['admin_id'] = admin.id
            return redirect(url_for('admin_dashboard'))

        flash('Invalid admin credentials.', 'error')
    return render_template('admin/login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    return redirect(url_for('admin_login'))


@app.route('/admin/add-value', methods=['GET', 'POST'])
@admin_required
def admin_add_value():
    if request.method == 'POST':
        amount_raw = request.form.get('amount', '').strip()
        note = request.form.get('note', '').strip()
        try:
            amount = float(amount_raw)
        except ValueError:
            amount = 0

        if amount <= 0:
            flash('Enter an amount greater than 0.', 'error')
        else:
            record_ledger_entry('deposit', amount, note or 'Manual deposit')
            db.session.commit()
            flash(f'KES {amount:.0f} added to Net Position.', 'success')
        return redirect(url_for('admin_add_value'))

    total_deposits = db.session.query(db.func.sum(LedgerEntry.amount)).filter_by(
        entry_type='deposit').scalar() or 0
    total_debits = db.session.query(db.func.sum(LedgerEntry.amount)).filter_by(
        entry_type='debit').scalar() or 0
    ledger_balance = total_deposits - total_debits
    entries = LedgerEntry.query.order_by(LedgerEntry.created_at.desc()).limit(100).all()
    return render_template('admin/add_value.html', entries=entries, ledger_balance=ledger_balance,
                            total_deposits=total_deposits, total_debits=total_debits)


@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    admin = Admin.query.get(session['admin_id'])

    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not check_password_hash(admin.password_hash, current_password):
            flash('Current password is incorrect.', 'error')
        elif len(new_password) < 6:
            flash('New password must be at least 6 characters.', 'error')
        elif new_password != confirm_password:
            flash('New password and confirmation do not match.', 'error')
        else:
            admin.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash('Password updated successfully.', 'success')
        return redirect(url_for('admin_settings'))

    return render_template('admin/settings.html', admin=admin)


@app.route('/admin/settings/update-email', methods=['POST'])
@admin_required
def admin_update_email():
    admin = Admin.query.get(session['admin_id'])
    new_email = request.form.get('email', '').strip().lower()
    admin.email = new_email or None
    db.session.commit()
    if new_email:
        flash('Admin email set. You will now need to enter it at login for extra protection.', 'success')
    else:
        flash('Admin email removed. Login no longer requires an email match.', 'success')
    return redirect(url_for('admin_settings'))


@app.route('/admin/settings/update-withdrawal-source', methods=['POST'])
@admin_required
def admin_update_withdrawal_source():
    admin = Admin.query.get(session['admin_id'])
    new_source = request.form.get('withdrawal_source', '').strip()
    admin.withdrawal_source = new_source or None
    db.session.commit()
    flash('Withdrawal source updated — users will see this on their dashboard.', 'success')
    return redirect(url_for('admin_settings'))


@app.route('/admin/settings/toggle-leaderboard', methods=['POST'])
@admin_required
def admin_toggle_leaderboard():
    admin = Admin.query.get(session['admin_id'])
    admin.leaderboard_enabled = not admin.leaderboard_enabled
    db.session.commit()
    flash('Leaderboard enabled for users.' if admin.leaderboard_enabled
          else 'Leaderboard disabled — hidden from users, no data was deleted.', 'success')
    return redirect(url_for('admin_settings'))


# ---------------------------------------------------------------------------
# ADMIN DASHBOARD
# ---------------------------------------------------------------------------
@app.route('/admin')
@admin_required
def admin_dashboard():
    total_users = User.query.count()
    active_users = User.query.filter_by(is_active_account=True).count()

    total_fees_collected = db.session.query(db.func.sum(ActivationPayment.amount)).filter_by(
        status='confirmed').scalar() or 0
    total_referral_paid = db.session.query(db.func.sum(ReferralCommission.amount)).scalar() or 0
    total_photo_paid = db.session.query(db.func.sum(Photo.reward_amount)).join(
        PhotoLike, PhotoLike.photo_id == Photo.id).scalar() or 0
    total_video_paid = db.session.query(db.func.sum(Video.reward_amount)).join(
        VideoView, VideoView.video_id == Video.id).scalar() or 0
    total_trivia_paid = db.session.query(db.func.sum(TriviaQuestion.reward_amount)).join(
        TriviaAnswer, (TriviaAnswer.question_id == TriviaQuestion.id) & (TriviaAnswer.is_correct == True)
    ).scalar() or 0
    total_task_paid_usd = db.session.query(db.func.sum(WritingTask.price_usd)).join(
        TaskSubmission, TaskSubmission.task_id == WritingTask.id
    ).filter(TaskSubmission.status == 'approved').scalar() or 0
    total_task_paid = total_task_paid_usd * USD_TO_KES_RATE
    total_survey_paid = db.session.query(db.func.sum(Survey.reward_amount)).join(
        SurveyResponse, SurveyResponse.survey_id == Survey.id).scalar() or 0
    total_funzone_paid = db.session.query(db.func.sum(FunZoneSession.prize_amount)).filter_by(
        prize_paid=True).scalar() or 0
    total_withdrawn = db.session.query(db.func.sum(Withdrawal.amount)).filter_by(
        status='approved').scalar() or 0
    total_transaction_fees = db.session.query(db.func.sum(Withdrawal.fee)).filter_by(
        status='approved').scalar() or 0

    total_paid_out = (total_referral_paid + total_photo_paid + total_video_paid + total_trivia_paid
                       + total_task_paid + total_survey_paid + total_funzone_paid)

    total_ledger_deposits = db.session.query(db.func.sum(LedgerEntry.amount)).filter_by(
        entry_type='deposit').scalar() or 0
    total_ledger_debits = db.session.query(db.func.sum(LedgerEntry.amount)).filter_by(
        entry_type='debit').scalar() or 0

    net_position = (total_fees_collected + total_transaction_fees + total_ledger_deposits) \
        - total_paid_out - total_ledger_debits

    pending_payments = ActivationPayment.query.filter_by(status='pending').count()
    pending_withdrawals = Withdrawal.query.filter_by(status='pending').count()
    pending_task_submissions = TaskSubmission.query.filter_by(status='pending').count()
    pending_advertise_requests = AdvertiseRequest.query.filter_by(status='new').count()
    pending_support_messages = SupportMessage.query.filter_by(status='new').count()
    pending_password_resets = PasswordResetRequest.query.filter_by(
        email_sent=False, resolved=False).count()

    # --- Signups trend (last 14 days) ---
    today = now_utc().date()
    days = [today - timedelta(days=i) for i in range(13, -1, -1)]
    signups_by_day = []
    fees_by_day = []
    for d in days:
        start = datetime.combine(d, datetime.min.time())
        end = start + timedelta(days=1)
        signups_by_day.append(User.query.filter(User.created_at >= start, User.created_at < end).count())
        fees_by_day.append(
            db.session.query(db.func.sum(ActivationPayment.amount)).filter(
                ActivationPayment.status == 'confirmed', ActivationPayment.processed_at >= start,
                ActivationPayment.processed_at < end).scalar() or 0
        )
    chart_labels = [d.strftime('%d %b') for d in days]
    signups_points = build_sparkline_points(signups_by_day, width=560, height=100)
    fees_bars = build_bar_chart(fees_by_day, width=560, height=100)

    # --- Payout breakdown, for the horizontal bar chart and the donut chart ---
    breakdown_raw = [
        ('Photos', total_photo_paid, 'camera', '#0ea5e9'),
        ('Videos', total_video_paid, 'video', '#7c3aed'),
        ('Trivia', total_trivia_paid, 'brain', '#f59e0b'),
        ('Writing Tasks', total_task_paid, 'edit', '#0891b2'),
        ('Surveys', total_survey_paid, 'clipboard', '#d97706'),
        ('Referrals', total_referral_paid, 'users', '#ec4899'),
        ('Fun Zone', total_funzone_paid, 'trophy', '#10b981'),
    ]
    max_breakdown = max((v for _, v, _, _ in breakdown_raw), default=0) or 1
    payout_breakdown = [
        {'label': label, 'amount': amount, 'icon': icon_name, 'color': color,
         'percent': round((amount / max_breakdown) * 100, 1)}
        for label, amount, icon_name, color in breakdown_raw
    ]
    donut_segments = build_donut_segments([(label, amount, color) for label, amount, _, color in breakdown_raw])

    # --- System health — simple config checks, not live monitoring, but useful at a
    # glance: is SMTP set up, is PayHero set up, is login OTP on. ---
    smtp_ready = mail_config.SMTP_USER not in ('', 'YOUR_BREVO_LOGIN_EMAIL') and \
        mail_config.SMTP_PASSWORD not in ('', 'YOUR_BREVO_SMTP_KEY')
    payhero_ready = payhero_config.API_USERNAME not in ('', 'YOUR_API_USERNAME') and \
        payhero_config.API_PASSWORD not in ('', 'YOUR_API_PASSWORD')
    system_health = [
        {'name': 'Database', 'ok': True, 'detail': 'Connected'},
        {'name': 'PayHero', 'ok': payhero_ready, 'detail': 'Configured' if payhero_ready else 'Using placeholder keys'},
        {'name': 'Email (SMTP)', 'ok': smtp_ready, 'detail': 'Configured' if smtp_ready else 'Using placeholder credentials'},
        {'name': 'Signup Email Verification', 'ok': smtp_ready, 'detail': 'Enabled' if smtp_ready else 'Needs SMTP configured'},
    ]

    # --- Alerts — things that need admin attention right now. ---
    alerts = []
    if pending_payments:
        alerts.append({'title': f'{pending_payments} payment{"s" if pending_payments != 1 else ""} awaiting confirmation',
                        'detail': 'Activation payments not yet auto-confirmed', 'icon': 'banknote', 'color': 'amber'})
    if pending_withdrawals:
        alerts.append({'title': f'{pending_withdrawals} withdrawal{"s" if pending_withdrawals != 1 else ""} pending approval',
                        'detail': 'Users waiting for their M-Pesa payout', 'icon': 'wallet', 'color': 'pink'})
    if pending_task_submissions:
        alerts.append({'title': f'{pending_task_submissions} writing task submission{"s" if pending_task_submissions != 1 else ""} to review',
                        'detail': 'Approve or reject to release payment', 'icon': 'inbox', 'color': 'blue'})
    if pending_advertise_requests:
        alerts.append({'title': f'{pending_advertise_requests} new ad request{"s" if pending_advertise_requests != 1 else ""}',
                        'detail': 'Advertisers waiting on a reply', 'icon': 'megaphone', 'color': 'purple'})
    if pending_support_messages:
        alerts.append({'title': f'{pending_support_messages} unread support message{"s" if pending_support_messages != 1 else ""}',
                        'detail': 'Users waiting on a reply', 'icon': 'headset', 'color': 'green'})
    if pending_password_resets:
        alerts.append({'title': f'{pending_password_resets} password reset email{"s" if pending_password_resets != 1 else ""} failed to send',
                        'detail': 'User may be locked out — share the temp password manually', 'icon': 'lock', 'color': 'pink'})
    if not smtp_ready:
        alerts.append({'title': 'Email is not fully configured', 'detail': 'SMTP still using placeholder credentials in mailer.py',
                        'icon': 'alert-triangle', 'color': 'amber'})
    if not payhero_ready:
        alerts.append({'title': 'PayHero is not fully configured', 'detail': 'API credentials still using placeholders in payhero.py',
                        'icon': 'alert-triangle', 'color': 'amber'})

    # --- Recent activity — latest payments and withdrawals, newest first. ---
    recent_payments = ActivationPayment.query.order_by(ActivationPayment.created_at.desc()).limit(5).all()
    recent_withdrawals = Withdrawal.query.order_by(Withdrawal.requested_at.desc()).limit(5).all()
    recent_activity = sorted(
        [{'time': p.created_at, 'type': 'Activation Payment', 'user': p.user.username if p.user else '—',
          'amount': p.amount, 'status': p.status} for p in recent_payments] +
        [{'time': w.requested_at, 'type': 'Withdrawal', 'user': w.user.username if w.user else '—',
          'amount': w.amount, 'status': w.status} for w in recent_withdrawals],
        key=lambda r: r['time'], reverse=True
    )[:8]

    # Plain JSON-serializable data for the Chart.js bar graphs and pie chart.
    charts_json = {
        'labels': chart_labels,
        'signups': signups_by_day,
        'fees': [round(v, 2) for v in fees_by_day],
        'breakdown_labels': [label for label, _, _, _ in breakdown_raw],
        'breakdown_values': [round(amount, 2) for _, amount, _, _ in breakdown_raw],
        'breakdown_colors': [color for _, _, _, color in breakdown_raw],
        'finance_labels': ['Activation Fees', 'Transaction Costs', 'Rewards + Referrals Paid', 'Withdrawn'],
        'finance_values': [round(total_fees_collected, 2), round(total_transaction_fees, 2),
                            round(total_paid_out, 2), round(total_withdrawn, 2)],
        'finance_colors': ['#10b981', '#10b981', '#f59e0b', '#3b82f6'],
        'users_labels': ['Total Registered', 'Active (Paid)', 'Pending Payments',
                          'Pending Withdrawals', 'Submissions to Review', 'New Ad Requests'],
        'users_values': [total_users, active_users, pending_payments, pending_withdrawals,
                          pending_task_submissions, pending_advertise_requests],
        'users_colors': ['#3b82f6', '#10b981', '#f59e0b', '#ec4899', '#60a5fa', '#a78bfa'],
    }

    return render_template(
        'admin/dashboard.html', total_users=total_users, active_users=active_users,
        total_fees_collected=total_fees_collected, total_paid_out=total_paid_out,
        total_withdrawn=total_withdrawn, net_position=net_position,
        total_transaction_fees=total_transaction_fees,
        pending_payments=pending_payments, pending_withdrawals=pending_withdrawals,
        pending_task_submissions=pending_task_submissions,
        pending_advertise_requests=pending_advertise_requests,
        pending_support_messages=pending_support_messages,
        pending_password_resets=pending_password_resets,
        activation_fee=ACTIVATION_FEE, referral_bonus=REFERRAL_BONUS, usd_rate=USD_TO_KES_RATE,
        chart_labels=chart_labels, signups_points=signups_points, fees_bars=fees_bars,
        signups_by_day=signups_by_day, fees_by_day=fees_by_day, payout_breakdown=payout_breakdown, donut_segments=donut_segments,
        charts_json=charts_json, system_health=system_health, alerts=alerts, recent_activity=recent_activity,
    )


def gather_report_stats():
    """Snapshot of platform stats used for the on-screen report preview and the PDF download.
    Kept separate from admin_dashboard() so tweaking the dashboard's charts can't accidentally
    break the report."""
    total_users = User.query.count()
    active_users = User.query.filter_by(is_active_account=True).count()

    total_fees_collected = db.session.query(db.func.sum(ActivationPayment.amount)).filter_by(
        status='confirmed').scalar() or 0
    total_referral_paid = db.session.query(db.func.sum(ReferralCommission.amount)).scalar() or 0
    total_photo_paid = db.session.query(db.func.sum(Photo.reward_amount)).join(
        PhotoLike, PhotoLike.photo_id == Photo.id).scalar() or 0
    total_video_paid = db.session.query(db.func.sum(Video.reward_amount)).join(
        VideoView, VideoView.video_id == Video.id).scalar() or 0
    total_trivia_paid = db.session.query(db.func.sum(TriviaQuestion.reward_amount)).join(
        TriviaAnswer, (TriviaAnswer.question_id == TriviaQuestion.id) & (TriviaAnswer.is_correct == True)
    ).scalar() or 0
    total_task_paid_usd = db.session.query(db.func.sum(WritingTask.price_usd)).join(
        TaskSubmission, TaskSubmission.task_id == WritingTask.id
    ).filter(TaskSubmission.status == 'approved').scalar() or 0
    total_task_paid = total_task_paid_usd * USD_TO_KES_RATE
    total_survey_paid = db.session.query(db.func.sum(Survey.reward_amount)).join(
        SurveyResponse, SurveyResponse.survey_id == Survey.id).scalar() or 0
    total_funzone_paid = db.session.query(db.func.sum(FunZoneSession.prize_amount)).filter_by(
        prize_paid=True).scalar() or 0
    total_withdrawn = db.session.query(db.func.sum(Withdrawal.amount)).filter_by(
        status='approved').scalar() or 0
    total_transaction_fees = db.session.query(db.func.sum(Withdrawal.fee)).filter_by(
        status='approved').scalar() or 0

    total_paid_out = (total_referral_paid + total_photo_paid + total_video_paid + total_trivia_paid
                       + total_task_paid + total_survey_paid + total_funzone_paid)

    total_ledger_deposits = db.session.query(db.func.sum(LedgerEntry.amount)).filter_by(
        entry_type='deposit').scalar() or 0
    total_ledger_debits = db.session.query(db.func.sum(LedgerEntry.amount)).filter_by(
        entry_type='debit').scalar() or 0

    net_position = (total_fees_collected + total_transaction_fees + total_ledger_deposits) \
        - total_paid_out - total_ledger_debits

    pending_payments = ActivationPayment.query.filter_by(status='pending').count()
    pending_withdrawals = Withdrawal.query.filter_by(status='pending').count()
    pending_task_submissions = TaskSubmission.query.filter_by(status='pending').count()
    pending_advertise_requests = AdvertiseRequest.query.filter_by(status='new').count()
    pending_support_messages = SupportMessage.query.filter_by(status='new').count()

    return {
        'generated_at': now_utc(),
        'total_users': total_users,
        'active_users': active_users,
        'total_fees_collected': total_fees_collected,
        'total_transaction_fees': total_transaction_fees,
        'total_withdrawn': total_withdrawn,
        'total_ledger_deposits': total_ledger_deposits,
        'total_ledger_debits': total_ledger_debits,
        'net_position': net_position,
        'total_paid_out': total_paid_out,
        'breakdown': [
            ('Photos', total_photo_paid),
            ('Videos', total_video_paid),
            ('Trivia', total_trivia_paid),
            ('Writing Tasks', total_task_paid),
            ('Surveys', total_survey_paid),
            ('Referrals', total_referral_paid),
            ('Fun Zone', total_funzone_paid),
        ],
        'pending_payments': pending_payments,
        'pending_withdrawals': pending_withdrawals,
        'pending_task_submissions': pending_task_submissions,
        'pending_advertise_requests': pending_advertise_requests,
        'pending_support_messages': pending_support_messages,
    }


@app.route('/admin/reports')
@admin_required
def admin_reports():
    stats = gather_report_stats()
    return render_template('admin/reports.html', stats=stats)


@app.route('/admin/reports/download')
@admin_required
def admin_reports_download():
    stats = gather_report_stats()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                             leftMargin=18 * mm, rightMargin=18 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('ReportTitle', parent=styles['Title'], fontSize=18,
                                  textColor=colors.HexColor('#111827'))
    sub_style = ParagraphStyle('ReportSub', parent=styles['Normal'], fontSize=9,
                                textColor=colors.HexColor('#6b7280'))
    section_style = ParagraphStyle('ReportSection', parent=styles['Heading2'], fontSize=13,
                                    spaceBefore=14, spaceAfter=6, textColor=colors.HexColor('#111827'))

    elements = []

    logo_path = os.path.join(BASE_DIR, 'static', 'img', 'logo.png')
    header_cells = []
    if os.path.exists(logo_path):
        header_cells.append(RLImage(logo_path, width=13 * mm, height=13 * mm))
    header_cells.append(Paragraph('BizNova — System Report', title_style))
    header_table = Table([header_cells], colWidths=[16 * mm, None] if len(header_cells) == 2 else None)
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(header_table)
    elements.append(Paragraph(
        f"Generated {stats['generated_at'].strftime('%d %B %Y, %H:%M UTC')}", sub_style))
    elements.append(Spacer(1, 10 * mm))

    def stat_table(rows):
        t = Table(rows, colWidths=[100 * mm, 60 * mm])
        t.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#374151')),
            ('TEXTCOLOR', (1, 0), (1, -1), colors.HexColor('#111827')),
            ('FONTNAME', (1, 0), (1, -1), 'Helvetica-Bold'),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#e5e7eb')),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        return t

    elements.append(Paragraph('Users', section_style))
    elements.append(stat_table([
        ['Total registered users', f"{stats['total_users']}"],
        ['Active (paid) users', f"{stats['active_users']}"],
    ]))

    elements.append(Paragraph('Money In', section_style))
    elements.append(stat_table([
        ['Activation fees collected', f"KES {stats['total_fees_collected']:,.0f}"],
        ['Withdrawal transaction fees', f"KES {stats['total_transaction_fees']:,.0f}"],
        ['Manually deposited (Add Value)', f"KES {stats['total_ledger_deposits']:,.0f}"],
    ]))

    elements.append(Paragraph('Money Out', section_style))
    breakdown_rows = [[label, f"KES {amount:,.0f}"] for label, amount in stats['breakdown']]
    breakdown_rows.append(['Total rewards & referrals paid', f"KES {stats['total_paid_out']:,.0f}"])
    breakdown_rows.append(['Withdrawn by users', f"KES {stats['total_withdrawn']:,.0f}"])
    breakdown_rows.append(['Committed at creation (e.g. Products)', f"KES {stats['total_ledger_debits']:,.0f}"])
    elements.append(stat_table(breakdown_rows))

    elements.append(Paragraph('Net Position', section_style))
    net_color = '#15803d' if stats['net_position'] >= 0 else '#b91c1c'
    net_style = ParagraphStyle('NetPosition', parent=styles['Normal'], fontSize=16,
                                textColor=colors.HexColor(net_color), spaceAfter=4)
    elements.append(Paragraph(f"KES {stats['net_position']:,.0f}", net_style))
    elements.append(Paragraph(
        'Fees + transaction costs + manual deposits, minus rewards paid out and amounts '
        'committed at creation.', sub_style))

    elements.append(Paragraph('Pending Items', section_style))
    elements.append(stat_table([
        ['Pending activation payments', f"{stats['pending_payments']}"],
        ['Pending withdrawals', f"{stats['pending_withdrawals']}"],
        ['Writing task submissions to review', f"{stats['pending_task_submissions']}"],
        ['New advertise requests', f"{stats['pending_advertise_requests']}"],
        ['New support messages', f"{stats['pending_support_messages']}"],
    ]))

    elements.append(Spacer(1, 10 * mm))
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'], fontSize=8,
                                   textColor=colors.HexColor('#9ca3af'), alignment=TA_CENTER)
    elements.append(Paragraph('BizNova — confidential admin report', footer_style))

    doc.build(elements)
    buffer.seek(0)
    filename = f"biznova-report-{stats['generated_at'].strftime('%Y%m%d-%H%M')}.pdf"
    return Response(buffer.read(), mimetype='application/pdf',
                     headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.route('/admin/payments')
@admin_required
def admin_payments():
    # Most activations confirm themselves automatically via the M-Pesa callback.
    # This page is for the fallback cases: manual code submissions, or STK
    # attempts that failed/timed out and need a human look.
    payments = ActivationPayment.query.filter(
        ActivationPayment.status.in_(['pending', 'failed'])
    ).order_by(ActivationPayment.created_at.desc()).all()
    return render_template('admin/payments.html', payments=payments)


@app.route('/admin/payments/<int:payment_id>/confirm', methods=['POST'])
@admin_required
def confirm_payment(payment_id):
    payment = ActivationPayment.query.get_or_404(payment_id)
    if payment.status == 'confirmed':
        flash('That payment was already confirmed.', 'error')
        return redirect(url_for('admin_payments'))

    # Safety net: never confirm a code that another payment already used. Catches the case
    # where two users submitted the same code before either was reviewed, or a manual code
    # that was later also received through the automatic M-Pesa callback.
    if payment.mpesa_code:
        code = payment.mpesa_code.strip().upper()
        clash = ActivationPayment.query.filter(
            db.func.upper(ActivationPayment.mpesa_code) == code,
            ActivationPayment.id != payment.id,
            ActivationPayment.status == 'confirmed').first()
        if clash is None:
            clash = TaskUnlockPayment.query.filter(
                db.func.upper(TaskUnlockPayment.mpesa_code) == code,
                TaskUnlockPayment.status == 'confirmed').first()
        if clash is not None:
            flash(f'Not confirmed: M-Pesa code {code} was already used for another payment. '
                  f'Reject this one if it is a duplicate.', 'error')
            return redirect(url_for('admin_payments'))

    payment.status = 'confirmed'
    payment.processed_at = now_utc()
    user = payment.user
    activate_user_and_pay_referral(user)
    db.session.commit()
    flash(f'Payment confirmed. {user.username} is now active.', 'success')
    return redirect(url_for('admin_payments'))


@app.route('/admin/payments/<int:payment_id>/reject', methods=['POST'])
@admin_required
def reject_payment(payment_id):
    payment = ActivationPayment.query.get_or_404(payment_id)
    payment.status = 'rejected'
    payment.processed_at = now_utc()
    db.session.commit()
    flash('Payment rejected.', 'success')
    return redirect(url_for('admin_payments'))


@app.route('/admin/withdrawals')
@admin_required
def admin_withdrawals():
    pending = Withdrawal.query.filter_by(status='pending').order_by(Withdrawal.requested_at.desc()).all()
    return render_template('admin/withdrawals.html', withdrawals=pending)


@app.route('/admin/withdrawal-fees', methods=['GET', 'POST'])
@admin_required
def admin_withdrawal_fees():
    if request.method == 'POST':
        try:
            min_amount = float(request.form.get('min_amount', 0))
            max_amount = float(request.form.get('max_amount', 0))
            fee = float(request.form.get('fee', 0))
        except ValueError:
            flash('Please enter valid numbers.', 'error')
            return redirect(url_for('admin_withdrawal_fees'))

        if max_amount <= min_amount:
            flash('The maximum amount must be greater than the minimum.', 'error')
            return redirect(url_for('admin_withdrawal_fees'))

        tier = WithdrawalFeeTier(min_amount=min_amount, max_amount=max_amount, fee=fee)
        db.session.add(tier)
        db.session.commit()
        flash(f'Fee tier added: KES {min_amount:.0f}–{max_amount:.0f} costs KES {fee:.0f}.', 'success')
        return redirect(url_for('admin_withdrawal_fees'))

    tiers = WithdrawalFeeTier.query.order_by(WithdrawalFeeTier.min_amount).all()
    return render_template('admin/withdrawal_fees.html', tiers=tiers)


@app.route('/admin/withdrawal-fees/<int:tier_id>/delete', methods=['POST'])
@admin_required
def delete_withdrawal_fee(tier_id):
    tier = WithdrawalFeeTier.query.get_or_404(tier_id)
    db.session.delete(tier)
    db.session.commit()
    flash('Fee tier removed.', 'success')
    return redirect(url_for('admin_withdrawal_fees'))


@app.route('/admin/withdrawals/<int:withdrawal_id>/approve', methods=['POST'])
@admin_required
def approve_withdrawal(withdrawal_id):
    withdrawal = Withdrawal.query.get_or_404(withdrawal_id)
    if withdrawal.status != 'pending':
        flash('This withdrawal was already processed — no changes made.', 'error')
        return redirect(url_for('admin_withdrawals'))

    withdrawal.status = 'approved'
    withdrawal.processed_at = now_utc()
    db.session.commit()
    flash('Withdrawal marked as approved — remember to send the M-Pesa payment manually.', 'success')
    notify_user_email(
        withdrawal.user, 'Your withdrawal has been sent — BizNova',
        f'Hi {withdrawal.user.full_name},\n\n'
        f'Your withdrawal of KES {withdrawal.net_amount or withdrawal.amount:.0f} has been sent to '
        f'{withdrawal.mpesa_number}. Please check your M-Pesa messages.\n\n— BizNova'
    )
    return redirect(url_for('admin_withdrawals'))


@app.route('/admin/withdrawals/<int:withdrawal_id>/reject', methods=['POST'])
@admin_required
def reject_withdrawal(withdrawal_id):
    withdrawal = Withdrawal.query.get_or_404(withdrawal_id)
    if withdrawal.status != 'pending':
        flash('This withdrawal was already processed — no changes made.', 'error')
        return redirect(url_for('admin_withdrawals'))

    withdrawal.status = 'rejected'
    withdrawal.processed_at = now_utc()
    withdrawal.user.wallet_balance += withdrawal.amount  # refund to wallet
    db.session.commit()
    flash('Withdrawal rejected and amount refunded to user wallet.', 'success')
    notify_user_email(
        withdrawal.user, 'Your withdrawal request was rejected — BizNova',
        f'Hi {withdrawal.user.full_name},\n\n'
        f'Your withdrawal request for KES {withdrawal.amount:.0f} was rejected, and the full '
        f'amount has been refunded to your BizNova wallet.\n\n— BizNova'
    )
    return redirect(url_for('admin_withdrawals'))


@app.route('/admin/password-resets')
@admin_required
def admin_password_resets():
    requests = PasswordResetRequest.query.order_by(
        PasswordResetRequest.resolved.asc(), PasswordResetRequest.created_at.desc()).all()
    return render_template('admin/password_resets.html', requests=requests)


@app.route('/admin/password-resets/<int:request_id>/resolve', methods=['POST'])
@admin_required
def admin_resolve_password_reset(request_id):
    reset_request = PasswordResetRequest.query.get_or_404(request_id)
    reset_request.resolved = True
    reset_request.resolved_at = now_utc()
    db.session.commit()
    flash('Marked as handled.', 'success')
    return redirect(url_for('admin_password_resets'))


@app.route('/admin/users')
@admin_required
def admin_users():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin/users.html', users=users)



@app.route('/admin/users/<int:user_id>/reset-password', methods=['POST'])
@admin_required
def admin_reset_user_password(user_id):
    """Generates a brand-new temporary password for a user and shows it to the admin once,
    so a locked-out user can be helped without the admin ever seeing their original password."""
    user = User.query.get_or_404(user_id)
    temp_password = secrets.token_urlsafe(6)
    user.password_hash = generate_password_hash(temp_password)
    db.session.commit()
    flash(f'New temporary password for {user.username}: {temp_password} — share this with them '
          f'directly (e.g. by phone). It will not be shown again.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/suspend', methods=['POST'])
@admin_required
def admin_suspend_user(user_id):
    """Suspends a user account (e.g. for suspected fraud) — blocks login immediately,
    including for anyone already logged in (see login_required)."""
    user = User.query.get_or_404(user_id)
    reason = request.form.get('reason', '').strip()
    user.is_suspended = True
    user.suspension_reason = reason or 'Suspended by admin.'
    db.session.commit()

    notify_user_email(
        user, 'Your BizNova account has been suspended',
        f'Hi {user.full_name},\n\n'
        f'Your BizNova account has been suspended and you will not be able to log in '
        f'until this is resolved.\n\n'
        f'{"Reason: " + reason if reason else ""}\n\n'
        f'If you believe this is a mistake, please contact us at biznova254@gmail.com.\n\n'
        f'— BizNova'
    )
    flash(f'{user.username} has been suspended.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/unsuspend', methods=['POST'])
@admin_required
def admin_unsuspend_user(user_id):
    user = User.query.get_or_404(user_id)
    user.is_suspended = False
    user.suspension_reason = None
    db.session.commit()

    notify_user_email(
        user, 'Your BizNova account has been reinstated',
        f'Hi {user.full_name},\n\n'
        f'Your BizNova account has been reinstated and you can now log in as usual.\n\n'
        f'— BizNova'
    )
    flash(f'{user.username} has been unsuspended.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/leaderboard')
@admin_required
def admin_leaderboard():
    period = request.args.get('period', 'week')
    if period not in ('week', 'month', 'all'):
        period = 'week'
    rows, period_label = get_leaderboard(period, limit=10)

    max_count = max((r['count'] for r in rows), default=0) or 1
    for r in rows:
        r['percent'] = round((r['count'] / max_count) * 100, 1)

    fixed_bonus = {'week': WEEKLY_LEADERBOARD_BONUS, 'month': MONTHLY_LEADERBOARD_BONUS}.get(period)

    already_paid_top = False
    if rows and fixed_bonus:
        already_paid_top = LeaderboardPayout.query.filter_by(
            user_id=rows[0]['user_id'], period_label=period_label).first() is not None

    recent_payouts = LeaderboardPayout.query.order_by(LeaderboardPayout.paid_at.desc()).limit(20).all()

    return render_template('admin/leaderboard.html', rows=rows, period=period, period_label=period_label,
                            recent_payouts=recent_payouts, fixed_bonus=fixed_bonus,
                            already_paid_top=already_paid_top,
                            weekly_bonus=WEEKLY_LEADERBOARD_BONUS, monthly_bonus=MONTHLY_LEADERBOARD_BONUS)


@app.route('/admin/leaderboard/pay/<int:user_id>', methods=['POST'])
@admin_required
def admin_pay_leaderboard_bonus(user_id):
    user = User.query.get_or_404(user_id)
    period_label = request.form.get('period_label', 'All-Time').strip()
    try:
        amount = float(request.form.get('amount', 0))
    except ValueError:
        amount = 0

    if amount <= 0:
        flash('Please enter a valid bonus amount.', 'error')
        return redirect(url_for('admin_leaderboard'))

    user.wallet_balance += amount
    payout = LeaderboardPayout(user_id=user.id, amount=amount, period_label=period_label)
    db.session.add(payout)
    db.session.commit()
    flash(f'Paid KES {amount:.0f} bonus to {user.username} for {period_label}.', 'success')
    return redirect(url_for('admin_leaderboard'))


# ---- Content uploads: photos ----
@app.route('/admin/photos', methods=['GET', 'POST'])
@admin_required
def admin_photos():
    if request.method == 'POST':
        file = request.files.get('photo')
        caption = request.form.get('caption', '').strip()
        reward = request.form.get('reward_amount', PHOTO_LIKE_REWARD)
        try:
            reward = float(reward)
        except ValueError:
            reward = PHOTO_LIKE_REWARD

        if not file or file.filename == '':
            flash('Please choose an image file.', 'error')
        elif not allowed_file(file.filename):
            flash('Invalid file type. Use png, jpg, jpeg, gif, or webp.', 'error')
        else:
            filename = secure_filename(f"{secrets.token_hex(6)}_{file.filename}")
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            photo = Photo(filename=filename, caption=caption, reward_amount=reward)
            db.session.add(photo)
            db.session.commit()
            flash('Photo uploaded! It will be available to users for 24 hours.', 'success')
        return redirect(url_for('admin_photos'))

    cutoff = now_utc() - timedelta(hours=24)
    active_photos = Photo.query.filter(Photo.uploaded_at >= cutoff).order_by(Photo.uploaded_at.desc()).all()
    return render_template('admin/upload_photo.html', photos=active_photos, default_reward=PHOTO_LIKE_REWARD)


# ---- Content uploads: videos ----
@app.route('/admin/videos', methods=['GET', 'POST'])
@admin_required
def admin_videos():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        url_ = request.form.get('url', '').strip()
        reward = request.form.get('reward_amount', VIDEO_WATCH_REWARD)
        min_watch_minutes = request.form.get('min_watch_minutes', 5)
        video_file = request.files.get('video')

        try:
            reward = float(reward)
        except ValueError:
            reward = VIDEO_WATCH_REWARD
        try:
            min_watch_seconds = max(1, int(float(min_watch_minutes) * 60))
        except ValueError:
            min_watch_seconds = 300

        video_filename = None
        if video_file and video_file.filename:
            if allowed_video_file(video_file.filename):
                video_filename = secure_filename(f"{secrets.token_hex(6)}_{video_file.filename}")
                os.makedirs(DAILY_VIDEO_FOLDER, exist_ok=True)
                video_file.save(os.path.join(DAILY_VIDEO_FOLDER, video_filename))
            else:
                flash('Invalid video type. Use mp4, webm, mov, or ogg.', 'error')
                return redirect(url_for('admin_videos'))

        if not title or (not url_ and not video_filename):
            flash('Please provide a title, and either upload a video or paste a video URL.', 'error')
        else:
            video = Video(title=title, url=url_ or None, video_filename=video_filename,
                          reward_amount=reward, min_watch_seconds=min_watch_seconds)
            db.session.add(video)
            db.session.commit()
            flash('Video added! It will be available to users for 24 hours.', 'success')
        return redirect(url_for('admin_videos'))

    cutoff = now_utc() - timedelta(hours=24)
    active_videos = Video.query.filter(Video.uploaded_at >= cutoff).order_by(Video.uploaded_at.desc()).all()
    return render_template('admin/upload_video.html', videos=active_videos, default_reward=VIDEO_WATCH_REWARD)


# ---- Content uploads: trivia ----
@app.route('/admin/trivia', methods=['GET', 'POST'])
@admin_required
def admin_trivia():
    if request.method == 'POST':
        question_text = request.form.get('question_text', '').strip()
        option_a = request.form.get('option_a', '').strip()
        option_b = request.form.get('option_b', '').strip()
        option_c = request.form.get('option_c', '').strip()
        option_d = request.form.get('option_d', '').strip()
        correct_option = request.form.get('correct_option', '').strip().lower()
        reward = request.form.get('reward_amount', TRIVIA_CORRECT_REWARD)
        try:
            reward = float(reward)
        except ValueError:
            reward = TRIVIA_CORRECT_REWARD

        if not all([question_text, option_a, option_b, option_c, option_d, correct_option]):
            flash('Please fill in the question, all four options, and the correct option.', 'error')
        elif correct_option not in ('a', 'b', 'c', 'd'):
            flash('Correct option must be a, b, c, or d.', 'error')
        else:
            question = TriviaQuestion(
                question_text=question_text, option_a=option_a, option_b=option_b,
                option_c=option_c, option_d=option_d, correct_option=correct_option,
                reward_amount=reward,
            )
            db.session.add(question)
            db.session.commit()
            flash('Trivia question added! It will be available to users for 24 hours.', 'success')
        return redirect(url_for('admin_trivia'))

    cutoff = now_utc() - timedelta(hours=24)
    active_questions = TriviaQuestion.query.filter(TriviaQuestion.uploaded_at >= cutoff).order_by(
        TriviaQuestion.uploaded_at.desc()).all()
    return render_template('admin/upload_trivia.html', questions=active_questions,
                            default_reward=TRIVIA_CORRECT_REWARD)


# ---------------------------------------------------------------------------
# ADMIN: WRITING TASKS
# ---------------------------------------------------------------------------
@app.route('/admin/tasks', methods=['GET', 'POST'])
@admin_required
def admin_tasks():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        instructions = request.form.get('instructions', '').strip()
        min_words = request.form.get('min_words', 300)
        price_usd = request.form.get('price_usd', 0)
        application_window_hours = request.form.get('application_window_hours', DEFAULT_APPLICATION_WINDOW_HOURS)
        duration_value = request.form.get('duration_value', 2)
        duration_unit = request.form.get('duration_unit', 'hours')

        try:
            min_words = int(min_words)
            price_usd = float(price_usd)
            application_window_hours = float(application_window_hours)
            duration_value = int(duration_value)
        except ValueError:
            flash('Please enter valid numbers for words, price, window, and duration.', 'error')
            return redirect(url_for('admin_tasks'))

        duration_minutes = duration_value * (60 if duration_unit == 'hours' else 1)

        if not title or not instructions:
            flash('Please provide a title and instructions.', 'error')
        else:
            task = WritingTask(
                title=title, instructions=instructions, min_words=min_words, price_usd=price_usd,
                application_window_hours=application_window_hours, duration_minutes=duration_minutes,
            )
            db.session.add(task)
            db.session.commit()
            flash('Writing task posted! Users can apply within the application window.', 'success')
        return redirect(url_for('admin_tasks'))

    tasks = WritingTask.query.order_by(WritingTask.posted_at.desc()).all()
    unlock_count_rows = db.session.query(
        TaskUnlockPayment.task_id, db.func.count(TaskUnlockPayment.id)
    ).group_by(TaskUnlockPayment.task_id).all()
    unlock_counts_map = dict(unlock_count_rows)
    unlock_attempt_counts = {t.id: unlock_counts_map.get(t.id, 0) for t in tasks}
    return render_template('admin/tasks.html', tasks=tasks, unlock_attempt_counts=unlock_attempt_counts,
                            usd_rate=USD_TO_KES_RATE,
                            default_window=DEFAULT_APPLICATION_WINDOW_HOURS)


@app.route('/admin/tasks/<int:task_id>/edit', methods=['POST'])
@admin_required
def admin_edit_task(task_id):
    task = WritingTask.query.get_or_404(task_id)
    if task.status != 'open':
        flash('You can only edit a task while it is still open for applications.', 'error')
        return redirect(url_for('admin_tasks'))

    try:
        task.price_usd = float(request.form.get('price_usd', task.price_usd))
        task.min_words = int(request.form.get('min_words', task.min_words))
        task.application_window_hours = float(
            request.form.get('application_window_hours', task.application_window_hours))
    except ValueError:
        flash('Please enter valid numbers.', 'error')
        return redirect(url_for('admin_tasks'))

    db.session.commit()
    flash('Task updated.', 'success')
    return redirect(url_for('admin_tasks'))


@app.route('/admin/tasks/submissions')
@admin_required
def admin_task_submissions():
    pending = TaskSubmission.query.filter_by(status='pending').order_by(
        TaskSubmission.submitted_at.desc()).all()
    return render_template('admin/task_submissions.html', submissions=pending)


@app.route('/admin/tasks/submissions/<int:submission_id>/approve', methods=['POST'])
@admin_required
def approve_task_submission(submission_id):
    submission = TaskSubmission.query.get_or_404(submission_id)
    if submission.status != 'pending':
        flash('This submission was already reviewed — no changes made.', 'error')
        return redirect(url_for('admin_task_submissions'))

    task = submission.task
    submission.status = 'approved'
    submission.reviewed_at = now_utc()
    task.status = 'approved'
    submission.user.wallet_balance += task.price_kes()
    db.session.commit()
    flash(f'Submission approved. {submission.user.username} earned KES {task.price_kes():.2f}.', 'success')
    return redirect(url_for('admin_task_submissions'))


@app.route('/admin/tasks/submissions/<int:submission_id>/reject', methods=['POST'])
@admin_required
def reject_task_submission(submission_id):
    submission = TaskSubmission.query.get_or_404(submission_id)
    if submission.status != 'pending':
        flash('This submission was already reviewed — no changes made.', 'error')
        return redirect(url_for('admin_task_submissions'))

    submission.status = 'rejected'
    submission.reviewed_at = now_utc()
    submission.admin_note = request.form.get('note', '').strip()
    submission.task.status = 'rejected'
    db.session.commit()
    flash('Submission rejected.', 'success')
    return redirect(url_for('admin_task_submissions'))


# ---------------------------------------------------------------------------
# ADMIN: PRODUCT ADVERTISEMENTS
# ---------------------------------------------------------------------------
@app.route('/admin/products', methods=['GET', 'POST'])
@admin_required
def admin_products():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        link_url = request.form.get('link_url', '').strip()
        video_url = request.form.get('video_url', '').strip()
        image_file = request.files.get('image')
        video_file = request.files.get('video')
        reward_raw = request.form.get('reward_amount', '0').strip()
        try:
            reward_amount = float(reward_raw) if reward_raw else 0.0
        except ValueError:
            reward_amount = 0.0

        if not title:
            flash('Please provide a title.', 'error')
            return redirect(url_for('admin_products'))

        image_filename = None
        if image_file and image_file.filename:
            if allowed_file(image_file.filename):
                image_filename = secure_filename(f"{secrets.token_hex(6)}_{image_file.filename}")
                os.makedirs(AD_IMAGE_FOLDER, exist_ok=True)
                image_file.save(os.path.join(AD_IMAGE_FOLDER, image_filename))
            else:
                flash('Invalid image type. Use png, jpg, jpeg, gif, or webp.', 'error')
                return redirect(url_for('admin_products'))

        video_filename = None
        if video_file and video_file.filename:
            if allowed_video_file(video_file.filename):
                video_filename = secure_filename(f"{secrets.token_hex(6)}_{video_file.filename}")
                os.makedirs(AD_VIDEO_FOLDER, exist_ok=True)
                video_file.save(os.path.join(AD_VIDEO_FOLDER, video_filename))
            else:
                flash('Invalid video type. Use mp4, webm, mov, or ogg.', 'error')
                return redirect(url_for('admin_products'))

        ad = ProductAd(title=title, description=description, link_url=link_url or None,
                        image_filename=image_filename, video_filename=video_filename,
                        video_url=video_url or None, reward_amount=reward_amount)
        db.session.add(ad)
        if reward_amount > 0:
            record_ledger_entry('debit', reward_amount, f'Product reward committed: {title}')
        db.session.commit()

        if reward_amount > 0:
            notif_message = f'New product added: {title}. Earn KES {reward_amount:.0f} by checking it out!'
        else:
            notif_message = f'New product added: {title}. Check it out!'
        notif = Notification(message=notif_message, link_url=url_for('product_ads'), product_ad_id=ad.id)
        db.session.add(notif)
        db.session.commit()

        flash('Product ad added! Users will be notified next time they log in.', 'success')
        return redirect(url_for('admin_products'))

    ads = ProductAd.query.order_by(ProductAd.created_at.desc()).all()
    return render_template('admin/products.html', ads=ads)


@app.route('/admin/products/<int:ad_id>/toggle', methods=['POST'])
@admin_required
def toggle_product_ad(ad_id):
    ad = ProductAd.query.get_or_404(ad_id)
    ad.active = not ad.active
    db.session.commit()
    return redirect(url_for('admin_products'))


# ---------------------------------------------------------------------------
# ADMIN: SELL PRODUCTS (marketplace / classifieds listings)
# ---------------------------------------------------------------------------
@app.route('/admin/sell-products', methods=['GET', 'POST'])
@admin_required
def admin_sell_products():
    if request.method == 'POST':
        name = request.form.get('name', '').strip() or None
        description = request.form.get('description', '').strip() or None
        owner_name = request.form.get('owner_name', '').strip() or None
        owner_contact = request.form.get('owner_contact', '').strip() or None
        location = request.form.get('location', '').strip() or None
        cost_raw = request.form.get('cost', '').strip()

        cost = None
        if cost_raw:
            try:
                cost = float(cost_raw)
            except ValueError:
                cost = None

        image_file = request.files.get('image')
        image_filename = None
        if image_file and image_file.filename:
            if allowed_file(image_file.filename):
                image_filename = secure_filename(f"{secrets.token_hex(6)}_{image_file.filename}")
                os.makedirs(SELL_PRODUCT_IMAGE_FOLDER, exist_ok=True)
                image_file.save(os.path.join(SELL_PRODUCT_IMAGE_FOLDER, image_filename))
            else:
                flash('Invalid image type. Use png, jpg, jpeg, gif, or webp.', 'error')
                return redirect(url_for('admin_sell_products'))

        listing = SellProduct(name=name, image_filename=image_filename, cost=cost,
                               description=description, owner_name=owner_name,
                               owner_contact=owner_contact, location=location)
        db.session.add(listing)
        db.session.commit()
        flash('Listing added! It will now appear on the Sell Products page for users.', 'success')
        return redirect(url_for('admin_sell_products'))

    listings = SellProduct.query.order_by(SellProduct.created_at.desc()).all()
    return render_template('admin/sell_products.html', listings=listings)


@app.route('/admin/sell-products/<int:listing_id>/toggle', methods=['POST'])
@admin_required
def toggle_sell_product(listing_id):
    listing = SellProduct.query.get_or_404(listing_id)
    listing.active = not listing.active
    db.session.commit()
    return redirect(url_for('admin_sell_products'))


@app.route('/admin/sell-products/<int:listing_id>/delete', methods=['POST'])
@admin_required
def delete_sell_product(listing_id):
    listing = SellProduct.query.get_or_404(listing_id)
    db.session.delete(listing)
    db.session.commit()
    flash('Listing deleted.', 'success')
    return redirect(url_for('admin_sell_products'))


# ---------------------------------------------------------------------------
# GIGS (free, admin-managed)
# ---------------------------------------------------------------------------
@app.route('/gigs')
@login_required
@active_required
def gigs():
    items = Gig.query.filter_by(active=True).order_by(Gig.created_at.desc()).all()
    return render_template('gigs.html', gigs=items, active_page='gigs')


@app.route('/gigs/<int:gig_id>')
@login_required
@active_required
def gig_detail(gig_id):
    gig = Gig.query.filter_by(id=gig_id, active=True).first_or_404()
    return render_template('gig_detail.html', gig=gig, active_page='gigs')


@app.route('/admin/gigs', methods=['GET', 'POST'])
@admin_required
def admin_gigs():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip() or None
        link_url = request.form.get('link_url', '').strip() or None
        whatsapp_raw = request.form.get('whatsapp_number', '').strip()

        if not title:
            flash('Please give the gig a title.', 'error')
            return redirect(url_for('admin_gigs'))
        if link_url and not re.match(r'^https?://', link_url, re.I):
            link_url = 'https://' + link_url
        whatsapp = None
        if whatsapp_raw:
            whatsapp = normalize_whatsapp(whatsapp_raw)
            if not whatsapp:
                flash('That WhatsApp number does not look valid. Include the country code, e.g. 254712345678.', 'error')
                return redirect(url_for('admin_gigs'))

        image_file = request.files.get('image')
        image_filename = None
        if image_file and image_file.filename:
            if not allowed_file(image_file.filename):
                flash('Invalid image type. Use png, jpg, jpeg, gif, or webp.', 'error')
                return redirect(url_for('admin_gigs'))
            image_filename = secure_filename(f"{secrets.token_hex(6)}_{image_file.filename}")
            os.makedirs(GIG_IMAGE_FOLDER, exist_ok=True)
            image_file.save(os.path.join(GIG_IMAGE_FOLDER, image_filename))

        db.session.add(Gig(title=title, description=description, link_url=link_url,
                           whatsapp_number=whatsapp, image_filename=image_filename))
        db.session.commit()
        flash('Gig added! It now shows on the Gigs page for users.', 'success')
        return redirect(url_for('admin_gigs'))

    items = Gig.query.order_by(Gig.created_at.desc()).all()
    return render_template('admin/gigs.html', gigs=items)


@app.route('/admin/gigs/<int:gig_id>/toggle', methods=['POST'])
@admin_required
def toggle_gig(gig_id):
    gig = Gig.query.get_or_404(gig_id)
    gig.active = not gig.active
    db.session.commit()
    return redirect(url_for('admin_gigs'))


@app.route('/admin/gigs/<int:gig_id>/delete', methods=['POST'])
@admin_required
def delete_gig(gig_id):
    gig = Gig.query.get_or_404(gig_id)
    GigPopupLog.query.filter_by(gig_id=gig.id).delete()
    db.session.delete(gig)
    db.session.commit()
    flash('Gig deleted.', 'success')
    return redirect(url_for('admin_gigs'))


@app.route('/admin/advertise-requests')
@admin_required
def admin_advertise_requests():
    requests_list = AdvertiseRequest.query.order_by(AdvertiseRequest.submitted_at.desc()).all()
    return render_template('admin/advertise_requests.html', requests_list=requests_list)


@app.route('/admin/advertise-requests/<int:request_id>/status', methods=['POST'])
@admin_required
def update_advertise_request_status(request_id):
    req = AdvertiseRequest.query.get_or_404(request_id)
    new_status = request.form.get('status', 'new')
    if new_status in ('new', 'contacted', 'closed'):
        req.status = new_status
        db.session.commit()
    return redirect(url_for('admin_advertise_requests'))


@app.route('/admin/support-messages')
@admin_required
def admin_support_messages():
    messages = SupportMessage.query.order_by(SupportMessage.submitted_at.desc()).all()
    return render_template('admin/support_messages.html', messages=messages)


@app.route('/admin/support-messages/<int:message_id>/status', methods=['POST'])
@admin_required
def update_support_message_status(message_id):
    msg = SupportMessage.query.get_or_404(message_id)
    new_status = request.form.get('status', 'new')
    if new_status in ('new', 'read', 'replied'):
        msg.status = new_status
        db.session.commit()
    return redirect(url_for('admin_support_messages'))


# ---------------------------------------------------------------------------
# ADMIN: SURVEYS
# ---------------------------------------------------------------------------
@app.route('/admin/surveys', methods=['GET', 'POST'])
@admin_required
def admin_surveys():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        reward_amount = request.form.get('reward_amount', 10)
        try:
            reward_amount = float(reward_amount)
        except ValueError:
            reward_amount = 10

        if not title:
            flash('Please provide a survey title.', 'error')
        else:
            survey = Survey(title=title, description=description, reward_amount=reward_amount)
            db.session.add(survey)
            db.session.commit()
            flash('Survey created! Now add questions to it below.', 'success')
        return redirect(url_for('admin_surveys'))

    surveys_list = Survey.query.order_by(Survey.created_at.desc()).all()
    response_counts = {s.id: SurveyResponse.query.filter_by(survey_id=s.id).count() for s in surveys_list}
    return render_template('admin/surveys.html', surveys=surveys_list, response_counts=response_counts)


@app.route('/admin/surveys/<int:survey_id>/questions', methods=['POST'])
@admin_required
def admin_add_survey_question(survey_id):
    survey = Survey.query.get_or_404(survey_id)
    question_text = request.form.get('question_text', '').strip()
    if not question_text:
        flash('Please enter a question.', 'error')
    else:
        db.session.add(SurveyQuestion(survey_id=survey.id, question_text=question_text))
        db.session.commit()
        flash('Question added.', 'success')
    return redirect(url_for('admin_surveys'))


@app.route('/admin/surveys/<int:survey_id>/toggle', methods=['POST'])
@admin_required
def toggle_survey(survey_id):
    survey = Survey.query.get_or_404(survey_id)
    survey.active = not survey.active
    db.session.commit()
    return redirect(url_for('admin_surveys'))


# ---------------------------------------------------------------------------
# FUN ZONE — weekend community chat (Fri / Sat / Sun) with a daily KES prize
# ---------------------------------------------------------------------------
@app.route('/fun-zone')
@login_required
@active_required
def fun_zone():
    user = current_user()
    today_is_fz_day = is_fun_zone_day()
    fz = get_today_fun_zone_session() if today_is_fz_day else None

    if not today_is_fz_day:
        state = 'closed_day'
    elif not fz:
        state = 'not_opened'
    elif fz.status == 'awaiting_topic':
        state = 'awaiting_topic'
    elif fz.status == 'open':
        state = 'open'
    else:
        state = 'ended'

    messages = []
    last_id = 0
    if fz and fz.status in ('open', 'closed'):
        recent = FunZoneMessage.query.filter_by(session_id=fz.id).order_by(
            FunZoneMessage.id.desc()).limit(100).all()
        messages = [serialize_fun_zone_message(m, user.id) for m in reversed(recent)]
        last_id = messages[-1]['id'] if messages else 0

    last_winner_session = FunZoneSession.query.filter(
        FunZoneSession.winner_id.isnot(None), FunZoneSession.prize_paid == True
    ).order_by(FunZoneSession.session_date.desc()).first()

    history = get_fun_zone_history()

    return render_template(
        'fun_zone.html', user=user, fz=fz, state=state, messages=messages, last_id=last_id,
        history=history,
        is_host=bool(fz and fz.host_user_id == user.id and fz.status == 'awaiting_topic'),
        last_winner_session=last_winner_session, next_day_label=next_fun_zone_day_label(),
        max_len=FUN_ZONE_MAX_MESSAGE_LEN, prize=FUN_ZONE_PRIZE, active_page='fun_zone')


FUN_ZONE_HISTORY_HOURS = 24      # members can look back this far; the admin sees everything


def get_fun_zone_history():
    """Past Fun Zone sessions (not today's) that were active in the last 24 hours — this
    is all members get to see of the history. The admin sees every session, with its
    date, topic and day, at /admin/fun-zone."""
    cutoff = now_utc() - timedelta(hours=FUN_ZONE_HISTORY_HOURS)
    rows = FunZoneSession.query.filter(
        FunZoneSession.session_date < fun_zone_today(), FunZoneSession.topic.isnot(None)
    ).order_by(FunZoneSession.session_date.desc()).limit(10).all()
    history = []
    for s in rows:
        last_activity = s.closed_at or s.opened_at or s.created_at
        if not last_activity or last_activity < cutoff:
            continue
        history.append({
            'id': s.id,
            'day': s.session_date.strftime('%A'),
            'date': s.session_date.strftime('%d %b %Y'),
            'topic': s.topic,
            'winner': mask_username(s.winner.username) if s.winner else None,
            'messages': FunZoneMessage.query.filter_by(session_id=s.id).count(),
        })
    return history


@app.route('/fun-zone/history/<int:session_id>')
@login_required
@active_required
def fun_zone_history_messages(session_id):
    """Read-only conversation of a past session — only while it is inside the 24-hour window."""
    user = current_user()
    allowed = {h['id'] for h in get_fun_zone_history()}
    if session_id not in allowed:
        return jsonify({'success': False, 'message': 'This conversation is older than 24 hours and is no longer available.'}), 404
    msgs = FunZoneMessage.query.filter_by(session_id=session_id).order_by(
        FunZoneMessage.id.asc()).limit(300).all()
    return jsonify({'success': True, 'messages': [serialize_fun_zone_message(m, user.id) for m in msgs]})


@app.route('/fun-zone/messages')
@login_required
@active_required
def fun_zone_messages():
    """Polled by the chat page every few seconds for new messages."""
    user = current_user()
    fz = get_today_fun_zone_session() if is_fun_zone_day() else None
    if not fz:
        return jsonify({'status': 'none', 'live': False, 'messages': []})

    after = request.args.get('after', 0, type=int) or 0
    payload = {'status': fz.status, 'live': fun_zone_is_live(fz), 'topic': fz.topic, 'messages': []}
    if fz.status in ('open', 'closed'):
        new_msgs = FunZoneMessage.query.filter(
            FunZoneMessage.session_id == fz.id, FunZoneMessage.id > after
        ).order_by(FunZoneMessage.id.asc()).limit(200).all()
        payload['messages'] = [serialize_fun_zone_message(m, user.id) for m in new_msgs]
    return jsonify(payload)


@app.route('/fun-zone/send', methods=['POST'])
@login_required
@active_required
def fun_zone_send():
    user = current_user()
    fz = get_today_fun_zone_session() if is_fun_zone_day() else None
    if not fun_zone_is_live(fz):
        return jsonify({'success': False, 'message': 'The Fun Zone chat is not open right now.'}), 403

    body = (request.form.get('body') or '').strip()
    if not body:
        return jsonify({'success': False, 'message': 'Type a message first.'}), 400
    if len(body) > FUN_ZONE_MAX_MESSAGE_LEN:
        return jsonify({'success': False,
                        'message': f'Messages can be up to {FUN_ZONE_MAX_MESSAGE_LEN} characters.'}), 400
    if is_rate_limited(f'fun_zone_send:{user.id}', FUN_ZONE_MESSAGES_PER_WINDOW, FUN_ZONE_RATE_WINDOW_SECONDS):
        return jsonify({'success': False, 'message': 'You are sending messages too quickly — slow down a little.'}), 429

    msg = FunZoneMessage(session_id=fz.id, user_id=user.id, body=body)
    db.session.add(msg)
    db.session.commit()
    return jsonify({'success': True, 'message': serialize_fun_zone_message(msg, user.id)})


@app.route('/fun-zone/topic', methods=['POST'])
@login_required
@active_required
def fun_zone_submit_topic():
    """The member the system selected submits today's topic, which opens the chat."""
    user = current_user()
    fz = get_today_fun_zone_session() if is_fun_zone_day() else None
    if not fz or fz.status != 'awaiting_topic' or fz.host_user_id != user.id:
        flash('There is no topic request waiting for you right now.', 'error')
        return redirect(url_for('fun_zone'))

    topic = (request.form.get('topic') or '').strip()
    if len(topic) < 5:
        flash('Please write a topic of at least 5 characters.', 'error')
        return redirect(url_for('fun_zone'))
    if len(topic) > 300:
        flash('Please keep the topic under 300 characters.', 'error')
        return redirect(url_for('fun_zone'))

    fz.topic = topic
    fz.topic_source = 'member'
    fz.status = 'open'
    fz.opened_at = now_utc()
    db.session.commit()
    flash('Thank you! Your topic is live and the chat is now open.', 'success')
    return redirect(url_for('fun_zone'))


# ---- Admin side ----
@app.route('/admin/fun-zone')
@admin_required
def admin_fun_zone():
    today = fun_zone_today()
    today_session = FunZoneSession.query.filter_by(session_date=today).first()
    sessions = FunZoneSession.query.order_by(FunZoneSession.session_date.desc()).all()

    counts = {}
    for session_id, user_id in db.session.query(FunZoneMessage.session_id, FunZoneMessage.user_id).all():
        entry = counts.setdefault(session_id, {'messages': 0, 'users': set()})
        entry['messages'] += 1
        entry['users'].add(user_id)
    stats = {sid: {'messages': v['messages'], 'participants': len(v['users'])} for sid, v in counts.items()}

    total_prizes = db.session.query(db.func.sum(FunZoneSession.prize_amount)).filter_by(
        prize_paid=True).scalar() or 0

    return render_template(
        'admin/fun_zone.html', today=today, today_session=today_session, sessions=sessions, stats=stats,
        is_fz_day=is_fun_zone_day(today), today_name=today.strftime('%A'), prize=FUN_ZONE_PRIZE,
        total_prizes=total_prizes, active_member_count=User.query.filter_by(
            is_active_account=True, is_suspended=False).count())


@app.route('/admin/fun-zone/open', methods=['POST'])
@admin_required
def admin_fun_zone_open():
    today = fun_zone_today()
    if not is_fun_zone_day(today):
        flash('The Fun Zone can only be opened on Friday, Saturday or Sunday.', 'error')
        return redirect(url_for('admin_fun_zone'))
    if FunZoneSession.query.filter_by(session_date=today).first():
        flash("Today's Fun Zone session already exists.", 'error')
        return redirect(url_for('admin_fun_zone'))

    mode = request.form.get('mode', 'admin')
    if mode == 'system':
        host = pick_fun_zone_host()
        if not host:
            flash('No active members are available to select right now.', 'error')
            return redirect(url_for('admin_fun_zone'))
        fz = FunZoneSession(session_date=today, status='awaiting_topic', host_user_id=host.id,
                            prize_amount=FUN_ZONE_PRIZE)
        db.session.add(fz)
        db.session.commit()
        notify_user_email(
            host, 'You were selected to set today\'s Fun Zone topic',
            f'Hi {host.full_name},\n\nYou have been selected to bring today\'s Fun Zone topic on BizNova. '
            f'Log in, open your dashboard and tap the Fun Zone notice to submit it. Once your topic is live, '
            f'members can chat and the day\'s winner receives KES {FUN_ZONE_PRIZE:.0f}.\n\n— BizNova')
        flash(f'The system selected @{host.username}. They have been notified on their dashboard '
              f'and can now submit the topic.', 'success')
    else:
        topic = (request.form.get('topic') or '').strip()
        if len(topic) < 5 or len(topic) > 300:
            flash('Please enter a topic between 5 and 300 characters.', 'error')
            return redirect(url_for('admin_fun_zone'))
        fz = FunZoneSession(session_date=today, topic=topic, topic_source='admin', status='open',
                            opened_at=now_utc(), prize_amount=FUN_ZONE_PRIZE)
        db.session.add(fz)
        db.session.commit()
        flash("Today's Fun Zone is open. Members can chat now.", 'success')
    return redirect(url_for('admin_fun_zone_session', session_id=fz.id))


@app.route('/admin/fun-zone/<int:session_id>')
@admin_required
def admin_fun_zone_session(session_id):
    fz = FunZoneSession.query.get_or_404(session_id)
    messages = FunZoneMessage.query.filter_by(session_id=fz.id).order_by(FunZoneMessage.id.asc()).all()

    message_counts = {}
    for m in messages:
        message_counts[m.user_id] = message_counts.get(m.user_id, 0) + 1
    participants = []
    if message_counts:
        users = User.query.filter(User.id.in_(list(message_counts.keys()))).all()
        participants = sorted(users, key=lambda u: -message_counts[u.id])

    return render_template(
        'admin/fun_zone_session.html', fz=fz, messages=messages, participants=participants,
        message_counts=message_counts, prize=FUN_ZONE_PRIZE,
        can_change_topic=fz.status != 'closed',
        eat_offset=timedelta(hours=FUN_ZONE_UTC_OFFSET_HOURS))


@app.route('/admin/fun-zone/<int:session_id>/set-topic', methods=['POST'])
@admin_required
def admin_fun_zone_set_topic(session_id):
    fz = FunZoneSession.query.get_or_404(session_id)
    if fz.status == 'closed':
        flash('This session is closed.', 'error')
        return redirect(url_for('admin_fun_zone_session', session_id=fz.id))
    topic = (request.form.get('topic') or '').strip()
    if len(topic) < 5 or len(topic) > 300:
        flash('Please enter a topic between 5 and 300 characters.', 'error')
        return redirect(url_for('admin_fun_zone_session', session_id=fz.id))
    fz.topic = topic
    if fz.status == 'awaiting_topic':
        fz.topic_source = 'admin'
        fz.status = 'open'
        fz.opened_at = now_utc()
    db.session.commit()
    flash('Topic saved — the chat is open.' if fz.status == 'open' else 'Topic saved.', 'success')
    return redirect(url_for('admin_fun_zone_session', session_id=fz.id))


@app.route('/admin/fun-zone/<int:session_id>/repick', methods=['POST'])
@admin_required
def admin_fun_zone_repick(session_id):
    fz = FunZoneSession.query.get_or_404(session_id)
    if fz.status != 'awaiting_topic':
        flash('A new member can only be selected while the session is waiting for its topic.', 'error')
        return redirect(url_for('admin_fun_zone_session', session_id=fz.id))
    host = pick_fun_zone_host(exclude_ids=[fz.host_user_id] if fz.host_user_id else [])
    if not host:
        flash('No other active members are available to select.', 'error')
        return redirect(url_for('admin_fun_zone_session', session_id=fz.id))
    fz.host_user_id = host.id
    db.session.commit()
    notify_user_email(
        host, 'You were selected to set today\'s Fun Zone topic',
        f'Hi {host.full_name},\n\nYou have been selected to bring today\'s Fun Zone topic on BizNova. '
        f'Log in, open your dashboard and tap the Fun Zone notice to submit it.\n\n— BizNova')
    flash(f'Selected @{host.username} instead. They have been notified on their dashboard.', 'success')
    return redirect(url_for('admin_fun_zone_session', session_id=fz.id))


@app.route('/admin/fun-zone/<int:session_id>/finish', methods=['POST'])
@admin_required
def admin_fun_zone_finish(session_id):
    """Closes the chat and, if a winner is chosen, credits the prize to their wallet (once)."""
    fz = FunZoneSession.query.get_or_404(session_id)
    winner_raw = request.form.get('winner_user_id', '').strip()

    winner = None
    if winner_raw:
        if not winner_raw.isdigit():
            flash('Please choose a winner from the list.', 'error')
            return redirect(url_for('admin_fun_zone_session', session_id=fz.id))
        winner = User.query.get(int(winner_raw))
        took_part = winner and FunZoneMessage.query.filter_by(session_id=fz.id, user_id=winner.id).first()
        if not winner or not took_part:
            flash('The winner must be a member who took part in this chat.', 'error')
            return redirect(url_for('admin_fun_zone_session', session_id=fz.id))
        if fz.prize_paid:
            flash('The prize for this session has already been awarded.', 'error')
            return redirect(url_for('admin_fun_zone_session', session_id=fz.id))

    if fz.status != 'closed':
        fz.status = 'closed'
        fz.closed_at = now_utc()

    if winner:
        winner.wallet_balance += fz.prize_amount or FUN_ZONE_PRIZE
        fz.winner_id = winner.id
        fz.prize_paid = True
        db.session.commit()
        notify_user_email(
            winner, 'You won today\'s BizNova Fun Zone!',
            f'Hi {winner.full_name},\n\nCongratulations — you were chosen as the winner of today\'s Fun Zone '
            f'("{fz.topic}"). KES {fz.prize_amount:.0f} has been added to your wallet.\n\n— BizNova')
        flash(f'Chat closed. KES {fz.prize_amount:.0f} credited to @{winner.username}.', 'success')
    else:
        db.session.commit()
        flash('Chat closed without a winner.', 'success')
    return redirect(url_for('admin_fun_zone_session', session_id=fz.id))


@app.route('/admin/fun-zone/messages/<int:message_id>/delete', methods=['POST'])
@admin_required
def admin_fun_zone_delete_message(message_id):
    msg = FunZoneMessage.query.get_or_404(message_id)
    session_id = msg.session_id
    db.session.delete(msg)
    db.session.commit()
    flash('Message removed.', 'success')
    return redirect(url_for('admin_fun_zone_session', session_id=session_id))


# ---------------------------------------------------------------------------
# CLI / SETUP HELPER
# ---------------------------------------------------------------------------
@app.cli.command('init-db')
def init_db():
    """Create all tables and a default admin account (username: admin, password: admin123)."""
    db.create_all()
    if not Admin.query.filter_by(username='admin').first():
        admin = Admin(username='admin', password_hash=generate_password_hash('admin123'),
                       withdrawal_source='M-Pesa Till 5502617')
        db.session.add(admin)
        db.session.commit()
        print('Default admin created -> username: admin | password: admin123 (CHANGE THIS)')
    else:
        print('Database already initialized.')


def ensure_schema_migrations():
    """db.create_all() only creates tables that don't exist yet — it never adds new
    columns OR new indexes to a table that's already there. So whenever a new column
    is added to an EXISTING model (as opposed to a brand new one), or an existing
    column gains index=True after the table was already created, sites that were
    already deployed before that change would otherwise either crash (missing column)
    or silently keep running slower than they should (missing index) — with no
    obvious error to point at the cause.

    This runs on every startup, checks for both gaps, and fixes them automatically —
    so upgrading the app to a newer zip never requires anyone to manually run SQL by
    hand. Add a line here any time a column (or its index) is added to a table that
    may already exist on a live site."""
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())

    def add_column_if_missing(table, column, ddl_type):
        if table not in existing_tables:
            return  # brand new table — db.create_all() already handles this case
        existing_cols = {c['name'] for c in inspector.get_columns(table)}
        if column not in existing_cols:
            with db.engine.connect() as conn:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl_type}'))
                conn.commit()
            logger.info('Auto-migration: added missing column %s.%s', table, column)

    def add_index_if_missing(table, column):
        if table not in existing_tables:
            return  # brand new table — db.create_all() already creates its indexes too
        existing_cols = {c['name'] for c in inspector.get_columns(table)}
        if column not in existing_cols:
            return  # column itself doesn't exist yet on this database — nothing to index
        indexed_cols = set()
        for idx in inspector.get_indexes(table):
            indexed_cols.update(idx['column_names'])
        if column not in indexed_cols:
            index_name = f'ix_{table}_{column}'
            with db.engine.connect() as conn:
                conn.execute(text(f'ALTER TABLE {table} ADD INDEX {index_name} ({column})'))
                conn.commit()
            logger.info('Auto-migration: added missing index on %s.%s', table, column)

    add_column_if_missing('referral_commission', 'level', 'INTEGER DEFAULT 1')

    # Profile details added alongside the Fun Zone.
    add_column_if_missing('user', 'profile_photo', 'VARCHAR(255)')
    add_column_if_missing('user', 'bio', 'VARCHAR(300)')
    add_column_if_missing('user', 'gender', 'VARCHAR(20)')
    add_column_if_missing('user', 'age_years', 'INTEGER')
    add_column_if_missing('user', 'occupation_status', 'VARCHAR(20)')

    for table, column in [
        ('activation_payment', 'status'),
        ('withdrawal', 'status'),
        ('writing_task', 'status'),
        ('task_unlock_payment', 'status'),
        ('task_submission', 'status'),
        ('support_message', 'status'),
        ('advertise_request', 'status'),
        ('ledger_entry', 'entry_type'),
        ('password_reset_request', 'email_sent'),
        ('password_reset_request', 'resolved'),
        ('fun_zone_session', 'status'),
    ]:
        add_index_if_missing(table, column)


def ensure_db():
    """Fallback initializer so the app works even without running flask init-db."""
    with app.app_context():
        db.create_all()
        ensure_schema_migrations()
        if not Admin.query.filter_by(username='admin').first():
            admin = Admin(username='admin', password_hash=generate_password_hash('admin123'),
                           withdrawal_source='M-Pesa Till 5502617')
            db.session.add(admin)
            db.session.commit()


if __name__ == '__main__':
    ensure_db()
    # Debug mode shows full Python tracebacks (source code, config, secret key) to
    # whoever triggers an error — never enable it on a live site. Set FLASK_DEBUG=1
    # in your local .env only while developing.
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug_mode)
else:
    ensure_db()
