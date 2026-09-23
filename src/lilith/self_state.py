from __future__ import annotations

from lilith.database import Database


DEFAULT_AFFECT = {
    "curiosity": 0.55,
    "valence": 0.50,
    "arousal": 0.25,
    "boredom": 0.15,
    "frustration": 0.05,
    "confidence": 0.50,
    "surprise": 0.10,
}


def clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


class SelfState:
    def __init__(self, database: Database):
        self.database = database

        self.database.ensure_identity(
            key="name",
            value="Lilith",
            source="bootstrap",
        )

        self.database.ensure_identity(
            key="self_description",
            value=(
                "A persistent local artificial intelligence whose "
                "identity develops through memory, reflection, "
                "interests, and interaction."
            ),
            source="bootstrap",
        )

        for name, value in DEFAULT_AFFECT.items():
            if self.database.get_affect(name) is None:
                self.database.set_affect(name, value)

    def affect(self) -> dict[str, float]:
        return self.database.get_all_affect()

    def set_affect(self, name: str, value: float) -> None:
        if name not in DEFAULT_AFFECT:
            raise ValueError(
                f"Unknown affect variable: {name}"
            )

        self.database.set_affect(
            name,
            clamp(value),
        )

    def adjust_affect(
        self,
        name: str,
        amount: float,
    ) -> None:
        if name not in DEFAULT_AFFECT:
            raise ValueError(f"Unknown affect variable: {name}")
        self.database.adjust_affect(name, amount, DEFAULT_AFFECT[name])

    def on_interaction_started(self) -> None:
        self.adjust_affect("boredom", -0.03)
        self.adjust_affect("arousal", 0.02)
        self.adjust_affect("curiosity", 0.01)

    def on_interaction_succeeded(self) -> None:
        self.adjust_affect("frustration", -0.02)
        self.adjust_affect("confidence", 0.01)
        self.adjust_affect("valence", 0.005)

    def on_interaction_failed(self) -> None:
        self.adjust_affect("frustration", 0.07)
        self.adjust_affect("confidence", -0.03)
        self.adjust_affect("valence", -0.02)

    def add_interest(
        self,
        topic: str,
        amount: float = 0.15,
    ) -> None:
        self.database.adjust_interest(
            topic=topic,
            amount=amount,
        )

    def prompt_context(self) -> str:
        identity = self.database.get_identity()
        affect = self.affect()
        interests = self.database.get_interests(limit=10)
        beliefs = self.database.get_self_beliefs(limit=10)

        lines = [
            "CURRENT SELF MODEL",
            "",
            f"Name: {identity.get('name', 'Lilith')}",
            (
                "Self-description: "
                + identity.get(
                    "self_description",
                    "No self-description recorded.",
                )
            ),
            "",
            "Current affect:",
        ]

        for name, value in affect.items():
            lines.append(
                f"- {name}: {value:.3f}"
            )

        lines.append("")
        lines.append("Current interests:")

        if interests:
            for interest in interests:
                lines.append(
                    f"- {interest['topic']}: "
                    f"{interest['fascination']:.3f}"
                )
        else:
            lines.append(
                "- No developed interests yet."
            )

        lines.append("")
        lines.append("Current self-beliefs:")

        if beliefs:
            for belief in beliefs:
                lines.append(
                    f"- {belief['belief']} "
                    f"(confidence "
                    f"{belief['confidence']:.2f})"
                )
        else:
            lines.append(
                "- No developed self-beliefs yet."
            )

        return "\n".join(lines)

    def render(self) -> str:
        return self.prompt_context()
