"""SimMessenger: the Messenger port without a real provider (spec §6.1, MESSENGER=sim).

Renders the template and records a `message.sent` event; nothing leaves the machine. Replies are
simulated by posting to /webhooks/messages (the console's "simulate a reply"). A real WhatsApp or
SMS provider (phase 7) implements the same port.
"""

from __future__ import annotations

from typing import Any

from dispatch.core.events import MessageEvent
from dispatch.core.ids import new_id
from dispatch.core.models import Customer, utcnow
from dispatch.core.ports import MessageRef
from dispatch.store import Store

from .templates import render


class SimMessenger:
    def __init__(self, store: Store):
        self.store = store

    async def send(self, to: Customer, template: str, vars: dict[str, Any], channel: str = "whatsapp") -> MessageRef:
        return self.send_sync(to, template, vars, channel)

    def send_sync(self, to: Customer, template: str, vars: dict[str, Any], channel: str = "whatsapp") -> MessageRef:
        text = render(template, {"name": to.name.split()[0], **vars})
        ref = MessageRef(id=new_id("message"), channel="sim", sent_at=utcnow())
        self.store.record_message("message.sent", MessageEvent(
            message_id=ref.id, customer_id=to.id, order_id=vars.get("order_id"), channel=channel, text=text))
        return ref
