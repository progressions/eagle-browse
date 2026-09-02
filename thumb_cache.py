"""Byte-bounded LRU cache for decoded thumbnail textures."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

# ~96 MiB of estimated RGBA at current zoom. 400×360²×4 ≈ 198 MiB was the old cap.
DEFAULT_BYTE_BUDGET = 96 * 1024 * 1024


def estimate_texture_bytes(edge_px: int) -> int:
    """Estimated raw RGBA bytes for a square decoded thumb."""
    side = max(0, int(edge_px))
    return side * side * 4


@dataclass(frozen=True, slots=True)
class ThumbCacheMetrics:
    entries: int
    bytes: int
    byte_budget: int
    hits: int
    misses: int
    evictions: int
    inflight: int = 0


@dataclass(slots=True)
class _Entry(Generic[T]):
    value: T
    size: int
    nbytes: int


class ThumbTextureCache(Generic[T]):
    """LRU map keyed by thumb cache key; retention limited by byte budget."""

    def __init__(self, byte_budget: int = DEFAULT_BYTE_BUDGET) -> None:
        self.byte_budget = max(0, int(byte_budget))
        self._entries: OrderedDict[str, _Entry[T]] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def bytes(self) -> int:
        return self._bytes

    def get(self, key: str) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry.value

    def put(self, key: str, value: T, *, size: int) -> None:
        nbytes = estimate_texture_bytes(size)
        old = self._entries.pop(key, None)
        if old is not None:
            self._bytes -= old.nbytes
        self._entries[key] = _Entry(value=value, size=int(size), nbytes=nbytes)
        self._bytes += nbytes
        self._evict_to_budget()

    def pop(self, key: str) -> T | None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return None
        self._bytes -= entry.nbytes
        return entry.value

    def discard_matching(self, predicate) -> int:
        """Drop entries where predicate(key, size) is true. Returns count removed."""
        drop = [k for k, e in self._entries.items() if predicate(k, e.size)]
        for key in drop:
            entry = self._entries.pop(key)
            self._bytes -= entry.nbytes
            self.evictions += 1
        return len(drop)

    def discard_sizes_except(self, keep_size: int) -> int:
        """Reclaim textures decoded for other zoom sizes."""
        return self.discard_matching(lambda _key, size: size != keep_size)

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0

    def keys(self) -> list[str]:
        return list(self._entries.keys())

    def _evict_to_budget(self) -> None:
        while self._bytes > self.byte_budget and self._entries:
            _key, entry = self._entries.popitem(last=False)
            self._bytes -= entry.nbytes
            self.evictions += 1

    def metrics(self, *, inflight: int = 0) -> ThumbCacheMetrics:
        return ThumbCacheMetrics(
            entries=len(self._entries),
            bytes=self._bytes,
            byte_budget=self.byte_budget,
            hits=self.hits,
            misses=self.misses,
            evictions=self.evictions,
            inflight=int(inflight),
        )
