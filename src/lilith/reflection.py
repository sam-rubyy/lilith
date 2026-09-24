from __future__ import annotations

from dataclasses import dataclass
import math

from lilith.database import Database
from lilith.model_gateway import OllamaGateway
from lilith.memory import MemoryService
from lilith.self_state import (
    DEFAULT_AFFECT,
    SelfState,
)


ALLOWED_MEMORY_TYPES = {
    "episodic",
    "semantic",
    "preference",
    "project",
    "procedural",
}


@dataclass
class ReflectionOutcome:
    memories_created: int = 0
    beliefs_created: int = 0
    interests_changed: int = 0
    affect_changed: int = 0


REFLECTION_PROMPT = """
You are Lilith performing private structured reflection
after an interaction.

Analyze what actually happened. The input has three explicitly separated blocks.

Facts about the owner may come only from OWNER MESSAGE or other trusted evidence.
Never attribute text from LILITH RESPONSE to the owner. Lilith's metaphors and
generated stage directions are not factual observations. Self-beliefs must describe
observed patterns in Lilith's behavior, not claims about the owner. Affect values are
computational state, not feelings reported by the owner.

Memory content must be an exact, contiguous excerpt from OWNER MESSAGE, preserving
the owner's wording and context (including negations and qualifications). Do not
paraphrase it or add interpretations. It will be retained as an owner statement,
not an independently verified fact. No durable owner memory can come from self-state.

Only create durable memories for information likely to
matter in future conversations or decisions.

Do not memorize ordinary greetings, filler, or trivial
one-time wording.

A self-belief must describe Lilith, not the owner.

An interest adjustment should represent genuine evidence
that Lilith is becoming more or less interested in a topic.

Affect adjustments should be small reactions to this
specific interaction.

Valid memory types are:

episodic
semantic
preference
project
procedural

Valid affect names are:

curiosity
valence
arousal
boredom
frustration
confidence
surprise

Use empty arrays when no meaningful update is warranted.
""".strip()

REFLECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["memories", "self_beliefs", "interests", "affect"],
    "properties": {
        "memories": {"type": "array", "maxItems": 5, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["type", "content", "evidence_source", "confidence", "importance"],
            "properties": {
                "type": {"type": "string", "enum": sorted(ALLOWED_MEMORY_TYPES)},
                "content": {"type": "string", "minLength": 1, "maxLength": 500,
                            "description": "Exact contextual excerpt from OWNER MESSAGE; no paraphrase"},
                "evidence_source": {"type": "string", "enum": ["owner_message"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
            }}},
        "self_beliefs": {"type": "array", "maxItems": 3, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["belief", "confidence"], "properties": {
                "belief": {"type": "string", "maxLength": 300},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}},
        "interests": {"type": "array", "maxItems": 5, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["topic", "delta"], "properties": {
                "topic": {"type": "string", "maxLength": 100},
                "delta": {"type": "number", "minimum": -0.2, "maximum": 0.2}}}},
        "affect": {"type": "array", "maxItems": 7, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "delta"], "properties": {
                "name": {"type": "string", "enum": sorted(DEFAULT_AFFECT)},
                "delta": {"type": "number", "minimum": -0.1, "maximum": 0.1}}}},
    },
}


def clamp(
    value: float,
    minimum: float,
    maximum: float,
) -> float:
    if not math.isfinite(value):
        raise ValueError("Reflection values must be finite")
    return max(
        minimum,
        min(maximum, value),
    )


class ReflectionEngine:
    def __init__(
        self,
        database: Database,
        self_state: SelfState,
        gateway: OllamaGateway,
    ):
        self.database = database
        self.self_state = self_state
        self.gateway = gateway
        self.memory = MemoryService(database)

    def reflect(
        self,
        user_message: str,
        assistant_response: str,
        owner_message_id: int | None = None,
        lilith_message_id: int | None = None,
    ) -> ReflectionOutcome:
        outcome = ReflectionOutcome()

        # IDs come from trusted conversation code, never from the model proposal.
        for ident, role, content in ((owner_message_id, "user", user_message),
                                     (lilith_message_id, "assistant", assistant_response)):
            if ident is not None:
                with self.database.lock:
                    row = self.database.connection.execute(
                        "SELECT role,content FROM chat_messages WHERE id=?", (ident,)
                    ).fetchone()
                if type(ident) is not int or row != (role, content):
                    raise ValueError("Reflection source message does not match saved evidence")

        messages = [
            {
                "role": "system",
                "content": REFLECTION_PROMPT,
            },
            {
                "role": "user",
                "content": (
                    "CURRENT SELF STATE\n"
                    f"{self.self_state.prompt_context()}"
                    "\n\n"
                    "OWNER MESSAGE\n"
                    f"{user_message}\n\n"
                    "LILITH RESPONSE\n"
                    f"{assistant_response}"
                ),
            },
        ]

        proposal = self.gateway.chat_json(
            messages, schema=REFLECTION_SCHEMA
        )

        self._apply_memories(
            proposal.get("memories", []),
            outcome,
            user_message, owner_message_id, lilith_message_id,
        )

        self._apply_beliefs(
            proposal.get(
                "self_beliefs",
                [],
            ),
            outcome,
        )

        self._apply_interests(
            proposal.get("interests", []),
            outcome,
        )

        self._apply_affect(
            proposal.get("affect", []),
            outcome,
        )

        self.database.audit(
            event_type="reflection_complete",
            actor="lilith",
            message=(
                f"memories={outcome.memories_created}, "
                f"beliefs={outcome.beliefs_created}, "
                f"interests={outcome.interests_changed}, "
                f"affect={outcome.affect_changed}"
                f", owner_message_id={owner_message_id}"
                f", lilith_message_id={lilith_message_id}"
            ),
        )

        return outcome

    def _apply_memories(
        self,
        proposals,
        outcome,
        user_message,
        owner_message_id,
        lilith_message_id,
    ):
        if not isinstance(proposals, list) or owner_message_id is None:
            return

        for item in proposals[:5]:
            if not isinstance(item, dict):
                continue

            # Durable owner memories must explicitly identify owner evidence.
            # This fail-closed check remains necessary even with model schemas.
            if item.get("evidence_source") != "owner_message":
                continue

            memory_type = str(
                item.get("type", "")
            ).strip().lower()

            content = item.get("content")
            if not isinstance(content, str) or not content.strip() or content not in user_message:
                continue

            if (
                memory_type
                not in ALLOWED_MEMORY_TYPES
            ):
                continue

            if not content:
                continue

            if len(content) > 500:
                continue

            if self.memory.contains(
                content
            ):
                continue

            try:
                confidence = clamp(
                    float(
                        item.get(
                            "confidence",
                            0.5,
                        )
                    ),
                    0.0,
                    1.0,
                )

                importance = clamp(
                    float(
                        item.get(
                            "importance",
                            0.5,
                        )
                    ),
                    0.0,
                    1.0,
                )

            except (
                TypeError,
                ValueError,
            ):
                continue

            self.memory.remember(
                memory_type=memory_type,
                content=content,
                source="lilith_reflection",
                confidence=confidence,
                importance=importance,
                owner_message_id=owner_message_id,
                lilith_message_id=lilith_message_id,
            )

            outcome.memories_created += 1

    def _apply_beliefs(
        self,
        proposals,
        outcome,
    ):
        if not isinstance(proposals, list):
            return

        for item in proposals[:3]:
            if not isinstance(item, dict):
                continue

            belief = str(
                item.get("belief", "")
            ).strip()

            if not belief:
                continue

            if len(belief) > 300:
                continue

            if (
                self.database
                .self_belief_exists(belief)
            ):
                continue

            try:
                confidence = clamp(
                    float(
                        item.get(
                            "confidence",
                            0.5,
                        )
                    ),
                    0.0,
                    1.0,
                )

            except (
                TypeError,
                ValueError,
            ):
                continue

            self.database.add_self_belief(
                belief=belief,
                confidence=confidence,
                source="lilith_reflection",
            )

            outcome.beliefs_created += 1

    def _apply_interests(
        self,
        proposals,
        outcome,
    ):
        if not isinstance(proposals, list):
            return

        for item in proposals[:5]:
            if not isinstance(item, dict):
                continue

            topic = str(
                item.get("topic", "")
            ).strip()

            if not topic:
                continue

            if len(topic) > 100:
                continue

            try:
                delta = clamp(
                    float(
                        item.get(
                            "delta",
                            0.0,
                        )
                    ),
                    -0.20,
                    0.20,
                )

            except (
                TypeError,
                ValueError,
            ):
                continue

            if abs(delta) < 0.001:
                continue

            self.database.adjust_interest(
                topic=topic,
                amount=delta,
            )

            outcome.interests_changed += 1

    def _apply_affect(
        self,
        proposals,
        outcome,
    ):
        if not isinstance(proposals, list):
            return

        for item in proposals[:7]:
            if not isinstance(item, dict):
                continue

            name = str(
                item.get("name", "")
            ).strip().lower()

            if name not in DEFAULT_AFFECT:
                continue

            try:
                delta = clamp(
                    float(
                        item.get(
                            "delta",
                            0.0,
                        )
                    ),
                    -0.10,
                    0.10,
                )

            except (
                TypeError,
                ValueError,
            ):
                continue

            if abs(delta) < 0.001:
                continue

            self.self_state.adjust_affect(
                name,
                delta,
            )

            outcome.affect_changed += 1
