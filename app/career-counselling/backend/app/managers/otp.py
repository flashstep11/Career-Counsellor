import random
import hashlib
import secrets
from datetime import datetime, timedelta
import httpx
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig
from app.core.database import get_database
from app.config import settings

OTP_TTL_MINUTES = 5
VERIFICATION_TOKEN_TTL_MINUTES = 10


# FastAPI-Mail configuration for email OTP delivery
mail_conf = ConnectionConfig(
    MAIL_USERNAME=settings.MAIL_USERNAME,
    MAIL_PASSWORD=settings.MAIL_PASSWORD,
    MAIL_FROM=settings.MAIL_FROM,
    MAIL_PORT=settings.MAIL_PORT,
    MAIL_SERVER=settings.MAIL_SERVER,
    MAIL_STARTTLS=settings.MAIL_STARTTLS,
    MAIL_SSL_TLS=settings.MAIL_SSL_TLS,
    USE_CREDENTIALS=True,
    VALIDATE_CERTS=True,
)


async def _send_otp_email_via_brevo_api(email: str, otp: str) -> None:
    if not settings.BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY is not configured")

    sender_email = (settings.MAIL_FROM or "").strip()
    if not sender_email:
        raise RuntimeError("MAIL_FROM must be set when using Brevo API")

    sender_name = (settings.MAIL_FROM_NAME or "AlumNiti").strip() or "AlumNiti"

    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": email}],
        "subject": "AlumNiti Verification Code",
        "textContent": (
            f"Your AlumNiti verification code is: {otp}\n\n"
            f"This code expires in {OTP_TTL_MINUTES} minutes."
        ),
    }

    timeout = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": settings.BREVO_API_KEY,
                "accept": "application/json",
                "content-type": "application/json",
            },
            json=payload,
        )

    if resp.status_code >= 400:
        detail = resp.text
        if len(detail) > 500:
            detail = detail[:500] + "…"
        raise RuntimeError(f"Brevo API error {resp.status_code}: {detail}")


async def _send_otp_email_via_smtp(email: str, otp: str) -> None:
    message = MessageSchema(
        subject="AlumNiti Verification Code",
        recipients=[email],
        body=(
            f"Your AlumNiti verification code is: {otp}\n\n"
            f"This code expires in {OTP_TTL_MINUTES} minutes."
        ),
        subtype="plain",
    )
    fm = FastMail(mail_conf)
    await fm.send_message(message)


async def _send_otp_email(email: str, otp: str) -> None:
    if settings.BREVO_API_KEY:
        await _send_otp_email_via_brevo_api(email=email, otp=otp)
        return

    await _send_otp_email_via_smtp(email=email, otp=otp)


def _hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()


async def send_otp(email: str) -> dict:
    """
    Check for duplicate email, generate a 6-digit OTP, send via email,
    and store the hashed OTP in the otp_verifications collection.

    Returns {"ok": True} on success or raises ValueError.
    """
    db = get_database()

    # Block duplicate emails already in use by a registered user
    existing = await db.users.find_one({"email": email})
    if existing:
        raise ValueError("email_taken")

    otp = str(random.randint(100000, 999999))
    otp_hash = _hash_otp(otp)
    expires_at = datetime.utcnow() + timedelta(minutes=OTP_TTL_MINUTES)

    if settings.OTP_DEBUG_LOG:
        print(f"🔐 OTP for {email}: {otp} (expires in {OTP_TTL_MINUTES} minutes)")

    # Upsert so re-sends overwrite the old OTP for that email
    await db.otp_verifications.update_one(
        {"email": email},
        {
            "$set": {
                "email": email,
                "otp_hash": otp_hash,
                "expires_at": expires_at,
                "verified": False,
                "verification_token": None,
                "created_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )

    try:
        await _send_otp_email(email=email, otp=otp)
    except Exception as e:
        # Clean up the record so users can retry
        await db.otp_verifications.delete_one({"email": email})
        raise RuntimeError(
            "Failed to send OTP email: "
            f"{e}. "
            "If you're deploying on a platform that blocks outbound SMTP (common), "
            "set BREVO_API_KEY to send OTP via Brevo HTTPS API instead of SMTP."
        )

    if settings.OTP_DEBUG_RETURN:
        return {"ok": True, "debug_otp": otp}
    return {"ok": True}


async def verify_otp(email: str, otp: str) -> str:
    """
    Verify the OTP for a given email address.

    Returns a one-time verification_token on success, raises ValueError otherwise.
    """
    db = get_database()
    record = await db.otp_verifications.find_one({"email": email})

    if not record:
        raise ValueError("no_otp")

    if record.get("verified"):
        raise ValueError("already_verified")

    if datetime.utcnow() > record["expires_at"]:
        raise ValueError("otp_expired")

    if record["otp_hash"] != _hash_otp(otp):
        raise ValueError("invalid_otp")

    verification_token = secrets.token_urlsafe(32)
    token_expires_at = datetime.utcnow() + timedelta(minutes=VERIFICATION_TOKEN_TTL_MINUTES)

    await db.otp_verifications.update_one(
        {"email": email},
        {
            "$set": {
                "verified": True,
                "verification_token": verification_token,
                "token_expires_at": token_expires_at,
            }
        },
    )

    return verification_token


async def consume_verification_token(email: str, token: str) -> bool:
    """
    Validate and consume a verification token during signup.
    Returns True if valid, False otherwise.
    Deletes the record after successful consumption.
    """
    db = get_database()
    record = await db.otp_verifications.find_one({"email": email})

    if not record:
        return False

    if not record.get("verified"):
        return False

    if record.get("verification_token") != token:
        return False

    if datetime.utcnow() > record.get("token_expires_at", datetime.min):
        return False

    # One-time use — delete after successful consumption
    await db.otp_verifications.delete_one({"email": email})
    return True
