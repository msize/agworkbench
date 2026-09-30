#!/usr/bin/env python3
"""tslog - the local-time prefix on every line a long-running pane prints (#78).

The `#queue` conductor (`conductor.py run`), the `#triage` watch (`triage.py watch`) and the relay
call `install()` before they print anything. From then on every line they write to stdout or stderr
starts with `HH:MM:SS `, or with `YYYY-MM-DD HH:MM:SS ` on the process's first line and on the first
line after the local date changes. The stamp is taken when a line's first character is written.

Stamping happens at the stream, not at each `print`, so a multi-line write, argparse and a traceback
are stamped too. Output that is parsed or written to a file (JSON replies, `--dry-run`, the
`triage-N.log` a `triage.py run` writes) never installs it.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime

LINE = re.compile(r'[^\n]*\n|[^\n]+')


class Stamper:
    """The prefix for the next line; stdout and stderr share one, so they share the date state."""

    def __init__(self, clock=None):
        self.clock = clock or datetime.now
        self.day = None

    def prefix(self) -> str:
        now = self.clock()
        full = now.date() != self.day
        self.day = now.date()
        return now.strftime('%Y-%m-%d %H:%M:%S ' if full else '%H:%M:%S ')


class StampedStream:
    """A text stream that writes the stamper's prefix at every line start."""

    def __init__(self, stream, stamper: Stamper):
        self.stream = stream
        self.stamper = stamper
        self.at_start = True

    def write(self, text: str) -> int:
        for piece in LINE.findall(text):      # only '\n' ends a line; each piece keeps its own
            if self.at_start:
                self.stream.write(self.stamper.prefix())
            self.stream.write(piece)
            self.at_start = piece.endswith('\n')
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def install(clock=None) -> Stamper | None:
    """Wrap sys.stdout and sys.stderr with one shared Stamper. Idempotent: a stream already wrapped
    is kept, and its stamper is shared with the other one; a missing stream (pythonw) is skipped."""
    streams = ('stdout', 'stderr')
    wrapped = [getattr(sys, name) for name in streams if isinstance(getattr(sys, name), StampedStream)]
    stamper = wrapped[0].stamper if wrapped else Stamper(clock)
    for name in streams:
        stream = getattr(sys, name)
        if stream is not None and not isinstance(stream, StampedStream):
            setattr(sys, name, StampedStream(stream, stamper))
    return stamper
