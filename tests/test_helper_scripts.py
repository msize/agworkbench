"""#45: revmux and revdiff helpers never end silently - a helper that fails still mails the planner."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

import hub  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PWSH = shutil.which('pwsh')


@unittest.skipIf(PWSH is None, 'pwsh not on PATH')
class HelperScripts(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / ('test helper scripts ' + uuid.uuid4().hex)
        self.checkout = self.folder / 'checkout'
        (self.checkout / '.workbench').mkdir(parents=True)
        self.bin = self.folder / 'bin'
        self.bin.mkdir()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(hub.reload_paths)

    def run_script(self, script, *params, pane=None):
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''))
        for name in ('AGWINTERM_PANE_ID', 'AGWINTERM_SESSION_ID'):
            env.pop(name, None)                          # no pane: no marker, nothing else changes
        if pane:
            # A pane id writes the marker; no terminal answers, so its rows are empty.
            env.update(AGWINTERM_PANE_ID=pane, AGWINTERM_PIPE='agw-test-' + uuid.uuid4().hex,
                       AGWINTERMCTL=str(self.folder / 'no-agwintermctl.exe'))
        return subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'lib' / script),
                               '-Checkout', str(self.checkout), *params],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120, env=env)

    def mails(self):
        os.environ['AI_HUB'] = str(self.checkout / '.workbench')
        return [hub.parse_message(path) for path in sorted((self.checkout / '.workbench' / 'inbox' / 'claude').glob('*.md'))]

    def test_a_revmux_round_that_fails_mails_the_planner(self):
        (self.bin / 'revmux.cmd').write_text('@echo revmux is broken\r\n@exit /b 3\r\n', encoding='utf-8')
        scope = self.checkout / 'scope.md'
        scope.write_text('scope', encoding='utf-8')
        done = self.run_script('run-revmux.ps1', '-ScopeFile', str(scope), '-Round', '2')
        self.assertNotEqual(0, done.returncode)
        [mail] = self.mails()
        self.assertEqual(('helper', 'note'), (mail['from'], mail['kind']))
        self.assertEqual('revmux round 2: ended without a report (it did not finish)', mail['subject'])

    def stub_revmux(self):
        """A revmux that records its argv, answers `new` with its paths and a round with a clean report."""
        stub = self.bin / 'revmux_stub.py'
        stub.write_text(
            'import json, sys, pathlib\n'
            f'root = pathlib.Path(r"{self.folder}")\n'
            'with open(root / "calls.txt", "a", encoding="utf-8") as f: f.write(" ".join(sys.argv[1:]) + "\\n")\n'
            'if sys.argv[1] == "new":\n'
            '    run = sys.argv[sys.argv.index("--run") + 1]\n'
            '    d = root / "tasks" / run; (d / "input").mkdir(parents=True, exist_ok=True)\n'
            '    print(json.dumps({"round_dir": str(d), "scope": str(d / "input" / "scope.md")}))\n'
            'else:\n'
            '    print("# Review\\n\\nNo findings.\\n\\n## Sources\\n\\n| agent | status |\\n| --- | --- |\\n| a | ok |")\n',
            encoding='utf-8')
        (self.bin / 'revmux.cmd').write_text(f'@"{sys.executable}" "{stub}" %*\r\n', encoding='utf-8')

    def test_a_round_records_the_revmux_run_it_used(self):
        # #77: review-round reads the run's events.jsonl from this record.
        self.stub_revmux()
        scope = self.checkout / 'scope.md'
        scope.write_text('scope', encoding='utf-8')
        done = self.run_script('run-revmux.ps1', '-ScopeFile', str(scope), '-Round', '2', '-Profile', 'kimi-mixed')
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        record = json.loads((self.checkout / '.workbench/review/revmux-r2.json').read_text(encoding='utf-8-sig'))
        self.assertEqual({'run': 'r2', 'dir': str(self.folder / 'tasks' / 'r2'), 'profile': 'kimi-mixed', 'attempt': 0,
                          'scope': str(scope)}, record)
        [mail] = self.mails()
        self.assertEqual('revmux round 2: clean', mail['subject'])

    def test_a_rerun_passes_its_own_run_name(self):
        self.stub_revmux()
        scope = self.checkout / 'scope.md'
        scope.write_text('scope', encoding='utf-8')
        done = self.run_script('run-revmux.ps1', '-ScopeFile', str(scope), '-Round', '2', '-Run', 'r2-1', '-Attempt', '1')
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        calls = (self.folder / 'calls.txt').read_text(encoding='utf-8').splitlines()
        self.assertIn('new --task workbench --run r2-1', calls)
        self.assertTrue(any(call.startswith('--task workbench --run r2-1 ') for call in calls), calls)
        record = json.loads((self.checkout / '.workbench/review/revmux-r2.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(('r2-1', 1), (record['run'], record['attempt']))
        self.assertTrue((self.checkout / '.workbench/review/revmux-r2.md').exists())

    def marker(self, pane):
        return json.loads((self.checkout / '.workbench' / 'state' / 'helpers' / f'{pane}.done').read_text(encoding='utf-8'))

    def mail_ids(self):
        return [path.stem for path in sorted((self.checkout / '.workbench' / 'inbox' / 'claude').glob('*.md'))]

    def test_the_marker_names_the_posted_report(self):
        # #84: the relay closes the session once this mail has been read.
        self.stub_revmux()
        scope = self.checkout / 'scope.md'
        scope.write_text('scope', encoding='utf-8')
        done = self.run_script('run-revmux.ps1', '-ScopeFile', str(scope), '-Round', '2', pane='p-rev')
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        [mid] = self.mail_ids()
        marker = self.marker('p-rev')
        self.assertEqual(('revmux', 2, mid, 'claude'), (marker['kind'], marker['round'], marker['mail'], marker['to']))
        self.assertIn(f'posted {mid}.md -> claude', done.stdout)            # still echoed to the pane

    def test_a_failed_rounds_marker_names_no_mail(self):
        # #84 r1: the fallback note is not a result - it sends the planner to this session - so the
        # marker names no mail and the relay keeps the session open.
        (self.bin / 'revmux.cmd').write_text('@echo revmux is broken\r\n@exit /b 3\r\n', encoding='utf-8')
        scope = self.checkout / 'scope.md'
        scope.write_text('scope', encoding='utf-8')
        done = self.run_script('run-revmux.ps1', '-ScopeFile', str(scope), '-Round', '2', pane='p-fail')
        self.assertNotEqual(0, done.returncode)
        [mid] = self.mail_ids()
        marker = self.marker('p-fail')
        self.assertEqual(('revmux', 2), (marker['kind'], marker['round']))
        self.assertNotIn('mail', marker)
        self.assertNotIn('to', marker)
        self.assertIn(f'posted {mid}.md -> claude', done.stdout)            # the note is still echoed

    def test_a_review_that_fails_mails_the_planner(self):
        (self.bin / 'revdiff.ps1').write_text("throw 'revdiff crashed'\n", encoding='utf-8')
        done = self.run_script('human-review.ps1', '-Base', 'origin/main')
        self.assertNotEqual(0, done.returncode)
        [mail] = self.mails()
        self.assertEqual(('helper', 'note'), (mail['from'], mail['kind']))
        self.assertTrue(mail['subject'].startswith('human review (revdiff): ended without a result'), mail['subject'])
        self.assertIn('revdiff crashed', done.stdout + done.stderr)

    def review(self, stub):
        (self.bin / 'revdiff.cmd').write_text(stub, encoding='utf-8')
        done = self.run_script('human-review.ps1', '-Base', 'origin/main')
        return done, [(mail['from'], mail['subject']) for mail in self.mails()]

    def test_a_failed_revdiff_with_no_output_is_not_no_annotations(self):
        # r1 F1: a non-zero exit with nothing written must not tell the planner the human had nothing to add.
        done, mails = self.review('@exit /b 3\r\n')
        self.assertNotEqual(0, done.returncode)
        self.assertEqual([('helper', 'human review (revdiff): ended without a result (revdiff exit 3)')], mails)

    def test_annotations_are_posted_whatever_revdiffs_exit(self):
        done, mails = self.review('@echo fix this line>%3\r\n@exit /b 3\r\n')
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual([('human', 'human review (revdiff): annotations to address')], mails)

    def test_a_clean_quit_with_no_output_is_no_annotations(self):
        done, mails = self.review('@exit /b 0\r\n')
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual([('human', 'human review (revdiff): no annotations')], mails)


if __name__ == '__main__':
    unittest.main()
