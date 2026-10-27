import html
import re
from datetime import datetime, timedelta, timezone

INDIA = timezone(timedelta(hours=5, minutes=30))
QR_SIZE = 240


def _single_line(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or re.search(r"[\r\n\x00-\x1f\x7f]", value):
        raise ValueError("Unsafe email field")
    return value.strip()


def valid_until(expires_at):
    if expires_at is None:
        return "Until every approved meal is used"
    moment = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(INDIA)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.day} {local.strftime('%b %Y')}, {hour}:{local.strftime('%M %p')} IST (UTC+05:30)"


def subject(visitor):
    event = visitor.get("event_name")
    return "Your meal QR for " + _single_line(event, 150) if event else "Your visitor meal QR"


def details(name, visitor):
    rows = [("Company", visitor["company_name"])]
    if visitor.get("department_name"):
        rows.append(("Department", visitor["department_name"]))
    if visitor.get("event_name"):
        rows.append(("Event", visitor["event_name"]))
    rows.append(("Meals", ", ".join(visitor["meal_types"]) + " (one each)"))
    rows.append(("Valid until", valid_until(visitor.get("expires_at"))))
    return _single_line(name, 150), rows


def text_body(name, visitor):
    name, rows = details(name, visitor)
    lines = [f"Hello {name},", "", "Show the attached QR at the meal counter. Each approved meal can be served once.", ""]
    lines += [f"{label}: {value}" for label, value in rows]
    lines += ["", "Keep this QR private. If it is lost or shared, contact the office that invited you.", ""]
    return "\n".join(lines)


def html_body(name, visitor, image_src):
    name, rows = details(name, visitor)
    escape = lambda value: html.escape(str(value), quote=True)
    table = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:#5a6b75;font-size:13px;white-space:nowrap;vertical-align:top">{escape(label)}</td>'
        f'<td style="padding:6px 0;color:#173746;font-size:14px;font-weight:600">{escape(value)}</td></tr>'
        for label, value in rows
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>' + escape(subject(visitor)) + '</title></head>'
        '<body style="margin:0;padding:0;background:#f1f5f6;font-family:-apple-system,Segoe UI,Arial,sans-serif">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f6"><tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border:1px solid #dfe7e9;border-radius:10px">'
        '<tr><td style="padding:22px 24px;background:#102d3a;border-radius:10px 10px 0 0;color:#ffffff;font-size:18px;font-weight:700">Your visitor meal QR</td></tr>'
        '<tr><td style="padding:24px">'
        f'<p style="margin:0 0 14px;font-size:15px;color:#173746">Hello {escape(name)},</p>'
        '<p style="margin:0 0 18px;font-size:14px;line-height:1.55;color:#395361">Show this QR at the meal counter. Each approved meal can be served once.</p>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 20px">{table}</table>'
        f'<p style="margin:0 0 8px;text-align:center"><img src="{escape(image_src)}" width="{QR_SIZE}" height="{QR_SIZE}" alt="Your meal QR code" '
        f'style="display:inline-block;width:{QR_SIZE}px;height:{QR_SIZE}px;border:1px solid #dfe7e9;border-radius:8px"></p>'
        '<p style="margin:16px 0 0;font-size:12px;line-height:1.5;color:#647781">Keep this QR private. If it is lost or shared, contact the office that invited you.</p>'
        '</td></tr></table>'
        '<p style="margin:14px 0 0;font-size:11px;color:#8a9aa2">Sent by Meal Office. This message has no tracking.</p>'
        '</td></tr></table></body></html>'
    )
