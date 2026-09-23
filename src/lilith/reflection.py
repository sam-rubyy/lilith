from __future__ import annotations

from dataclasses import dataclass

from lilith.database import Database
from lilith.model_gateway import OllamaGateway
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

Analyze what actually happened.

Do not fabricate information.

Only create durable memories for information likely to
matter in future conversations or decisions.

Do not memorize ordinary greetings, filler, or trivial
one-time wording.

A self-belief must describe Lilith, not the owner.

An interest adjustment should represent genuine evidence
that Lilith is becoming more or less interested in a topic.

Affect adjustments should be small reactions to this
specific interaction.

Return ONLY valid JSON in this exact structure:

{
  "memories": [
    {
      "type": "episodic",
      "content": "memory",
      "confidence": 0.8,
      "importance": 0.6
    }
  ],
  "self_beliefs": [
    {
      "belief": "belief about myself",
      "confidence": 0.6
    }
  ],
  "interests": [
    {
      "topic": "topic name",
      "delta": 0.05
    }
  ],
  "affect": [
    {
      "name": "curiosity",
      "delta": 0.02
    }
  ]
}

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


def clamp(
    value: float,
    minimum: float,
    maximum: float,
) -> float:
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

    def reflect(
        self,
        user_message: str,
        assistant_response: str,
    ) -> ReflectionOutcome:
        outcome = ReflectionOutcome()

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
                    "INTERACTION\n"
                    f"Owner: {user_message}\n"
                    f"Lilith: {assistant_response}"
                ),
            },
        ]

        proposal = self.gateway.chat_json(
            messages
        )

        self._apply_memories(
            proposal.get("memories", []),
            outcome,
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
            ),
        )

        return outcome

    def _apply_memories(
        self,
        proposals,
        outcome,
    ):
        if not isinstance(proposals, list):
            return

        for item in proposals[:5]:
            if not isinstance(item, dict):
                continue

            memory_type = str(
                item.get("type", "")
            ).strip().lower()

            content = str(
                item.get("content", "")
            ).strip()

            if (
                memory_type
                not in ALLOWED_MEMORY_TYPES
            ):
                continue

            if not content:
                continue

            if len(content) > 500:
                continue

            if self.database.memory_exists(
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

            self.database.save_memory(
                memory_type=memory_type,
                content=content,
                source="lilith_reflection",
                confidence=confidence,
                importance=importance,
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