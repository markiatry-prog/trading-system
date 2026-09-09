"""Visible progress for a run that takes minutes over millions of bars.

A long process that prints nothing is indistinguishable from a hung
one, which is how two runs came to be killed by a reboot nobody knew
was costly. This reports enough to answer "is it advancing, and how
long is left" without flooding a terminal: a line at most every
`min_interval` seconds, plus one at the end of every stage.

THE ETA IS AN EXTRAPOLATION AND IS LABELLED AS ONE. It assumes the
remaining sessions cost what the completed ones did, which is roughly
true because per-session work is bounded. It is not a promise, and
nothing in the study reads it.
"""
from __future__ import annotations

import sys
import time
from typing import Optional, TextIO


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


class StageProgress:
    """One stage's progress line, rate-limited."""

    def __init__(self, stage: str, total_sessions: int,
                 stream: Optional[TextIO] = None, min_interval: float = 5.0,
                 clock=time.monotonic):
        self.stage = stage
        self.total_sessions = max(1, total_sessions)
        self.stream = stream if stream is not None else sys.stdout
        self.min_interval = min_interval
        self._clock = clock
        self.started = clock()
        self._last_emit = 0.0
        self.sessions = 0
        self.bars = 0
        self.events = 0
        self.last_checkpoint: Optional[str] = None

    def advance(self, bars: int, events: int) -> None:
        self.sessions += 1
        self.bars += bars
        self.events += events
        now = self._clock()
        if now - self._last_emit >= self.min_interval:
            self._last_emit = now
            self.emit()

    def note_checkpoint(self, when: str) -> None:
        self.last_checkpoint = when

    def line(self) -> str:
        elapsed = self._clock() - self.started
        fraction = self.sessions / self.total_sessions
        eta = (elapsed / fraction - elapsed) if fraction > 0 else 0.0
        checkpoint = (f"  ckpt {self.last_checkpoint[11:19]}"
                      if self.last_checkpoint else "  ckpt none")
        return (f"   {self.stage:11} {self.sessions:>5}/{self.total_sessions} "
                f"sessions {fraction:>6.1%}  {self.bars:>10,} bars  "
                f"{self.events:>8,} events  "
                f"elapsed {format_duration(elapsed):>9}  "
                f"ETA ~{format_duration(eta):>9}{checkpoint}")

    def emit(self) -> None:
        print(self.line(), file=self.stream, flush=True)

    def finish(self) -> float:
        elapsed = self._clock() - self.started
        self._last_emit = self._clock()
        self.emit()
        return elapsed
