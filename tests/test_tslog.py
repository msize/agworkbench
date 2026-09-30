"""The timestamp prefix on every line the long-running panes print (#78), with an injected clock."""
import io
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import tslog


class Clock:
    def __init__(self, *times):
        self.times = [datetime.fromisoformat(t) for t in times]

    def __call__(self):
        return self.times.pop(0) if len(self.times) > 1 else self.times[0]


def stamped(*times):
    out = io.StringIO()
    return out, tslog.StampedStream(out, tslog.Stamper(Clock(*times)))


class Prefix(unittest.TestCase):
    def test_the_first_line_has_the_date_and_the_rest_of_the_day_does_not(self):
        stamper = tslog.Stamper(Clock('2026-09-30 13:04:05', '2026-09-30 13:04:06', '2026-09-30 23:59:59'))
        self.assertEqual(['2026-09-30 13:04:05 ', '13:04:06 ', '23:59:59 '],
                         [stamper.prefix() for _ in range(3)])

    def test_the_first_line_after_midnight_has_the_date_again(self):
        stamper = tslog.Stamper(Clock('2026-09-30 23:59:59', '2026-10-01 00:00:00', '2026-10-01 00:00:01'))
        self.assertEqual(['2026-09-30 23:59:59 ', '2026-10-01 00:00:00 ', '00:00:01 '],
                         [stamper.prefix() for _ in range(3)])

    def test_the_default_clock_is_local_time(self):
        self.assertRegex(tslog.Stamper().prefix(), r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d $')


class Stream(unittest.TestCase):
    def test_every_line_of_one_multi_line_write_is_stamped(self):
        out, stream = stamped('2026-09-30 10:00:00', '2026-09-30 10:00:01', '2026-09-30 10:00:02')
        self.assertEqual(len('queue done\n\n| Issue |\n'), stream.write('queue done\n\n| Issue |\n'))
        self.assertEqual('2026-09-30 10:00:00 queue done\n10:00:01 \n10:00:02 | Issue |\n', out.getvalue())

    def test_a_print_is_one_line_with_one_stamp(self):
        out, stream = stamped('2026-09-30 10:00:00', '2026-09-30 10:00:01')
        print('#724: pending', file=stream, flush=True)         # two writes: the text, then '\n'
        print('#469:', 'priority:P1', file=stream)
        self.assertEqual('2026-09-30 10:00:00 #724: pending\n10:00:01 #469: priority:P1\n', out.getvalue())

    def test_a_line_is_stamped_when_its_first_character_is_written(self):
        out, stream = stamped('2026-09-30 10:00:00', '2026-09-30 10:00:05')
        stream.write('')
        self.assertEqual('', out.getvalue())
        stream.write('partial')
        stream.write('')
        stream.write(' rest\nnext')
        self.assertEqual('2026-09-30 10:00:00 partial rest\n10:00:05 next', out.getvalue())

    def test_writelines_is_stamped_too(self):
        out, stream = stamped('2026-09-30 10:00:00', '2026-09-30 10:00:01')
        stream.writelines(['Traceback (most recent call last):\n', 'ValueError: x\n'])
        self.assertEqual('2026-09-30 10:00:00 Traceback (most recent call last):\n10:00:01 ValueError: x\n',
                         out.getvalue())

    def test_other_attributes_reach_the_wrapped_stream(self):
        out, stream = stamped('2026-09-30 10:00:00')
        stream.write('x\n')
        self.assertEqual(out.getvalue(), stream.getvalue())
        self.assertEqual(out.encoding, stream.encoding)
        self.assertFalse(stream.isatty())
        stream.flush()


class Install(unittest.TestCase):
    def setUp(self):
        self.out, self.err = io.StringIO(), io.StringIO()
        self.enterContext(patch.object(sys, 'stdout', self.out))
        self.enterContext(patch.object(sys, 'stderr', self.err))

    def test_stdout_and_stderr_share_the_date_state(self):
        tslog.install(Clock('2026-09-30 23:59:58', '2026-10-01 00:00:01', '2026-10-01 00:00:02'))
        print('before midnight')
        print('queue: boom', file=sys.stderr)
        print('after midnight')
        self.assertEqual('2026-09-30 23:59:58 before midnight\n00:00:02 after midnight\n', self.out.getvalue())
        self.assertEqual('2026-10-01 00:00:01 queue: boom\n', self.err.getvalue())

    def test_installing_twice_never_wraps_twice(self):
        first = tslog.install(Clock('2026-09-30 10:00:00'))
        stdout = sys.stdout
        self.assertIs(first, tslog.install())
        self.assertIs(stdout, sys.stdout)
        self.assertIs(self.out, sys.stdout.stream)
        print('once')
        self.assertEqual('2026-09-30 10:00:00 once\n', self.out.getvalue())

    def test_a_stream_wrapped_alone_shares_its_stamper_with_the_other(self):
        stamper = tslog.Stamper(Clock('2026-09-30 10:00:00'))
        sys.stdout = tslog.StampedStream(self.out, stamper)
        self.assertIs(stamper, tslog.install())
        self.assertIs(stamper, sys.stderr.stamper)

    def test_a_missing_stream_is_skipped(self):
        sys.stderr = None
        tslog.install(Clock('2026-09-30 10:00:00'))
        self.assertIsNone(sys.stderr)
        self.assertIsInstance(sys.stdout, tslog.StampedStream)


if __name__ == '__main__':
    unittest.main()
