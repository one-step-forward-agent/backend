"""Mail from the service's own mailbox (SMTP_* settings): the codes that confirm an email at sign-up."""

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.core.config import settings

SENDER_NAME = "Dayla"


def _send(message: EmailMessage) -> None:
    context = ssl.create_default_context()
    # 465 is SSL from the first byte; 587 (and 25) start plain and switch with STARTTLS
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=20) as smtp:
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            smtp.starttls(context=context)
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)


async def send_email(to: str, subject: str, text: str, html: str | None = None) -> None:
    message = EmailMessage()
    # Yandex refuses a From that is not the mailbox logged in to
    message["From"] = formataddr((SENDER_NAME, settings.smtp_from))
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain=settings.smtp_from.rsplit("@", 1)[-1])
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    await asyncio.to_thread(_send, message)
