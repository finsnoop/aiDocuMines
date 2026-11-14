from __future__ import annotations

import email.utils as eut
import re
from typing import Iterable, List, Tuple, Optional

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template import Context, Template

from .models import EmailTemplate, OutboxEmail, EmailAttachment, EmailMessageLog

# utils.py (replace render_template with this version)
from django.template import Engine, Context


def normalize_addresses(addrs: Iterable[str] | str | None) -> List[str]:
    """
    Accept a string ('a@x.com, b@x.com') or list; return a cleaned list.
    """
    if not addrs:
        return []
    if isinstance(addrs, str):
        parts = re.split(r"[;,]", addrs)
        addrs = [p.strip() for p in parts if p.strip()]
    return [a for a in addrs if a]




def render_template(tpl_obj, ctx: dict[str, object]):
    """
    Render subject/body using different autoescape rules:
      - subject: autoescape=False
      - body_text: autoescape=False
      - body_html: autoescape=True
    """
    context = Context(ctx or {})

    # Subject (no escaping)
    subj_engine = Engine(autoescape=False)
    subj_t = subj_engine.from_string(tpl_obj.subject_template or "")
    subject = (subj_t.render(context) or "").strip()

    # Plain text (no escaping)
    text_engine = Engine(autoescape=False)
    text_t = text_engine.from_string(tpl_obj.body_text_template or "")
    body_text = (text_t.render(context) or "").strip()

    # HTML (escape ON)
    html_engine = Engine(autoescape=True)
    html_t = html_engine.from_string(tpl_obj.body_html_template or "")
    body_html = (html_t.render(context) or "").strip()

    return subject, body_text, body_html




def _default_from() -> str:
    return getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@localhost")


def _attach_core_file(msg: EmailMultiAlternatives, core_file, filename_hint: Optional[str], mime: Optional[str]):
    """
    Try common attribute names on your core.File model to read bytes.
    """
    # Try common FileField attrs
    for attr in ("blob", "file", "content", "document", "uploaded_file"):
        if hasattr(core_file, attr):
            f = getattr(core_file, attr)
            try:
                name = filename_hint or getattr(core_file, "filename", None) or getattr(f, "name", "attachment")
                msg.attach(name, f.read(), mime or None)
                return
            except Exception:
                pass


def build_email_message(out: OutboxEmail) -> EmailMultiAlternatives:
    """
    Build EmailMultiAlternatives with HTML part and attachments.
    """
    from_email = out.from_email or _default_from()

    msg = EmailMultiAlternatives(
        subject=out.subject or "",
        body=out.body_text or "",
        from_email=from_email,
        to=normalize_addresses(out.to),
        cc=normalize_addresses(out.cc),
        bcc=normalize_addresses(out.bcc),
        reply_to=normalize_addresses(out.reply_to),
        headers=out.headers or {},
        connection=get_connection(),  # honors your EMAIL_* settings
    )
    if out.body_html:
        msg.attach_alternative(out.body_html, "text/html")

    for a in out.attachments.all():
        if a.uploaded:
            msg.attach(a.filename or a.uploaded.name, a.uploaded.read(), a.mime_type or None)
        elif a.core_file:
            _attach_core_file(msg, a.core_file, a.filename, a.mime_type)

    # Ensure Message-Id header exists (most backends add it; safe to include)
    msg.extra_headers = msg.extra_headers or {}
    msg.extra_headers.setdefault("Message-Id", eut.make_msgid())

    return msg


def log(out: OutboxEmail, event: str, **details) -> None:
    EmailMessageLog.objects.create(outbox=out, event=event, details=details)


def render_into_outbox(out: OutboxEmail) -> None:
    """
    If a template is set, render and store the snapshot fields on the Outbox.
    """
    if out.template:
        subj, text, html = render_template(out.template, out.context or {})
        updates = []
        if subj and subj != out.subject:
            out.subject = subj
            updates.append("subject")
        if text and text != out.body_text:
            out.body_text = text
            updates.append("body_text")
        if html and html != out.body_html:
            out.body_html = html
            updates.append("body_html")
        if updates:
            out.save(update_fields=updates)
        log(out, "rendered", template=out.template.code)

