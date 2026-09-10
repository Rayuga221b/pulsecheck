"""Slack incoming-webhook notifier.

Why Slack incoming webhooks and not the Slack Web API / a bot token: an incoming
webhook is a single opaque URL that accepts one HTTP POST of ``{"text": "..."}``
and posts it to one preconfigured channel. No OAuth, no token refresh, no scopes,
no SDK. That is the entire contract, and it is the same shape as the Discord
webhook this originally used — the channel is a swappable detail, not a design.

Why no ``requests`` / ``slack_sdk``: one POST of a JSON body does not justify a
dependency to install and keep patched on the host. ``urllib`` from the standard
library is enough and is readable line by line.

The webhook URL is a secret. It lives only in ``.env`` (``SLACK_WEBHOOK_URL``);
``.env.example`` carries a placeholder. If it is unset the watchdog still runs and
still logs + records incidents — it just skips the Slack POST and says so. That
keeps local development and the M6 tests from needing a real webhook.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class SlackNotifier:
    webhook_url: str | None
    timeout_seconds: float = 5.0

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def _post(self, text: str) -> bool:
        """POST ``{"text": text}`` to the webhook. Returns True on HTTP 2xx.

        Never raises: a failed alert must not crash the watchdog or abort the
        recovery it was reporting. Failures are returned as False for the caller
        to log.
        """
        if not self.webhook_url:
            return False
        payload = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                return 200 <= resp.getcode() < 300
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def incident(
        self,
        *,
        component: str,
        symptom: str,
        detected_via: str,
        action_taken: str,
        detected_at: str,
        incident_id: int,
    ) -> bool:
        """Send the "something broke and here is what I did" alert.

        Deliberately reports all three of: the *symptom* (what was observed), the
        *action taken* (what the watchdog did about it), and the identifiers
        (component, incident id, time) needed to find the row and the logs.
        """
        text = (
            f":rotating_light: *pulsecheck incident #{incident_id}* — `{component}`\n"
            f"*Symptom:* {symptom}\n"
            f"*Detected via:* {detected_via}\n"
            f"*Action taken:* {action_taken}\n"
            f"*Detected at:* {detected_at}"
        )
        return self._post(text)

    def recovery(
        self,
        *,
        component: str,
        downtime_seconds: int | None,
        resolved_at: str,
        incident_id: int,
    ) -> bool:
        """Send the "it's back" alert, with how long the incident was open."""
        downtime = (
            f"{downtime_seconds}s" if downtime_seconds is not None else "unknown"
        )
        text = (
            f":white_check_mark: *pulsecheck recovered* — `{component}` "
            f"(incident #{incident_id})\n"
            f"*Downtime:* {downtime}\n"
            f"*Resolved at:* {resolved_at}"
        )
        return self._post(text)
