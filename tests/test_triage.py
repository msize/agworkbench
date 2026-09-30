"""Triage (#34) with a fake gh, git and model at the process boundary. Nothing calls GitHub or a model."""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))
import triage as t
import tslog

PRODUCT = 'yeroo/docxy'
PROJECT, WORD, EXCEL = 'yeroo/docxy-project-spec', 'yeroo/docxy-word-spec', 'yeroo/docxy-excel-spec'
SECRET = ['SCH-012', 'CHR-012', 'Schema capability', 'Charts area', 'table crash', 'docxy-project-spec',
          'docxy-word-spec', 'PRIVATE RATIONALE']


def done(code=0, out='', err=''):
    return subprocess.CompletedProcess([], code, out, err)


def issue(number, title='an issue', body='', labels=(), created='2026-01-01', association='OWNER', state='open'):
    return dict(number=number, title=title, body=body, labels=[{'name': n} for n in labels], created_at=created,
                author_association=association, state=state)


def answer(priority='P2', ux=False, rationale='PRIVATE RATIONALE: SCH-012 is fine', refs=(), exception='none'):
    return dict(priority=priority, ux=ux, rationale=rationale, specRefs=list(refs), exception=exception)


def envelope(value, **extra):
    return done(0, json.dumps(dict(type='result', is_error=False, structured_output=value, **extra)))


class TriageCase(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / ('test triage ' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.config = dict(specRepos=[PROJECT, WORD, EXCEL], model=None, bugLabel='bug')
        self.spec = {
            PROJECT: [issue(1, 'bug: table crash (docxy#100)', labels=['bug']),
                      issue(2, 'Schema capability SCH-012', 'Implemented by yeroo/docxy#12 and docxy#1000.', labels=['epic'])],
            WORD: [issue(5, 'Charts area CHR-012', 'See https://github.com/yeroo/docxy/issues/7, docxy-word#10, '
                                                   '#11 and other/docxy#11.', labels=['area'])],
        }
        self.missing = {EXCEL}
        self.product = [issue(100, 'Tables crash', labels=['bug'], created='2026-01-05'),
                        issue(12, 'Schema support', created='2026-01-02'),
                        issue(10, 'Wrong font', labels=['bug'], created='2026-01-03', association='NONE'),
                        issue(7, 'Chart colours', labels=['bug'], created='2026-01-04'),
                        issue(20, 'Already triaged', labels=['bug', 'priority:P2'], created='2026-01-01')]
        self.answers = {}                   # number -> a model answer (dict), or a CompletedProcess
        self.calls, self.model_calls, self.git_calls, self.out = [], [], [], []
        self.failing = {}                      # a gh call prefix -> the CompletedProcess it returns
        self.events = {}                       # issue number -> pages of its issue events (#77)
        self.unlisted = {}                     # issue number -> event ids the next events read leaves out
        self.next_event = 1000

    # --- fakes ----------------------------------------------------------------------------------
    def gh(self, *args, timeout=None):
        body = None
        if '--body-file' in args:
            body = Path(args[args.index('--body-file') + 1]).read_text(encoding='utf-8')
        self.calls.append((args, body))
        for prefix, result in self.failing.items():
            if args[:len(prefix)] == prefix:
                return result
        if args[:2] == ('repo', 'view'):
            if args[2] in self.missing:
                return done(1, '', f"GraphQL: Could not resolve to a Repository with the name '{args[2]}'.")
            return done(0, json.dumps({'nameWithOwner': args[2]}))
        if args[:2] == ('repo', 'clone'):
            (Path(args[3]) / '.git').mkdir(parents=True)
            return done()
        if args[0] == 'api':
            path = args[1]
            events = re.fullmatch(rf'repos/{PRODUCT}/issues/(\d+)/events\?per_page=100', path)
            if events:
                self.assertEqual(('--paginate', '--slurp'), args[2:4])
                listed = self.events.get(int(events[1]), [[]])
                hidden = self.unlisted.pop(int(events[1]), set())    # GitHub lists a new event late
                return done(0, json.dumps([[e for e in page if e['id'] not in hidden] for page in listed]))
            for repo, issues in [(PRODUCT, self.product), *self.spec.items()]:
                if path.startswith(f'repos/{repo}/issues?'):
                    return done(0, json.dumps([issues]))
                if path.startswith(f'repos/{repo}/issues/'):
                    n = int(path.rsplit('/', 1)[1])
                    return done(0, json.dumps(next(i for i in issues if i['number'] == n)))
        if args[:2] == ('issue', 'create'):
            return done(0, f'https://github.com/{args[args.index("--repo") + 1]}/issues/99\n')
        if args[:2] == ('issue', 'edit') and args[args.index('--repo') + 1] == PRODUCT:
            # GitHub records a labeled/unlabeled event per label change; the kimi ones matter (#77).
            number = int(args[2])
            target = next(i for i in self.product if i['number'] == number)
            for flag, kind in (('--add-label', 'labeled'), ('--remove-label', 'unlabeled')):
                for i, arg in enumerate(args):
                    if arg == flag and args[i + 1] == 'kimi':
                        self.event(number, kind)
                        if kind == 'labeled':
                            target['labels'].append({'name': 'kimi'})
                        else:
                            target['labels'] = [l for l in target['labels'] if l['name'] != 'kimi']
            return done()
        if args[:2] in (('issue', 'edit'), ('issue', 'comment')):
            return done()
        raise AssertionError(f'unexpected gh call {args}')

    def event(self, number, kind, label='kimi'):
        """An issue event on the last page; a new page after every 30 (GitHub's default page size)."""
        pages = self.events.setdefault(number, [[]])
        if len(pages[-1]) >= 30:
            pages.append([])
        self.next_event += 1
        pages[-1].append({'id': self.next_event, 'event': kind, 'label': {'name': label},
                          'created_at': '1970-01-01T00:16:40Z'})            # the triage clock's 1000.0
        return self.next_event

    def git(self, *args, timeout=None):
        self.git_calls.append(args)
        if '--show-toplevel' in args:
            return done(0, args[args.index('--work-tree') + 1] + '\n')
        return done()

    def model(self, argv, cwd):
        facts = json.loads((Path(argv[-1]) / 'facts.json').read_text(encoding='utf-8'))
        number = facts['issue']['number']
        self.model_calls.append((number, argv, cwd, facts))
        value = self.answers.get(number, answer())
        if isinstance(value, BaseException):
            raise value
        return value if isinstance(value, subprocess.CompletedProcess) else envelope(value)

    def triage(self, **kwargs):
        return t.Triage(PRODUCT, self.config, gh=self.gh, git=self.git, model=self.model, claude=['claude'],
                        cache=self.folder / 'cache', state=self.folder / 'state', out=self.out.append,
                        clock=lambda: 1000.0, **kwargs)

    def writes(self):
        return [(args, body) for args, body in self.calls if args[:2] in (('issue', 'edit'), ('issue', 'comment'), ('issue', 'create'))]

    def public(self):
        return [(args, body) for args, body in self.writes() if PRODUCT in args]

    def private(self):
        return [(args, body) for args, body in self.writes() if PRODUCT not in args]

    def labels_written(self):
        return {int(args[2]): args for args, _ in self.public() if args[:2] == ('issue', 'edit')}


class Config(TriageCase):
    def write(self, value):
        path = self.folder / 'config.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def test_a_valid_entry_is_found_by_repo_name_in_any_case(self):
        path = self.write({'bugLabel': 'defect', 'triage': {'Yeroo/Docxy': {'specRepos': [PROJECT], 'model': 'sonnet'}}})
        self.assertEqual(dict(specRepos=[PROJECT], model='sonnet', bugLabel='defect', kimiLabel=False),
                         t.load_config(path, PRODUCT))
        path = self.write({'triage': {PRODUCT: {'specRepos': [PROJECT], 'kimiLabel': True}}})
        self.assertIs(True, t.load_config(path, PRODUCT)['kimiLabel'])

    def test_invalid_entries_are_refused(self):
        for value in ({}, {'triage': []}, {'triage': {'o/other': {'specRepos': [PROJECT]}}},
                      {'triage': {PRODUCT: {'specRepos': []}}}, {'triage': {PRODUCT: {'specRepos': 'x/y'}}},
                      {'triage': {PRODUCT: {'specRepos': ['not a repo']}}},
                      {'triage': {PRODUCT: {'specRepos': [PROJECT, PROJECT.upper()]}}},
                      {'triage': {PRODUCT: {'specRepos': [PROJECT], 'model': 'x; rm'}}},
                      {'bugLabel': 'bug,regression', 'triage': {PRODUCT: {'specRepos': [PROJECT]}}},
                      {'triage': {PRODUCT: {'specRepos': [PROJECT], 'kimiLabel': 'yes'}}},
                      {'triage': {PRODUCT: {'specRepos': [PROJECT], 'kimiLabel': 1}}}):
            with self.subTest(value=value), self.assertRaises(t.ConfigError):
                t.load_config(self.write(value), PRODUCT)

    def test_a_missing_spec_repo_is_skipped_with_a_note(self):
        self.assertEqual(0, self.triage().run_once())
        self.assertIn(f'{EXCEL}: does not exist yet; skipped', self.out)
        self.assertNotIn(('repo', 'clone', EXCEL), [args[:3] for args, _ in self.calls])


class Facts(TriageCase):
    def test_incomplete_facts_write_nothing(self):
        for prefix, result in ((('repo', 'view', WORD), done(1, '', 'error connecting to api.github.com')),
                               (('repo', 'view', WORD), done(1, '', 'HTTP 401: Bad credentials')),
                               (('api', f'repos/{WORD}/issues?state=open&per_page=100'), done(1, '', 'HTTP 502')),
                               (('repo', 'clone', WORD), done(1, '', 'clone failed')),
                               (('api', f'repos/{PRODUCT}/issues?state=open&per_page=100'), done(1, '', 'rate limit'))):
            with self.subTest(prefix=prefix):
                self.calls.clear()
                shutil.rmtree(self.folder / 'cache', ignore_errors=True)
                self.failing = {prefix: result}
                self.assertEqual(t.FactsError.code, self.triage().run_once())
                self.assertEqual([], self.writes())
                self.assertEqual([], self.model_calls)

    def test_a_cached_clone_is_fetched_and_reset(self):
        self.triage().run_once()
        self.git_calls.clear()
        self.calls.clear()
        self.triage().run_once()
        clone = self.folder / 'cache' / 'yeroo' / 'docxy-project-spec'
        pinned = ('--git-dir', str(clone / '.git'), '--work-tree', str(clone))
        self.assertIn(pinned + ('fetch', '--depth', '1', 'origin'), self.git_calls)
        self.assertIn(pinned + ('reset', '--hard', 'FETCH_HEAD'), self.git_calls)
        self.assertIn(pinned + ('clean', '-fdx'), self.git_calls)
        for args in self.git_calls:                  # r21: never git -C (discovery could reach an ancestor)
            self.assertEqual('--git-dir', args[0])
        self.assertNotIn(('repo', 'clone'), [args[:2] for args, _ in self.calls])

    def test_a_damaged_cache_is_cloned_again_and_a_failed_fetch_stops_the_run(self):
        self.triage().run_once()
        clone = self.folder / 'cache' / 'yeroo' / 'docxy-project-spec'
        (clone / 'stale.txt').write_text('x')
        self.git = lambda *args, timeout=None: done(128 if 'rev-parse' in args else 0)
        self.calls.clear()
        self.triage().run_once()
        self.assertIn(('repo', 'clone', PROJECT), [args[:3] for args, _ in self.calls])
        self.assertFalse((clone / 'stale.txt').exists())
        self.git = lambda *args, timeout=None: (done(1, '', 'network down') if 'fetch' in args
                                                else TriageCase.git(self, *args))
        self.calls.clear()
        self.assertEqual(t.FactsError.code, self.triage().run_once())
        self.assertEqual([], self.writes())

    def test_a_git_dir_that_resolves_elsewhere_is_never_fetched_reset_or_cleaned(self):
        # r21: an invalid .git must not let reset --hard / clean -fdx reach an ancestor repository.
        self.triage().run_once()
        seen = []

        def git(*args, timeout=None):
            seen.append(args)
            if '--show-toplevel' in args:
                return done(0, str(self.folder) + '\n')          # an ancestor, not the clone
            return done()
        self.git = git
        self.calls.clear()
        self.triage().run_once()
        self.assertEqual([], [a for a in seen if {'fetch', 'reset', 'clean'} & set(a)])
        self.assertIn(('repo', 'clone', PROJECT), [args[:3] for args, _ in self.calls])

    def test_a_stalled_git_or_a_locked_cache_is_a_facts_error(self):
        # r21: no traceback, nothing written, exit 4.
        self.triage().run_once()

        def stalled(*args, timeout=None):
            if 'fetch' in args:
                raise subprocess.TimeoutExpired('git', 300)
            return self.__class__.git(self, *args, timeout=timeout)
        self.git = stalled
        self.calls.clear()
        self.assertEqual(t.FactsError.code, self.triage().run_once())
        self.assertEqual([], self.writes())
        self.git = lambda *args, timeout=None: done(128)            # damaged: it must be replaced...
        with patch.object(t, 'remove_tree', side_effect=PermissionError('locked file')):
            self.assertEqual(t.FactsError.code, self.triage().run_once())   # ...but cannot be
        self.assertTrue(any('cannot replace the spec cache' in line for line in self.out))

    def test_no_readable_spec_repo_stops_the_run(self):
        # r21 M3: a private repo the gh account cannot see looks exactly like a missing one.
        self.missing = {PROJECT, WORD, EXCEL}
        self.assertEqual(t.FactsError.code, self.triage().run_once())
        self.assertEqual([], self.writes())
        self.assertEqual([], self.model_calls)
        self.assertTrue(any('none of the spec repos could be read' in line for line in self.out))

    def test_the_model_reads_the_clones_under_their_locks(self):
        # r21 m2: a watch session and the conductor's jobs share the cache.
        from conductor import Lock, QueueError
        busy = []
        real = self.model

        def model(argv, cwd):
            for name in ('docxy-project-spec', 'docxy-word-spec'):
                try:
                    with Lock(self.folder / 'cache' / 'yeroo' / f'{name}.lock', timeout=0):
                        busy.append(False)
                except QueueError:
                    busy.append(True)
            return real(argv, cwd)
        self.model = model
        self.triage().run_once(numbers=[10])
        self.assertEqual([True, True], busy)

    def test_a_busy_cache_waits_then_fails_the_run(self):
        from conductor import Lock
        with patch.object(t, 'LOCK_WAIT', 0.1), Lock(self.folder / 'cache' / 'yeroo' / 'docxy-project-spec.lock'):
            self.assertEqual(t.FactsError.code, self.triage().run_once())
        self.assertTrue(any('the spec cache is busy' in line for line in self.out))


class References(unittest.TestCase):
    def test_exact_references_only(self):
        for text, found in (('docxy#10', {10}), ('see yeroo/docxy#10.', {10}), ('(docxy#10)', {10}),
                            ('docxy#100', {100}), ('docxy-word#10', set()), ('other/docxy#10', set()),
                            ('#10', set()), ('mydocxy#10', set()), ('docxy#10x', {10}),
                            ('https://github.com/yeroo/docxy/issues/10', {10}),
                            ('https://github.com/yeroo/docxy/pull/11', {11}),
                            ('https://github.com/yeroo/docxy/issues/100', {100}),
                            ('https://github.com/other/docxy/issues/10', set()),
                            ('Yeroo/DocXY#3', {3})):
            with self.subTest(text=text):
                self.assertEqual(found, t.references(PRODUCT, text))


class Decisions(TriageCase):
    def follow_up(self, number, severity='minor', label='follow-up', association='OWNER', marker=True):
        body = f'Severity: {severity}; origin: review r2\n'
        if marker:
            body += t.PLANNER_MARKER
        item = issue(number, 'deferred finding', body, [label], association=association)
        self.product = [old for old in self.product if old['number'] != number]
        self.product.append(item)
        return item

    def test_minor_follow_ups_are_capped_and_logged(self):
        for number, label in ((31, 'follow-up'), (32, 'follow-up-nested')):
            self.follow_up(number, label=label)
            self.answers[number] = answer('P1', rationale='PRIVATE RATIONALE: SCH-012 must stay private')
            self.assertEqual(0, self.triage().run_once(numbers=[number], retriage=True))
            self.assertIn('priority:P2', self.labels_written()[number])
            self.assertIn('follow-up: capped at P2 (no data loss/crash)', self.public()[-1][1])
            for args, body in self.public():
                public_text = ' '.join(args) + (body or '')
                for secret in SECRET:
                    self.assertNotIn(secret, public_text)
                self.assertNotIn('the model said', public_text)
                if args[:2] == ('issue', 'comment'):
                    self.assertIn(body, t.PUBLIC_COMMENTS)
            self.assertIn('the model said P1', self.private()[-1][1])
            self.assertIn('capped at P2', self.private()[-1][1])

    def test_exception_or_major_severity_keeps_the_grade(self):
        for number, severity, exception, grade in ((33, 'minor', 'data-loss', 'P0'),
                                                   (34, 'major', 'none', 'P1'),
                                                   (35, 'blocker', 'none', 'P1')):
            self.follow_up(number, severity)
            self.answers[number] = answer(grade, exception=exception)
            self.triage().run_once(numbers=[number])
            self.assertIn('priority:' + grade, self.labels_written()[number])
            self.assertNotIn('capped at P2', self.public()[-1][1])

    def test_unknown_or_untrusted_severity_is_capped(self):
        for number, severity, association, marker in ((36, 'nonsense', 'OWNER', True),
                                                      (37, 'major', 'NONE', True),
                                                      (38, 'major', 'OWNER', False)):
            self.follow_up(number, severity, association=association, marker=marker)
            self.answers[number] = answer('P1')
            self.triage().run_once(numbers=[number])
            self.assertIn('priority:P2', self.labels_written()[number])

    def test_low_grade_has_no_cap_note(self):
        self.follow_up(39)
        self.answers[39] = answer('P3')
        self.triage().run_once(numbers=[39])
        self.assertNotIn('capped at P2', self.public()[-1][1])
        self.assertNotIn('capped at P2', self.private()[-1][1])

    def test_deterministic_p0_requires_an_exception_for_minor_follow_up(self):
        self.follow_up(100)
        self.answers[100] = answer('P0')
        self.triage().run_once(numbers=[100])
        self.assertEqual(True, self.model_calls[-1][3]['deterministicP0'])
        self.assertIn('priority:P2', self.labels_written()[100])
        self.assertIn('deterministic uncapped grade P0', self.private()[-1][1])
        self.answers[100] = answer('P0', exception='crash')
        self.triage().run_once(numbers=[100], retriage=True)
        self.assertIn('priority:P0', self.labels_written()[100])

    def test_floor_and_cap_log_both_uncapped_grades(self):
        self.follow_up(12)
        self.answers[12] = answer('P3')
        self.triage().run_once(numbers=[12])
        self.assertIn('priority:P2', self.labels_written()[12])
        self.assertIn('the model said P3, uncapped grade P1', self.private()[-1][1])

    def test_dry_run_shows_the_cap_without_writing(self):
        self.follow_up(42)
        self.answers[42] = answer('P1')
        self.assertEqual(0, self.triage(dry_run=True).run_once(numbers=[42]))
        self.assertTrue(any('#42: would label priority:P2' in line and 'capped at P2' in line
                            for line in self.out))
        self.assertEqual([], self.writes())

    def test_follow_up_selector_retriages_labelled_issues(self):
        self.follow_up(41)
        self.product[-1]['labels'].append({'name': 'priority:P1'})
        self.answers[41] = answer('P1')
        self.triage().run_once(retriage=True, follow_ups=True)
        self.assertEqual([41], [int(a[2]) for a, _ in self.public() if a[:2] == ('issue', 'edit')])

    def test_a_bug_mirrored_in_a_spec_repo_is_p0_without_the_model(self):
        self.triage().run_once(numbers=[100])
        self.assertEqual([], self.model_calls)
        self.assertIn('priority:P0', self.labels_written()[100])
        log = self.private()[-1][1]
        self.assertIn('source: deterministic', log)
        self.assertIn(f'{PROJECT}#1', log)

    def test_a_referenced_bug_is_p0_and_a_referenced_feature_gets_a_p1_floor(self):
        self.triage().run_once(numbers=[7])               # a bug named by a spec area by URL
        self.assertEqual([], self.model_calls)
        self.assertIn('priority:P0', self.labels_written()[7])
        self.answers[12] = answer('P3')                    # a feature named by an epic: the model may not go below P1
        self.triage().run_once(numbers=[12])
        self.assertEqual('P1', self.model_calls[-1][3]['floor'])
        self.assertIn('priority:P1', self.labels_written()[12])
        self.assertIn('raised to the floor P1', self.private()[-1][1])
        self.answers[12] = answer('P0')                    # ...and may raise it
        self.calls.clear()
        self.triage().run_once(numbers=[12], retriage=True)
        self.assertIn('priority:P0', self.labels_written()[12])

    def test_a_model_only_p0_from_an_outside_author_is_written_as_p1(self):
        self.answers[10] = answer('P0', ux=True)
        self.triage().run_once(numbers=[10])
        self.assertEqual(None, self.model_calls[-1][3]['floor'])            # docxy-word#10 is not docxy#10
        self.assertEqual('NONE', self.model_calls[-1][3]['issue']['authorAssociation'])
        self.assertIn('priority:P1', self.labels_written()[10])
        self.assertIn('an author outside the repo (NONE); written as P1', self.private()[-1][1])
        self.product[2]['author_association'] = 'COLLABORATOR'
        self.calls.clear()
        self.triage().run_once(numbers=[10], retriage=True)
        self.assertIn('priority:P0', self.labels_written()[10])

    def test_the_model_sees_untrusted_public_text_as_data(self):
        self.product[3]['labels'] = []                     # #7, not a bug: goes to the model
        self.triage().run_once(numbers=[7])
        number, argv, cwd, facts = self.model_calls[-1]
        self.assertIn('untrusted', facts['note'])
        self.assertIn('untrusted', argv[argv.index('-p') + 1])
        self.assertEqual([f'{WORD}#5'], [r['ref'] for r in facts['referencingSpecIssues']])
        self.assertEqual({PROJECT, WORD}, {s['repo'] for s in facts['specRepos']})


class ModelContract(TriageCase):
    BAD = {
        'not json': done(0, json.dumps({'is_error': False, 'result': 'I think P1.'})),
        'two objects': done(0, json.dumps({'is_error': False, 'result': '{"priority": "P1"} {"x": 1}'})),
        'extra key': envelope(dict(answer(), confidence=0.9)),
        'missing key': envelope({'priority': 'P1', 'ux': False, 'rationale': 'x'}),
        'missing exception': envelope({k: v for k, v in answer().items() if k != 'exception'}),
        'unknown exception': envelope(answer(exception='other')),
        'bad priority': envelope(answer('P5')),
        'ux as string': envelope(dict(answer(), ux='yes')),
        'empty rationale': envelope(answer(rationale=' ')),
        'long rationale': envelope(answer(rationale='x' * 2001)),
        'unknown spec ref': envelope(answer(refs=['yeroo/docxy-project-spec#77'])),
        'malformed spec ref': envelope(answer(refs=['SCH-012'])),
        'is_error': done(0, json.dumps({'is_error': True, 'result': 'something broke'})),
        'exit code': done(1, '', 'crashed'),
        'timeout': subprocess.TimeoutExpired('claude', 300),
    }

    def setUp(self):
        super().setUp()
        self.product.append(issue(30, 'Unreferenced', created='2026-01-06'))

    def test_malformed_output_fails_that_issue_only(self):
        for case, value in self.BAD.items():
            with self.subTest(case=case):
                self.calls.clear()
                self.answers = {10: value, 30: answer('P2')}
                self.assertEqual(1, self.triage().run_once(numbers=[10, 30]))
                self.assertNotIn(10, self.labels_written())                 # nothing written for it
                self.assertNotIn(10, [int(a[2]) for a, _ in self.public()])
                self.assertIn('priority:P2', self.labels_written()[30])       # the next one goes on
                self.assertTrue(any(line.startswith('#10: FAILED, nothing written') for line in self.out))

    def test_accepted_forms(self):
        for case, value in (('structured', envelope(answer('P3', refs=[f'{PROJECT}#2']))),
                            ('result text', done(0, json.dumps({'is_error': False, 'result': json.dumps(answer('P3'))}))),
                            ('fenced', done(0, json.dumps({'is_error': False,
                                                           'result': '```json\n' + json.dumps(answer('P3')) + '\n```'})))):
            with self.subTest(case=case):
                self.calls.clear()
                self.answers = {10: value}
                self.assertEqual(0, self.triage().run_once(numbers=[10], retriage=True))
                self.assertIn('priority:P3', self.labels_written()[10])

    def test_a_usage_limit_or_auth_failure_stops_the_run(self):
        for text in ("Claude AI usage limit reached|1760000000", 'Invalid API key · Please run /login'):
            with self.subTest(text=text):
                self.calls.clear()
                self.model_calls.clear()
                self.answers = {10: done(1, json.dumps({'is_error': True, 'result': text}))}
                self.assertEqual(t.StopRun.code, self.triage().run_once(numbers=[10, 30]))
                self.assertEqual([10], [n for n, *_ in self.model_calls])   # #30 is not tried
                self.assertEqual([], self.writes())


class Writing(TriageCase):
    def test_nothing_private_reaches_the_public_repo(self):
        self.answers = {10: answer('P1', ux=True, refs=[f'{PROJECT}#2']), 12: answer('P2')}
        self.triage().run_once()
        public = self.public()
        self.assertTrue(public)
        for args, body in public:
            text = ' '.join(args) + (body or '')
            for secret in SECRET:
                self.assertNotIn(secret, text)
            if args[:2] == ('issue', 'comment'):
                self.assertIn(body, t.PUBLIC_COMMENTS)
        for body in t.PUBLIC_COMMENTS:
            self.assertIn(t.PLANNER_MARKER, body)
            self.assertIn(t.TRIAGE_MARKER, body)
        self.assertTrue(any('PRIVATE RATIONALE' in (body or '') for _, body in self.private()))

    def test_the_private_log_is_written_before_anything_public(self):
        # r21 m1: private log, then the label, then the comment.
        self.triage().run_once(numbers=[100])
        order = [('log' if PRODUCT not in args else args[1]) for args, _ in self.writes()]
        self.assertEqual(['log', 'log', 'edit', 'comment'], order)          # create the log, comment, label, comment

    def test_a_failed_private_log_writes_nothing_public(self):
        self.failing = {('issue', 'create'): done(1, '', 'HTTP 502')}
        self.assertEqual(1, self.triage().run_once(numbers=[100]))
        self.assertEqual([], self.public())
        self.assertTrue(any(line.startswith('#100: FAILED, nothing written') for line in self.out))

    def test_a_failed_public_comment_after_the_label_is_a_partial_write(self):
        self.failing = {('issue', 'comment', '100'): done(1, '', 'HTTP 502')}
        results = {}
        self.assertEqual(1, self.triage().run_once(numbers=[100], results=results))
        self.assertIn('priority:P0', self.labels_written()[100])
        self.assertEqual(dict(priority='P0', written=True), {k: results['100'][k] for k in ('priority', 'written')})
        self.assertTrue(any(line.startswith('#100: labelled priority:P0, but the public comment failed')
                            for line in self.out))

    def test_the_log_issue_is_created_once_in_the_first_spec_repo(self):
        self.answers = {10: answer('P2'), 12: answer('P2')}
        self.triage().run_once()
        creates = [args for args, _ in self.private() if args[:2] == ('issue', 'create')]
        self.assertEqual([('issue', 'create', '--repo', PROJECT, '--title', 'Triage log')], [a[:6] for a in creates])
        comments = [args for args, _ in self.private() if args[:2] == ('issue', 'comment')]
        self.assertEqual({('issue', 'comment', '99', '--repo', PROJECT)}, {a[:5] for a in comments})
        self.assertEqual(4, len(comments))                                  # #12, #10, #7, #100

    def test_an_existing_log_issue_is_used_and_never_counts_as_a_reference(self):
        self.spec[PROJECT].append(issue(40, 'Triage log', f'{t.LOG_MARKER}\nyeroo/docxy#10 was logged here'))
        self.triage().run_once(numbers=[10])
        self.assertEqual(None, self.model_calls[-1][3]['floor'])
        self.assertNotIn(f'{PROJECT}#40', [i['ref'] for s in self.model_calls[-1][3]['specRepos'] for i in s['openIssues']])
        self.assertEqual([('issue', 'comment', '40', '--repo', PROJECT)], [a[:5] for a, _ in self.private()])

    def test_only_untriaged_issues_oldest_first_up_to_the_limit(self):
        self.triage().run_once(limit=2)
        self.assertEqual([12, 10], [int(a[2]) for a, _ in self.public() if a[:2] == ('issue', 'edit')])
        self.assertNotIn(20, self.labels_written())

    def test_retriage_replaces_the_priority_label(self):
        self.answers[20] = answer('P3')
        self.triage().run_once(numbers=[20])
        self.assertNotIn(20, self.labels_written())                          # labelled: left alone
        self.triage().run_once(numbers=[20], retriage=True)
        edit = self.labels_written()[20]
        self.assertEqual(('--add-label', 'priority:P3', '--remove-label', 'priority:P2'), edit[5:])

    def test_retriage_removes_ux_when_it_is_no_longer_the_reason(self):
        self.product[4]['labels'].append({'name': 'ux'})
        self.answers[20] = answer('P2', ux=False)
        self.triage().run_once(numbers=[20], retriage=True)
        self.assertIn('--remove-label', self.labels_written()[20])
        self.assertEqual('ux', self.labels_written()[20][-1])

    def test_a_label_added_meanwhile_wins(self):
        triage = self.triage()
        real = self.gh

        def gh(*args, timeout=None):
            if args[:2] == ('api', f'repos/{PRODUCT}/issues/10'):
                self.calls.append((args, None))
                return done(0, json.dumps(issue(10, labels=['bug', 'priority:P0'])))
            return real(*args, timeout=timeout)
        triage.gh = gh
        self.assertEqual(0, triage.run_once(numbers=[10]))
        self.assertEqual([], self.writes())
        self.assertIn('#10: labelled priority:P0 meanwhile; nothing written', self.out)

    def test_dry_run_writes_nothing(self):
        self.assertEqual(0, self.triage(dry_run=True).run_once())
        self.assertEqual([], self.writes())
        self.assertTrue(any(line.startswith('#100: would label priority:P0 (deterministic)') for line in self.out))
        self.assertFalse((self.folder / 'state' / 'yeroo' / 'docxy.json').exists())


def kimi_answer(priority='P2', suitable=True, reason='one crate, fixtures in tests/', rule=None, **kwargs):
    rule = rule or ('narrow-fix' if suitable else 'not-narrow')
    return dict(answer(priority, **kwargs), kimiSuitable=suitable, kimiRule=rule, kimiReason=reason)


class KimiLabel(TriageCase):
    """#77: with "kimiLabel": true triage also judges whether an issue suits Kimi and sets or clears the
    `kimi` label; P0/P1 never suits; a human's kimi label wins; --kimi-only judges nothing else."""

    def setUp(self):
        super().setUp()
        self.config['kimiLabel'] = True
        self.product.append(issue(30, 'Unreferenced', created='2026-01-06'))     # no spec floor
        self.answers = {n: kimi_answer() for n in (7, 10, 12, 20, 30, 100)}

    def kimi_change(self, number):
        for args, _ in self.public():
            if args[:2] == ('issue', 'edit') and int(args[2]) == number and 'kimi' in args:
                return args[args.index('kimi') - 1]
        return None

    def log_for(self, number):
        return [body for args, body in self.private() if args[:2] == ('issue', 'comment') and f'{PRODUCT}#{number} ' in body]

    def test_the_schema_and_prompt_ask_for_kimi_only_when_on(self):
        self.triage().run_once(numbers=[12])
        argv, facts = self.model_calls[-1][1], self.model_calls[-1][3]
        schema = json.loads(argv[argv.index('--json-schema') + 1])
        self.assertEqual([*t.SCHEMA['required'], 'kimiSuitable', 'kimiRule', 'kimiReason'], schema['required'])
        self.assertEqual([*t.KIMI_ALLOWED, *t.KIMI_EXCLUDED], schema['properties']['kimiRule']['enum'])
        self.assertEqual((True, False), (facts['kimiLabel'], facts['kimiOnly']))
        self.config['kimiLabel'] = False
        self.answers[12] = answer()
        self.triage().run_once(numbers=[12], retriage=True)
        argv, facts = self.model_calls[-1][1], self.model_calls[-1][3]
        self.assertEqual(t.SCHEMA, json.loads(argv[argv.index('--json-schema') + 1]))
        self.assertFalse(facts['kimiLabel'])
        self.assertIsNone(self.kimi_change(12))

    def test_an_answer_without_the_kimi_fields_fails_the_issue(self):
        self.answers[12] = answer('P2')
        self.assertEqual(1, self.triage().run_once(numbers=[12]))
        self.assertEqual([], self.public())

    def test_suitable_adds_kimi_and_the_reason_stays_private(self):
        self.answers[30] = kimi_answer('P2', reason='PRIVATE RATIONALE: one crate')
        self.assertEqual(0, self.triage().run_once(numbers=[30]))
        self.assertEqual('--add-label', self.kimi_change(30))
        [log] = self.log_for(30)
        self.assertIn('kimi: yes (narrow-fix) - PRIVATE RATIONALE: one crate', log)
        for args, body in self.public():
            self.assertNotIn('PRIVATE RATIONALE', ' '.join(args) + (body or ''))
            if args[:2] == ('issue', 'comment'):
                self.assertIn(body, t.PUBLIC_COMMENTS)
        self.assertEqual({'30': [1001]}, json.loads((self.folder / 'state/yeroo/docxy.kimi.json').read_text())['issues'])

    def test_unsuitable_removes_a_kimi_label_triage_set(self):
        self.triage().run_once(numbers=[30])                             # triage sets it
        self.calls.clear()
        self.answers[30] = kimi_answer('P3', suitable=False, reason='touches the save path', rule='save-path')
        self.assertEqual(0, self.triage().run_once(numbers=[30], retriage=True))
        self.assertEqual('--remove-label', self.kimi_change(30))
        self.assertIn('kimi: no (save-path) - touches the save path', self.log_for(30)[0])
        self.assertEqual([1001, 1002], json.loads((self.folder / 'state/yeroo/docxy.kimi.json').read_text())['issues']['30'])

    def test_an_own_event_github_lists_late_is_still_ours(self):
        real = self.event

        def late(number, kind, label='kimi'):
            made = real(number, kind, label)
            if label == 'kimi':
                self.unlisted[number] = {made}          # missing from the read-back right after the edit
            return made
        self.event = late
        self.triage().run_once(numbers=[30])
        self.assertEqual('--add-label', self.kimi_change(30))
        self.assertIn('#30: kimi label labeled; its event is not listed yet, recorded as pending', self.out)
        state = json.loads((self.folder / 'state/yeroo/docxy.kimi.json').read_text())
        self.assertEqual({'30': [{'action': 'labeled', 'at': 1000.0}]}, state['pending'])
        self.event = real
        self.calls.clear()
        self.answers[30] = kimi_answer('P3', suitable=False, reason='touches the save path')
        self.assertEqual(0, self.triage().run_once(numbers=[30], retriage=True))
        self.assertEqual('--remove-label', self.kimi_change(30))          # ours, not a human's
        state = json.loads((self.folder / 'state/yeroo/docxy.kimi.json').read_text())
        self.assertEqual(({'30': [1001, 1002]}, {}), (state['issues'], state['pending']))

    def test_p0_and_p1_are_never_suitable_whatever_the_model_says(self):
        self.product[4]['labels'].append({'name': 'kimi'})               # #20 was kimi (set by triage)
        self.events[20] = [[{'id': 1, 'event': 'labeled', 'label': {'name': 'kimi'}}]]
        (self.folder / 'state/yeroo').mkdir(parents=True)
        (self.folder / 'state/yeroo/docxy.kimi.json').write_text(json.dumps({'issues': {'20': [1]}}))
        self.answers[20] = kimi_answer('P1', suitable=True)
        self.assertEqual(0, self.triage().run_once(numbers=[20], retriage=True))
        self.assertEqual('--remove-label', self.kimi_change(20))
        self.assertIn('kimi: no (p0-p1) - P1 is never Kimi work', self.log_for(20)[0])
        self.answers[12] = kimi_answer('P0', suitable=True)
        self.triage().run_once(numbers=[12])
        self.assertIsNone(self.kimi_change(12))                           # not present: nothing to remove
        self.assertIn('kimi: no (p0-p1) - P0 is never Kimi work', self.log_for(12)[0])

    def test_the_deterministic_path_is_never_suitable(self):
        # #100 is a bug an open spec bug references: P0 without the model.
        self.assertEqual(0, self.triage().run_once(numbers=[100]))
        self.assertEqual([], [n for n, *_ in self.model_calls])
        self.assertIn(f'kimi: no (p0-p1) - {t.DETERMINISTIC_NOT_KIMI}', self.log_for(100)[0])
        self.assertIsNone(self.kimi_change(100))

    def test_a_human_kimi_label_wins(self):
        # Set by hand (triage never recorded it): left alone although the model says unsuitable.
        self.product[5]['labels'].append({'name': 'kimi'})
        self.event(30, 'labeled')
        self.answers[30] = kimi_answer('P2', suitable=False)
        self.assertEqual(0, self.triage().run_once(numbers=[30]))
        self.assertIsNone(self.kimi_change(30))
        self.assertIn('kimi: human override (labeled by hand); left alone', self.log_for(30)[0])

    def test_a_human_removal_wins_even_on_page_two(self):
        # Triage set it, the human removed it later; 40 other events push the removal to the second page.
        self.triage().run_once(numbers=[30])
        for _ in range(40):
            self.event(30, 'labeled', label='other')
        self.event(30, 'unlabeled')
        self.product[5]['labels'] = [l for l in self.product[5]['labels'] if l['name'] != 'kimi']
        self.assertEqual(2, len(self.events[30]))
        self.calls.clear()
        self.assertEqual(0, self.triage().run_once(numbers=[30], retriage=True))
        self.assertIsNone(self.kimi_change(30))
        self.assertIn('kimi: human override (unlabeled by hand); left alone', self.log_for(30)[0])

    def test_kimi_only_changes_no_priority_and_posts_no_public_comment(self):
        self.product[1]['labels'].append({'name': 'priority:P2'})        # #12 P2, #20 P2, the rest untriaged
        self.answers[12] = {'kimiSuitable': True, 'kimiRule': 'narrow-fix', 'kimiReason': 'narrow fix'}
        self.answers[20] = {'kimiSuitable': False, 'kimiRule': 'multi-crate', 'kimiReason': 'spans the UI and the harness'}
        self.assertEqual(0, self.triage().run_once(kimi_only=True))
        self.assertEqual([12, 20], sorted(n for n, *_ in self.model_calls))
        argv = self.model_calls[0][1]
        self.assertEqual(['kimiSuitable', 'kimiRule', 'kimiReason'],
                         json.loads(argv[argv.index('--json-schema') + 1])['required'])
        self.assertTrue(self.model_calls[0][3]['kimiOnly'])
        public = self.public()
        self.assertEqual([('issue', 'edit', '12', '--repo', PRODUCT, '--add-label', 'kimi')], [args for args, _ in public])
        self.assertIn('#12: kimi: yes (narrow-fix) - narrow fix', self.out)
        self.assertIn('#20: kimi: no (multi-crate) - spans the UI and the harness', self.out)
        self.assertIn('kimi: no (multi-crate) - spans the UI and the harness', self.log_for(20)[0])

    def test_kimi_only_refuses_a_product_without_the_label(self):
        self.config['kimiLabel'] = False
        with self.assertRaises(t.ConfigError):
            self.triage().run_once(kimi_only=True)
        path = self.folder / 'config.json'
        path.write_text(json.dumps({'triage': {PRODUCT: {'specRepos': [PROJECT]}}}), encoding='utf-8')
        with patch.dict(os.environ, {'AGWORKBENCH_CONFIG': str(path)}), patch('sys.stderr') as err:
            self.assertEqual(2, t.main(['run', '--repo', PRODUCT, '--kimi-only']))
        self.assertIn('kimiLabel', ''.join(str(c.args[0]) for c in err.write.call_args_list))

    def test_dry_run_shows_the_kimi_verdict_and_writes_nothing(self):
        self.assertEqual(0, self.triage(dry_run=True).run_once(numbers=[30]))
        self.assertEqual([], self.writes())
        self.assertTrue(any('[kimi: yes (narrow-fix) - one crate, fixtures in tests/]' in line for line in self.out),
                        self.out)

    # #82: the loosened rules, each verdict named by its rule ----------------------------------------
    def test_single_area_leftovers_gets_kimi(self):
        self.product.append(issue(31, 'Leftovers from #12: table polish', created='2026-01-07'))
        self.answers[31] = kimi_answer('P3', rule='leftovers-one-area', reason='every item in docxy-table')
        self.assertEqual(0, self.triage().run_once(numbers=[31]))
        self.assertEqual('--add-label', self.kimi_change(31))
        self.assertIn('kimi: yes (leftovers-one-area) - every item in docxy-table', self.log_for(31)[0])

    def test_save_path_leftovers_does_not(self):
        self.product.append(issue(31, 'Leftovers from #12: save polish', created='2026-01-07'))
        self.answers[31] = kimi_answer('P3', rule='leftovers-one-area')
        self.triage().run_once(numbers=[31])                             # triage labelled it earlier
        self.calls.clear()
        self.answers[31] = kimi_answer('P3', suitable=False, rule='save-path', reason='item 2 writes the docx')
        self.assertEqual(0, self.triage().run_once(numbers=[31], retriage=True))
        self.assertEqual('--remove-label', self.kimi_change(31))
        self.assertIn('kimi: no (save-path) - item 2 writes the docx', self.log_for(31)[0])
        self.product.append(issue(32, 'Leftovers from #14: save polish', created='2026-01-08'))
        self.answers[32] = kimi_answer('P2', suitable=False, rule='save-path')
        self.assertEqual(0, self.triage().run_once(numbers=[32]))
        self.assertIsNone(self.kimi_change(32))

    def test_two_crate_harness_verb_gets_kimi(self):
        self.product.append(issue(31, 'uiharness: a select-cell verb', created='2026-01-07'))
        self.answers[31] = kimi_answer('P2', rule='harness-two-crates', reason='uiharness + the app host side')
        self.assertEqual(0, self.triage().run_once(numbers=[31]))
        self.assertEqual('--add-label', self.kimi_change(31))

    def test_suitable_under_an_excluded_rule_is_not_labelled(self):
        self.answers[30] = kimi_answer('P2', suitable=True, rule='save-path', reason='small, but on save')
        self.assertEqual(0, self.triage().run_once(numbers=[30]))
        self.assertIsNone(self.kimi_change(30))
        self.assertIn('kimi: no (save-path) - model contradicted itself: said suitable under excluded rule '
                      'save-path: small, but on save', self.log_for(30)[0])

    def test_unsuitable_under_an_allowed_rule_stays_unsuitable(self):
        self.triage().run_once(numbers=[30])                             # triage set it
        self.calls.clear()
        self.answers[30] = kimi_answer('P2', suitable=False, rule='narrow-fix', reason='unsure')
        self.assertEqual(0, self.triage().run_once(numbers=[30], retriage=True))
        self.assertEqual('--remove-label', self.kimi_change(30))
        self.assertIn('kimi: no (narrow-fix) - model contradicted itself: said unsuitable under allowed rule '
                      'narrow-fix: unsure', self.log_for(30)[0])

    def test_a_contradiction_is_resolved_in_the_dry_run_too(self):
        self.product[1]['labels'].append({'name': 'priority:P2'})
        self.answers[12] = {'kimiSuitable': True, 'kimiRule': 'outside-format', 'kimiReason': 'x'}
        self.assertEqual(0, self.triage(dry_run=True).run_once(numbers=[12], kimi_only=True))
        self.assertIn('#12: would judge kimi: no (outside-format) - model contradicted itself: said suitable '
                      'under excluded rule outside-format: x', self.out)

    def test_an_unknown_rule_fails_the_issue(self):
        self.answers[30] = kimi_answer('P2', rule='looks-easy')
        self.assertEqual(1, self.triage().run_once(numbers=[30]))
        self.assertEqual([], self.public())
        self.assertTrue(any("invalid kimiRule 'looks-easy'" in line for line in self.out), self.out)

    def test_the_log_line_names_the_rule(self):
        self.answers[30] = kimi_answer('P2', rule='ui-single-view', reason='the ribbon tooltip only')
        self.triage().run_once(numbers=[30])
        self.assertIn('kimi: yes (ui-single-view) - the ribbon tooltip only', self.log_for(30)[0].splitlines())

    def test_an_untriaged_issue_now_is_not_p2_p3(self):
        # --kimi-only picked it as P2; its priority label was gone by the time the label is written.
        self.product[1]['labels'].append({'name': 'priority:P2'})
        triage = self.triage()
        real, reads = self.gh, []

        def gh(*args, timeout=None):
            if args[:2] == ('api', f'repos/{PRODUCT}/issues/12'):
                reads.append(args)
                if len(reads) > 1:                                       # the read before the write
                    self.calls.append((args, None))
                    return done(0, json.dumps(issue(12)))
            return real(*args, timeout=timeout)
        triage.gh = gh
        self.answers[12] = {'kimiSuitable': True, 'kimiRule': 'narrow-fix', 'kimiReason': 'narrow fix'}
        self.assertEqual(0, triage.run_once(numbers=[12], kimi_only=True))
        self.assertIsNone(self.kimi_change(12))
        self.assertIn('#12: kimi: no (not-p2-p3) - untriaged now, never Kimi work (narrow fix)', self.out)

    def test_kimi_only_prints_the_summary(self):
        # #12 labelled, #20 cleared (triage set it), #30 kept (a human's label), #7 unchanged, #10 failed.
        for n in (7, 10, 12, 20, 30):
            [i] = [i for i in self.product if i['number'] == n]
            i['labels'] = [l for l in i['labels'] if not l['name'].startswith('priority:')] + [{'name': 'priority:P2'}]
        self.product[4]['labels'].append({'name': 'kimi'})
        self.events[20] = [[{'id': 1, 'event': 'labeled', 'label': {'name': 'kimi'}}]]
        (self.folder / 'state/yeroo').mkdir(parents=True)
        (self.folder / 'state/yeroo/docxy.kimi.json').write_text(json.dumps({'issues': {'20': [1]}}))
        self.product[5]['labels'].append({'name': 'kimi'})
        self.event(30, 'labeled')                                        # by hand
        yes = {'kimiSuitable': True, 'kimiRule': 'narrow-fix', 'kimiReason': 'r'}
        no = {'kimiSuitable': False, 'kimiRule': 'multi-crate', 'kimiReason': 'r'}
        self.answers.update({7: no, 10: done(0, 'garbage'), 12: yes, 20: no, 30: no})
        self.assertEqual(1, self.triage().run_once(numbers=[7, 10, 12, 20, 30], kimi_only=True))
        self.assertEqual('kimi: 1 labelled, 1 cleared, 1 kept (human), 1 unchanged, 1 failed', self.out[-1])
        self.out.clear()
        self.answers[10] = yes
        self.assertEqual(0, self.triage().run_once(numbers=[10, 30], kimi_only=True))
        self.assertEqual('kimi: 1 labelled, 0 cleared, 1 kept (human)', self.out[-1])
        self.out.clear()
        self.assertEqual(0, self.triage(dry_run=True).run_once(numbers=[7, 12, 20], kimi_only=True))
        self.assertEqual('kimi (dry run): 1 would be yes, 2 would be no', self.out[-1])

    def test_a_full_run_prints_no_kimi_summary(self):
        self.triage().run_once(numbers=[30])
        self.assertFalse(any(line.startswith('kimi:') or line.startswith('kimi (') for line in self.out), self.out)


class Watch(TriageCase):
    def test_a_failing_issue_backs_off_and_is_given_up_after_three(self):
        notes = []
        clock = [1000.0]
        self.answers = {10: done(0, 'garbage'), 12: answer('P2')}
        triage = t.Triage(PRODUCT, self.config, gh=self.gh, git=self.git, model=self.model, claude=['claude'],
                          cache=self.folder / 'cache', state=self.folder / 'state', out=self.out.append,
                          clock=lambda: clock[0], notify=notes.append)
        tried = []
        for step in range(8):
            self.model_calls.clear()
            triage.run_once(watch=True)
            tried.append([n for n, *_ in self.model_calls if n == 10])
            clock[0] += t.BACKOFF * 4
        self.assertEqual(3, sum(len(x) for x in tried))                     # three tries, then left alone
        self.assertEqual(1, len(notes))
        self.assertIn('given up after 3 failures', notes[0])
        failures = json.loads((self.folder / 'state' / 'yeroo' / 'docxy.json').read_text(encoding='utf-8'))['failures']
        self.assertEqual(3, failures['10']['count'])

    def test_backoff_skips_an_issue_until_it_is_due(self):
        clock = [1000.0]
        self.answers = {10: done(0, 'garbage')}
        triage = self.triage()
        triage.clock = lambda: clock[0]
        triage.run_once(numbers=None, watch=True)
        self.model_calls.clear()
        triage.run_once(watch=True)
        self.assertNotIn(10, [n for n, *_ in self.model_calls])
        clock[0] += t.BACKOFF
        triage.run_once(watch=True)
        self.assertIn(10, [n for n, *_ in self.model_calls])


class WatchSession(TriageCase):
    def test_a_manual_run_never_counts_toward_the_watch_give_up(self):
        # r21: only -Watch counts failures and backs off.
        self.product.append(issue(30, 'Unreferenced', created='2026-01-06'))
        self.answers = {30: done(0, 'garbage')}
        for _ in range(4):
            self.triage().run_once(numbers=[30])
        self.assertFalse((self.folder / 'state' / 'yeroo' / 'docxy.json').exists())

    def test_nothing_in_one_scan_ends_the_watch(self):
        notes, sleeps = [], []
        triage = self.triage(notify=notes.append)
        with patch.object(triage, 'run_once', side_effect=[OSError('disk gone'), t.ConfigError('claude is not on PATH'), 0]):
            triage.watch(interval=300, sleep=sleeps.append, rounds=3)
        self.assertEqual([300, t.STOP_PAUSE, 300], sleeps)
        self.assertIn('scan failed: disk gone', notes[0])


class HeadlessCall(TriageCase):
    def test_the_argv_is_read_only_and_pinned(self):
        self.config['model'] = 'sonnet'
        self.triage().run_once(numbers=[10])
        _, argv, cwd, _ = self.model_calls[-1]
        state = self.folder / 'state' / 'claude'
        project = self.folder / 'cache' / 'yeroo' / 'docxy-project-spec'
        word = self.folder / 'cache' / 'yeroo' / 'docxy-word-spec'
        self.assertEqual(str(project), cwd)
        self.assertEqual(['claude', '-p'], argv[:2])
        self.assertEqual(['--restricted', '--tools', 'Read,Grep,Glob', '--strict-mcp-config',
                          '--mcp-config', str(state / 'mcp.json'), '--settings', str(state / 'settings.json'),
                          '--no-session-persistence', '--output-format', 'json',
                          '--json-schema', json.dumps(t.SCHEMA, separators=(',', ':')),
                          '--model', 'sonnet', '--add-dir', str(word), '--add-dir'], argv[3:-1])
        self.assertEqual({'promptSuggestionEnabled': False}, json.loads((state / 'settings.json').read_text()))
        self.assertEqual({'mcpServers': {}}, json.loads((state / 'mcp.json').read_text()))
        prompt = argv[2]
        self.assertIn(f'Facts file: {Path(argv[-1]) / "facts.json"}', prompt)
        self.assertNotIn('argument-hint', prompt)                            # the frontmatter is stripped
        self.assertNotIn('$ARGUMENTS', prompt)
        self.assertFalse(Path(argv[-1]).exists())                            # the facts are removed afterwards

    def test_a_batch_shim_is_refused(self):
        with patch.object(t.shutil, 'which', return_value=r'C:\npm\claude.cmd'):
            with self.assertRaises(t.ConfigError):
                t.find_claude()
        with patch.object(t.shutil, 'which', return_value=r'C:\bin\claude.exe'):
            self.assertEqual([r'C:\bin\claude.exe'], t.find_claude())

    def test_the_real_process_boundary_passes_every_argument_intact(self):
        # A python stub stands in for claude.exe: it records its argv and answers with an envelope.
        dump = self.folder / 'argv.json'
        stub = self.folder / 'claude_stub.py'
        stub.write_text('import json, sys\n'
                        f'json.dump(sys.argv[1:], open(r"{dump}", "w", encoding="utf-8"))\n'
                        'print(json.dumps({"is_error": False, "structured_output": '
                        '{"priority": "P2", "ux": False, "rationale": "r", "specRefs": [], '
                        '"exception": "none"}}))\n', encoding='utf-8')
        triage = t.Triage(PRODUCT, self.config, gh=self.gh, git=self.git, model=t.real_model,
                          claude=[sys.executable, str(stub)], cache=self.folder / 'cache',
                          state=self.folder / 'state', out=self.out.append)
        self.assertEqual(0, triage.run_once(numbers=[10]))
        received = json.loads(dump.read_text(encoding='utf-8'))
        self.assertEqual(json.dumps(t.SCHEMA, separators=(',', ':')), received[received.index('--json-schema') + 1])
        self.assertIn('"priority"', received[1])                             # the prompt, quotes and newlines intact
        self.assertIn('\n', received[1])
        self.assertIn('priority:P2', self.labels_written()[10])


class Processes(unittest.TestCase):
    def test_a_timeout_stops_the_child_tree(self):
        with patch.object(t.subprocess, 'Popen') as spawn, patch.object(t.subprocess, 'run') as kill:
            spawn.return_value.pid = 321
            spawn.return_value.communicate.side_effect = [subprocess.TimeoutExpired('claude', 300), (b'', b'')]
            with self.assertRaises(subprocess.TimeoutExpired):
                t.run(['claude', '-p', 'x'], timeout=300)
        if os.name == 'nt':
            self.assertEqual(['taskkill', '/PID', '321', '/T', '/F'], kill.call_args.args[0])
        else:
            spawn.return_value.kill.assert_called_once()


class Priorities(unittest.TestCase):
    def test_labels_give_the_highest_priority_or_none(self):
        self.assertEqual('P1', t.priority_of([{'name': 'bug'}, {'name': 'priority:P2'}, {'name': 'Priority:p1'}]))
        self.assertIsNone(t.priority_of([{'name': 'bug'}, {'name': 'priority:high'}]))
        self.assertEqual([0, 1, 2, 3, 4], [t.RANK[p] for p in ('P0', 'P1', None, 'P2', 'P3')])


class KimiRules(unittest.TestCase):
    def test_the_prompt_carries_the_owners_fixed_rules_and_the_fields(self):
        # #77, loosened by #82: the rules from the owner's evaluation, in substance.
        text = ' '.join(t.COMMAND.read_text(encoding='utf-8').split())
        for needle in ('`kimiLabel: true`', 'P2 or P3', 'one crate or a small area', 'P0 or P1',
                       'save or serialise paths', 'outside file-format spec', 'an umbrella or a batch',
                       'a leftovers list whose items do not all sit in one crate or area', 'a whole new editor',
                       '`Leftovers from #N` list whose items all sit in one crate or area',
                       'spanning at most 2 crates', "the verb's thin host side", 'a small UI/UX fix in a single view',
                       '`kimiSuitable`', '`kimiRule`', '`kimiReason`', '`kimiOnly: true`'):
            self.assertIn(needle, text)

    def test_the_prompt_names_every_rule(self):
        # #82: the prompt and the schema enum stay in step; the tool-only id is not the model's to give.
        text = t.COMMAND.read_text(encoding='utf-8')
        for rule in (*t.KIMI_ALLOWED, *t.KIMI_EXCLUDED):
            self.assertIn(f'`{rule}`:', text)
        self.assertNotIn(t.KIMI_NOT_P2_P3, text)
        self.assertNotIn(t.KIMI_NOT_P2_P3, t.KIMI_PROPERTIES['kimiRule']['enum'])
        example = next(block for block in re.findall(r'```json\n(.*?)```', text, re.S) if 'kimiSuitable' in block)
        self.assertIn('"kimiRule"', example)
        self.assertIn('{"kimiSuitable": ..., "kimiRule": "...", "kimiReason": "..."}', text)


class Command(unittest.TestCase):
    def test_main_reports_a_missing_config_entry(self):
        folder = ROOT / ('test triage ' + uuid.uuid4().hex)
        folder.mkdir()
        self.addCleanup(shutil.rmtree, folder, True)
        config = folder / 'config.json'
        config.write_text('{}', encoding='utf-8')
        with patch.dict(os.environ, AGWORKBENCH_CONFIG=str(config)), patch('sys.stderr') as err:
            self.assertEqual(2, t.main(['run', '--repo', PRODUCT]))
        self.assertIn('no "triage" section', ''.join(c.args[0] for c in err.write.call_args_list))



class Timestamps(unittest.TestCase):
    """#78: the `#triage` watch pane stamps every line; `run` (the conductor's triage-N.log) does not."""

    class Triage:
        def __init__(self, repo, config, out, **kwargs):
            self.out = out

        def watch(self, interval, limit):
            self.out('#469: priority:P1 (model)')
            self.out('triage scan failed: boom')

        def run_once(self, *args, **kwargs):
            self.out('#469: priority:P1 (model)')
            return 0

    def main(self, argv):
        out = io.StringIO()
        with patch.object(t, 'Triage', self.Triage), patch.object(t, 'load_config', return_value={}), \
                patch.object(t.tslog, 'install', wraps=tslog.install) as install, contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):     # install wraps both; neither may leak
            self.assertEqual(0, t.main(argv))
        return out.getvalue(), install

    def test_watch_stamps_every_line(self):
        text, install = self.main(['watch', '--repo', PRODUCT])
        install.assert_called_once()
        self.assertRegex(text, r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d #469: priority:P1 \(model\)\n'
                               r'\d\d:\d\d:\d\d triage scan failed: boom\n$')

    def test_run_is_not_stamped(self):
        text, install = self.main(['run', '--repo', PRODUCT])
        install.assert_not_called()
        self.assertEqual('#469: priority:P1 (model)\n', text)

    def test_a_watch_config_error_is_stamped_too(self):
        err = io.StringIO()
        with patch.object(t, 'load_config', side_effect=t.ConfigError('no "triage" section')), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(t.ConfigError.code, t.main(['watch', '--repo', PRODUCT]))
        self.assertRegex(err.getvalue(), r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d triage: no "triage" section\n$')


if __name__ == '__main__':
    unittest.main()
