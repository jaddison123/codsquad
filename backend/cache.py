"""
A small in-memory cache, so the server remembers recent answers from Global
Fishing Watch instead of asking again every time.

Why it's safe: fishing data for past dates hardly changes, so an answer from a
few hours ago is still right. Entries expire after `ttl` seconds anyway, and
the cache is cleared whenever the server restarts.

When the cache is full, the entry used least recently is dropped first.
"""

import time
from collections import OrderedDict


class TTLCache:
    def __init__(self, max_items: int, ttl: float, max_bytes: int | None = None):
        self.max_items = max_items
        self.ttl = ttl                # seconds an entry stays valid
        self.max_bytes = max_bytes    # optional cap on total size, for big CSVs
        self.bytes = 0
        self._items = OrderedDict()   # key -> (expires_at, value, size)

    def get(self, key):
        """The remembered value, or None if there isn't a fresh one."""
        item = self._items.get(key)
        if item is None:
            return None
        if item[0] < time.monotonic():  # too old
            self._remove(key)
            return None
        self._items.move_to_end(key)  # mark as recently used
        return item[1]

    def set(self, key, value, size: int = 0):
        if self.max_bytes and size > self.max_bytes:
            return  # too big to keep at all
        if key in self._items:
            self._remove(key)
        self._items[key] = (time.monotonic() + self.ttl, value, size)
        self.bytes += size
        # Drop the least recently used entries until we're back under the limits
        while len(self._items) > self.max_items or (self.max_bytes and self.bytes > self.max_bytes):
            self._remove(next(iter(self._items)))

    def _remove(self, key):
        _, _, size = self._items.pop(key)
        self.bytes -= size
