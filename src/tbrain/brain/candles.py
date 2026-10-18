"""1-minute OHLCV candles per instrument, built from feed updates (price = LTP, volume = increase in vtt)."""
from __future__ import annotations

MIN_MS = 60_000


class CandleBook:
    def __init__(self, max_bars: int = 400):
        self.max_bars = max_bars
        self._bars: dict[str, dict[int, list[float]]] = {}

    def update(self, key: str, ts_ms: int, price: float, dv: float = 0.0) -> None:
        m = int(ts_ms) - int(ts_ms) % MIN_MS
        book = self._bars.setdefault(key, {})
        b = book.get(m)
        if b is None:
            if book and m < max(book):  # late/out-of-order print for an old minute: fold into that minute if kept
                return
            book[m] = [price, price, price, price, float(dv)]
            if len(book) > self.max_bars:
                for old in sorted(book)[: len(book) - self.max_bars]:
                    del book[old]
        else:
            b[1] = max(b[1], price)
            b[2] = min(b[2], price)
            b[3] = price
            b[4] += dv

    def series(self, key: str, n: int = 150) -> list[list[float]]:
        book = self._bars.get(key) or {}
        return [[m, *book[m]] for m in sorted(book)[-n:]]

    def completed(self, key: str, now_ms: int, n: int = 60) -> list[list[float]]:
        cur = int(now_ms) - int(now_ms) % MIN_MS
        return [b for b in self.series(key, n + 1) if b[0] < cur][-n:]
