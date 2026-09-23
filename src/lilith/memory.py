"""Stable memory boundary; retrieval can evolve without changing cognition code."""


class MemoryService:
    def __init__(self, database):
        self.database = database

    def retrieve(self, *, limit=10):
        return self.database.recent_memories(limit=limit)

    def contains(self, content):
        return self.database.memory_exists(content)

    def remember(self, memory_type, content, source, *, confidence=1.0, importance=0.5):
        return self.database.save_memory(
            memory_type=memory_type,
            content=content,
            source=source,
            confidence=confidence,
            importance=importance,
        )
