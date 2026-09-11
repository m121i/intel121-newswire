"""Slack delivery, verified.

The one rule here is that a post is not considered delivered until Slack says `ok`.

The JI Newswire fired and forgot for months: it posted with exceptions muted and never
looked at the response, so when the webhook was revoked the script kept "succeeding" —
executions all green, the log still reporting "Posted N items" — into a dead endpoint
for eight days. `post()` therefore returns nothing and raises on anything that is not a
literal `ok` body, and the caller treats that as a failed run.

Note the `.strip()` on the webhook URL. A URL pasted into a secret with a trailing
newline fails in a way that looks exactly like a revoked token, and that cost a day
once already.
"""
from __future__ import annotations

import os

import requests

TIMEOUT = 20


class DeliveryError(RuntimeError):
    """Slack did not accept the message. Never swallowed — an undelivered digest must
    leave its items unclaimed so the next run resends them."""


def _escape(text: str) -> str:
    """Slack mrkdwn requires exactly these three escaped."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackClient:
    def __init__(self, webhook_url: str | None = None):
        raw = webhook_url if webhook_url is not None else os.environ.get("SLACK_WEBHOOK_URL", "")
        self.webhook_url = (raw or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    def post(self, text: str) -> None:
        """Send one message. Raises DeliveryError unless Slack answers with `ok`."""
        if not self.webhook_url:
            raise DeliveryError("SLACK_WEBHOOK_URL is not set")

        payload = {"text": text, "unfurl_links": False, "unfurl_media": False}
        try:
            r = requests.post(self.webhook_url, json=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise DeliveryError(f"{type(e).__name__} posting to Slack: {e}") from e

        body = (r.text or "").strip()
        if r.status_code != 200 or body != "ok":
            raise DeliveryError(
                f"Slack rejected the post: HTTP {r.status_code}, body {body[:200]!r}"
            )
