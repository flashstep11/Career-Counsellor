# OTP Email Setup (Brevo)

This backend can send OTP emails either via SMTP (FastAPI-Mail) or via Brevo's HTTPS API.

On some hosting providers (including many PaaS deployments), outbound SMTP ports like `587` are blocked or rate-limited.
If you see timeouts connecting to `smtp-relay.brevo.com:587`, use the **Brevo HTTPS API** option below.

## Recommended (Render): Brevo HTTPS API

1) In Brevo, create an API key:
- Transactional / SMTP & API  API Settings  Create API key

2) In Brevo, add and verify a Sender email:
- Settings  Senders  Add a sender  verify via email link

3) Set environment variables on Render (backend service):

- `BREVO_API_KEY=...` (from Brevo)
- `MAIL_FROM=verified-sender@example.com` (must be verified in Brevo)
- `MAIL_FROM_NAME=AlumNiti` (optional)

4) Redeploy the Render service.

The backend will automatically prefer the Brevo API when `BREVO_API_KEY` is set.

## Optional: SMTP (may fail on Render)

If your host allows SMTP egress, you can set:

- `MAIL_SERVER=smtp-relay.brevo.com`
- `MAIL_PORT=587`
- `MAIL_STARTTLS=true`
- `MAIL_SSL_TLS=false`
- `MAIL_USERNAME=...` (Brevo SMTP login)
- `MAIL_PASSWORD=...` (Brevo SMTP password/key)
- `MAIL_FROM=verified-sender@example.com`

Then redeploy.

## Debug flags (do not enable in production)

These are off by default:

- `OTP_DEBUG_LOG=true` logs OTP to server console
- `OTP_DEBUG_RETURN=true` returns `debug_otp` in the API response
