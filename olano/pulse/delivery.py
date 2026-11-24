"""PULSE delivery helpers for outbound channels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from olano.platforms import MessageEnvelope
from olano.pulse.models import PulseItem

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig
    from olano.platforms import ChannelRegistry
    from olano.pulse.engine import PulseEngine


class PulseDelivery:
    """Render and deliver PULSE items across supported surfaces."""

    def __init__(self, engine: PulseEngine) -> None:
        self.engine = engine

    def format_items(self, items: list[PulseItem], channel_type: str) -> str:
        def _line(item: PulseItem, markdown: bool = False) -> str:
            label = item.title or item.content
            detail = item.content if item.title and item.content != item.title else ""
            next_step = item.suggested_next_step.strip()
            parts = [label]
            if detail:
                parts.append(detail)
            if next_step:
                parts.append(f"Next: {next_step}")
            body = " — ".join(parts)
            if markdown:
                return f"*{item.priority_level.value}* {body}"
            return f"[{item.priority_level.value}] {body}"

        if channel_type == "discord":
            header = f"**PULSE** ({len(items)} pending)"
            bullets = [f"- {_line(item, markdown=True)}" for item in items]
            return "\n".join([header, *bullets])
        if channel_type in {"telegram", "slack"}:
            header = f"*PULSE* ({len(items)} pending)"
            bullets = [f"• {_line(item, markdown=True)}" for item in items]
            return "\n".join([header, *bullets])
        lines = [f"[PULSE] {len(items)} pending item(s)"]
        lines.extend(f"  {self._emoji(item)} {_line(item)}" for item in items)
        return "\n".join(lines)

    async def deliver_to_channels(
        self,
        channel_registry: ChannelRegistry,
        config: AgentConfig,
    ) -> int:
        items = self.engine.get_deliverable(max_items=config.pulse.max_items_per_session)
        if not items:
            return 0

        delivered = 0
        for ch_entry in config.channels:
            ch_dict = (
                ch_entry.to_dict() if hasattr(ch_entry, "to_dict") else {"type": str(ch_entry)}
            )
            channel_type = ch_dict.get("type") or str(ch_entry)
            if not channel_registry.has_channel(channel_type, agent_name=config.name):
                continue
            target = (
                ch_dict.get("default_chat_id")
                or ch_dict.get("chat_id")
                or ch_dict.get("channel_id")
                or ""
            )
            if not target:
                continue
            body = self.format_items(items, channel_type)
            message_id = await channel_registry.send(
                MessageEnvelope(
                    text=body,
                    target=str(target),
                    channel_type=channel_type,
                    agent_name=config.name,
                    metadata={"kind": "pulse_delivery"},
                )
            )
            if message_id is None:
                continue
            delivered += len(items)
            for item in items:
                self.engine.mark_delivered(item.id)
        return delivered

    @staticmethod
    def _emoji(item: PulseItem) -> str:
        return {
            "urgent": "⚠️",
            "normal": "💡",
            "low": "⏰",
        }.get(item.priority_level.value, "•")
