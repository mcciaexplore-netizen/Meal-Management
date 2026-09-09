import base64
import html
import io
import re
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate

from .errors import DependencyError, DomainError
from .security import required_text, token_digest


class DeliveryError(DomainError):
    def __init__(self, code, uncertain=False):
        self.uncertain = bool(uncertain)
        super().__init__(code)


def _email_address(value, code):
    if not isinstance(value, str) or not value.isascii() or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DeliveryError(code)
    value = value.strip().lower()
    if len(value) > 254 or value.count("@") != 1:
        raise DeliveryError(code)
    local, domain = value.rsplit("@", 1)
    if (
        not 1 <= len(local) <= 64 or local.startswith(".") or local.endswith(".") or ".." in local
        or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
        or "." not in domain or len(domain) > 253
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in domain.split("."))
    ):
        raise DeliveryError(code)
    return value


def validate_email_sender(value):
    return _email_address(value, "INVALID_EMAIL_SENDER")


def validate_email_recipient(value):
    recipient = _email_address(value, "INVALID_EMAIL_RECIPIENT")
    domain = recipient.rsplit("@", 1)[1]
    forbidden = ("test", "invalid", "localhost", "example", "example.com", "example.net", "example.org")
    if any(domain == suffix or domain.endswith("." + suffix) for suffix in forbidden):
        raise DeliveryError("TEST_EMAIL_RECIPIENT_FORBIDDEN")
    return recipient


def _qr_message(sender, preview):
    try:
        recipient = validate_email_recipient(preview.recipient_email)
        try:
            name = required_text(preview.employee_name, "EMAIL_CONTENT", 150)
            token_digest(preview.token)
        except DomainError as error:
            raise DeliveryError("INVALID_QR" if error.code == "INVALID_QR" else "INVALID_EMAIL_CONTENT") from None
        try:
            import qrcode

            output = io.BytesIO()
            qrcode.make(preview.token).save(output, format="PNG")
            message = EmailMessage(policy=SMTP)
            message["From"] = sender
            message["To"] = recipient
            message["Subject"] = "Your personal meal QR"
            message["Date"] = formatdate(localtime=False)
            identifier = "<" + secrets.token_hex(16) + "@" + sender.rsplit("@", 1)[1] + ">"
            message["Message-ID"] = identifier
            message.set_content(
                "Hello " + name + ",\n\nYour personal meal QR is attached. "
                "Present it when receiving a meal. Keep it private and contact your "
                "administrator if it is lost or shared.\n",
                cte="quoted-printable",
            )
            message.add_attachment(output.getvalue(), maintype="image", subtype="png", filename="meal-qr.png")
            wire = message.as_bytes()
        except Exception:
            raise DeliveryError("INVALID_EMAIL_CONTENT") from None
        return recipient, identifier, wire
    except DeliveryError:
        raise
    except Exception:
        raise DeliveryError("INVALID_EMAIL_CONTENT") from None


class GmailDelivery:
    def __init__(self, sender, app_password, enabled=False, smtp_factory=None, timeout=20):
        self.sender = validate_email_sender(sender)
        normalized = app_password.replace(" ", "") if isinstance(app_password, str) else ""
        if not re.fullmatch(r"[A-Za-z]{16}", normalized) or type(enabled) is not bool:
            raise DeliveryError("GMAIL_CONFIGURATION_REQUIRED")
        if type(timeout) not in {int, float} or not 1 <= timeout <= 30:
            raise DeliveryError("GMAIL_CONFIGURATION_REQUIRED")
        self._app_password = normalized
        self.enabled = enabled
        self.timeout = timeout
        self._smtp_factory = smtp_factory

    def _message(self, preview):
        recipient, identifier, wire = _qr_message(self.sender, preview)
        wire = re.sub(br"(?m)^\.", b"..", wire)
        if not wire.endswith(b"\r\n"):
            wire += b"\r\n"
        return recipient, identifier, wire + b".\r\n"

    def send(self, preview, *, allow_real_email=False):
        if self.enabled is not True or allow_real_email is not True:
            raise DeliveryError("REAL_EMAIL_NOT_AUTHORIZED")
        try:
            recipient, identifier, wire = self._message(preview)
        except DeliveryError:
            raise
        except Exception:
            raise DeliveryError("INVALID_EMAIL_CONTENT") from None
        smtp = None
        data_started = False
        try:
            factory = self._smtp_factory or smtplib.SMTP_SSL
            smtp = factory("smtp.gmail.com", 465, timeout=self.timeout, context=ssl.create_default_context())
            smtp.set_debuglevel(0)
            smtp.ehlo_or_helo_if_needed()
            smtp.login(self.sender, self._app_password)
            code, _ = smtp.mail(self.sender)
            if code != 250:
                raise DeliveryError("EMAIL_SENDER_REJECTED")
            code, _ = smtp.rcpt(recipient)
            if code not in {250, 251}:
                raise DeliveryError("EMAIL_RECIPIENT_REJECTED")
            code, _ = smtp.docmd("DATA")
            if code != 354:
                raise DeliveryError("EMAIL_DATA_REJECTED")
            data_started = True
            smtp.send(wire)
            code, _ = smtp.getreply()
            if code != 250:
                if type(code) is int and 400 <= code <= 599:
                    raise DeliveryError("EMAIL_DATA_REJECTED")
                raise DeliveryError("EMAIL_DELIVERY_UNCONFIRMED", uncertain=True)
            return identifier
        except DeliveryError:
            raise
        except smtplib.SMTPAuthenticationError:
            raise DeliveryError(
                "EMAIL_DELIVERY_UNCONFIRMED" if data_started else "EMAIL_AUTHENTICATION_FAILED",
                uncertain=data_started,
            ) from None
        except Exception:
            raise DeliveryError(
                "EMAIL_DELIVERY_UNCONFIRMED" if data_started else "EMAIL_DELIVERY_FAILED",
                uncertain=data_started,
            ) from None
        finally:
            if smtp is not None:
                try:
                    smtp.close()
                except Exception:
                    pass


class LocalEmailPreview:
    def render(self, preview):
        name = html.escape(preview.employee_name, quote=True)
        recipient = html.escape(preview.recipient_email, quote=True)
        image = base64.b64encode(preview.qr_svg.encode("utf-8")).decode("ascii")
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Employee meal QR email preview</title></head><body>'
            '<main><h1>Your personal meal QR</h1><p>To: ' + recipient + '</p>'
            '<p>Hello ' + name + ',</p>'
            '<p>Present this QR to your waiter when receiving a meal. '
            'Keep it private and report a lost or shared QR to your administrator.</p>'
            '<img width="280" height="280" alt="Personal meal QR" '
            'src="data:image/svg+xml;base64,' + image + '">'
            '<p>This local preview has not sent an email.</p></main></body></html>'
        )


class SESDelivery:
    def __init__(self, sender, region, enabled=False, client=None):
        if not sender or not region:
            raise DomainError("SES_CONFIGURATION_REQUIRED")
        self.sender = validate_email_sender(sender)
        self.region = region
        self.enabled = enabled
        self._client = client

    def _get_client(self):
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise DependencyError("BOTO3_NOT_INSTALLED") from None
            config = Config(retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=20)
            self._client = boto3.client("ses", region_name=self.region, config=config)
        return self._client

    def send(self, preview, *, allow_real_email=False):
        if self.enabled is not True or allow_real_email is not True:
            raise DeliveryError("REAL_EMAIL_NOT_AUTHORIZED")
        recipient, _, wire = _qr_message(self.sender, preview)
        response = self._get_client().send_raw_email(
            Source=self.sender,
            Destinations=[recipient],
            RawMessage={"Data": wire},
        )
        return response["MessageId"]


def delivery_from_settings(settings):
    if settings.email_backend == "preview":
        if settings.environment == "production":
            raise DomainError("PREVIEW_EMAIL_FORBIDDEN_IN_PRODUCTION")
        return LocalEmailPreview()
    if settings.email_backend == "ses":
        return SESDelivery(settings.email_sender, settings.aws_region, settings.email_send_enabled)
    if settings.email_backend == "gmail":
        return GmailDelivery(settings.email_sender, settings.gmail_app_password, settings.email_send_enabled)
    raise DomainError("INVALID_EMAIL_BACKEND")
