"""#24: recognising a usage limit from a pane frame, with fixtures built from the binaries' own strings."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import limits  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "limits"

# frame -> (tool, expected kind, exited)
EXPECTED = {
    "codex-limited-live": ("codex", "limited", False),
    "codex-limited-reached": ("codex", "limited", False),
    "codex-limited-credits": ("codex", "limited", False),
    "codex-limited-exited": ("codex", "limited", True),
    "claude-limited-idle": ("claude", "limited", False),
    "claude-limited-team": ("claude", "limited", False),
    "claude-limited-credit": ("claude", "limited", False),
    "codex-warning-chooser": ("codex", "warning", False),
    # #61: the chooser a fresh pane shows (the #68 frame without its heads-up rows), and the
    # regression case - the same chooser dumped in tool output, with the composer below it
    "codex-warning-chooser-no-heads-up": ("codex", "warning", False),
    "codex-warning-chooser-in-tool-output": ("codex", None, False),
    "codex-auto-switched": ("codex", None, False),
    "codex-tool-output": ("codex", None, False),
    "codex-working": ("codex", None, False),
    "claude-tool-output-idle": ("claude", None, False),
    "claude-tool-output-bare": ("claude", None, False),
    "claude-tool-output-running": ("claude", None, False),
    "claude-cat-fixture": ("claude", None, False),
    "claude-diff": ("claude", None, False),
    "claude-unittest-failure": ("claude", None, False),
    "claude-grep": ("claude", None, False),
    "claude-prose": ("claude", None, False),
    "claude-quoted": ("claude", None, False),
    "claude-scrolled": ("claude", None, False),
    "codex-text-above-fresh-claude": ("claude", None, False),
    # r17 M1: the agent's own reply glyph never carries a limit
    "claude-reply-starts-with-phrase": ("claude", None, False),
    "claude-reply-second-line-phrase": ("claude", None, False),
    "codex-message-starts-with-phrase": ("codex", None, False),
    # #65: Kimi Code, synthesised around the captured status-error frame (see README.md)
    "kimi-limited-quota": ("kimi", "limited", False),
    "kimi-limited-balance": ("kimi", "limited", False),
    "kimi-limited-usage": ("kimi", "limited", False),
    "kimi-limited-5hour": ("kimi", "limited", False),     # #77: the owner's evaluation, 2026-09-29/30
    "kimi-rate-limit-transient": ("kimi", None, False),
    "kimi-retrying": ("kimi", None, False),
    "kimi-tool-output": ("kimi", None, False),
    # FIX r1 m6: the tool call is the last item above the composer, its output ending in error + hint
    "kimi-tool-output-last": ("kimi", None, False),
    # FIX r2 m3: Kimi exited to the shell with a tool's quoted quota error + hint above the prompt
    "kimi-exited-quoted": ("kimi", None, False),
    "kimi-history": ("kimi", None, False),
    "kimi-diff": ("kimi", None, False),
}
KIMI_CAPTURED = ROOT / "tests" / "fixtures" / "kimi"


def frame(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


class Fixtures(unittest.TestCase):
    def test_every_frame_file_has_an_expectation(self):
        names = {p.stem for p in FIXTURES.glob("*.txt") if not p.stem.startswith("strings-")}
        self.assertEqual(set(EXPECTED), names)

    def test_frames_classify_as_expected(self):
        for name, (tool, kind, exited) in EXPECTED.items():
            with self.subTest(frame=name):
                found = limits.classify(frame(name), tool)
                self.assertEqual(kind, found.kind if found else None, found)
                if found:
                    self.assertEqual(exited, found.exited)

    def test_limit_rows_are_quoted_from_the_binaries_not_retyped(self):
        strings = {tool: (FIXTURES / f"strings-{tool}.txt").read_text(encoding="utf-8") for tool in ("codex", "claude")}
        # The #68 warning rows came from the planner's captured frame; everything else from the binaries.
        captured = {"Heads up, you have less than 10% of your weekly limit left. Run /status for a",
                    "Switch to gpt-5.6-luna for lower credit usage?"}
        for name, (tool, kind, _) in EXPECTED.items():
            if kind != "limited" or tool == "kimi":
                continue                 # Kimi's rows are composed from templates: test_kimi_rows_...
            with self.subTest(frame=name):
                line = limits.classify(frame(name), tool).line
                phrase = line.split(" ", 1)[1].lstrip() if not line[0].isalnum() else line
                fragment = limits.APOSTROPHES.sub("'", phrase[:24])
                source = limits.APOSTROPHES.sub("'", strings[tool])
                template = re.match(r"You've hit your ((?:session|weekly|Opus|Sonnet|Fable) limit)", fragment + phrase[24:])
                if tool == "claude" and template:
                    # Claude composes `You've hit your ${name}` from the limit-name table.
                    self.assertIn("You've hit your", source)
                    self.assertIn(f'"{template.group(1)}"', source)
                    continue
                self.assertTrue(fragment in source, f"{fragment!r} is not in strings-{tool}.txt")
        warning = limits.classify(frame("codex-warning-chooser"), "codex").line
        self.assertTrue(any(c in frame("codex-warning-chooser") for c in captured), warning)

    def test_the_binary_extraction_found_the_documented_strings(self):
        codex = (FIXTURES / "strings-codex.txt").read_text(encoding="utf-8")
        claude = (FIXTURES / "strings-claude.txt").read_text(encoding="utf-8")
        for needle in ("hit your usage limit", "Usage limit reached", "out of credits", "Approaching rate limits",
                       "Heads up, you have less than", "due to usage limits"):
            self.assertIn(needle, codex)
        for needle in ("You've hit your", "Usage limit reached", "usage credit limit reached",
                       'five_hour:"session limit"'):
            self.assertIn(needle, claude)
        kimi = (FIXTURES / "strings-kimi.txt").read_text(encoding="utf-8")
        for needle in ("exceeded_current_quota_error", "exceeded your current (?:token )?quota", "insufficient balance",
                       "this.showStatus(`Error: ${message}`", "return `[${error.code}] ${error.message}`",
                       "If this persists, run `/export-debug-zip`", "Retrying (${retry.nextAttempt}",
                       'PROVIDER_API_ERROR_CODE = "provider.api_error"', '"provider.rate_limit"'):
            self.assertIn(needle, kimi)

    def test_kimi_rows_are_composed_from_the_binarys_templates(self):
        """Kimi draws a session error as showStatus(`Error: ${formatErrorPayload}`) - `[code] message` -
        and then its report hint. The code, the hint and the quota wording come from strings-kimi.txt;
        only the provider's message around the quota words is invented (and marked in README.md)."""
        kimi = (FIXTURES / "strings-kimi.txt").read_text(encoding="utf-8")
        hint = re.search(r'return "(If this persists, run [^"]+)";', kimi).group(1)
        for name, (tool, kind, _) in EXPECTED.items():
            if tool != "kimi":
                continue
            with self.subTest(frame=name):
                text = frame(name)
                error = next(row.strip() for row in text.splitlines() if "Error: [provider." in row)
                code = re.search(r"\[(provider\.[a-z_]+)\]", error).group(1)
                # The strings run may break inside a code: provider.auth_error ends one run as `.auth_error"`.
                self.assertTrue(f'"{code}"' in kimi or f'.{code.split(".", 1)[1]}":' in kimi, code)
                self.assertTrue(" ".join(text.split()).find(" ".join(hint.split())) >= 0 or name == "kimi-diff")
                if kind == "limited" and name not in ("kimi-limited-usage", "kimi-limited-5hour"):
                    self.assertTrue(any(phrase in " ".join(text.split()) for phrase in
                                        ("exceeded your current quota", "insufficient balance")), name)

    def test_kimi_captured_frames_are_never_limits(self):
        for path in sorted(KIMI_CAPTURED.glob("*.txt")):
            with self.subTest(frame=path.name):
                self.assertIsNone(limits.classify(path.read_text(encoding="utf-8"), "kimi"))


class Position(unittest.TestCase):
    COMPOSER = "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n> \n" \
               "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n  status\n"

    def test_a_real_limit_below_an_old_tool_call_still_counts(self):
        text = ("\u25cf Bash(git status)\n  \u23bf  clean\n\n> continue with r16\n"
                "  \u23bf  You've hit your session limit \u00b7 resets 3pm\n" + self.COMPOSER)
        self.assertEqual("limited", limits.classify(text, "claude").kind)

    def test_typographic_apostrophes_and_case(self):
        text = "> go\n  \u23bf  You\u2019ve hit your weekly limit \u00b7 resets Mon\n" + self.COMPOSER
        self.assertEqual("limited", limits.classify(text, "claude").kind)
        self.assertEqual("limited", limits.classify(
            "\u25a0 you\u2019ve hit your usage limit.\n\n\u203a Ask Codex to do anything\n", "codex").kind)

    def test_forbidden_prefixes_never_count(self):
        for prefix in ('"', "`", "+ ", "- ", "> ", "# ", "| ", "lib/x.py:3: "):
            with self.subTest(prefix=prefix):
                text = f"{prefix}You\u2019ve hit your usage limit.\n\n\u203a Ask Codex to do anything\n"
                self.assertIsNone(limits.classify(text, "codex"))

    def test_exited_needs_the_phrase_in_the_last_commands_output(self):
        shell = "PS C:\\repo> "
        old = f"\u25a0 You\u2019ve hit your usage limit.\n{shell}codex\n{shell}git status\nclean\n{shell}\n"
        self.assertIsNone(limits.classify(old, "codex"))
        fresh = f"{shell}codex\n\u25a0 You\u2019ve hit your usage limit.\nTo continue this session, run codex resume x\n{shell}\n"
        self.assertTrue(limits.classify(fresh, "codex").exited)

    def test_the_warning_chooser_counts_only_at_the_bottom_of_the_pane(self):
        chooser = frame("codex-warning-chooser")
        self.assertTrue(limits.classify(chooser, "codex").line.startswith("⚠ Heads up"))
        # Answered: the heads-up row stays in history, the chooser is gone, the composer is back.
        answered = chooser.split("\n\n  Approaching")[0] + "\n\n› Ask Codex to do anything\n"
        self.assertIsNone(limits.classify(answered, "codex"))
        # Options with no warning row above them (another picker) never count.
        picker = "• Ran ls\n  └ lib\n\n› 1. Switch to plan mode\n  2. Keep current model\n"
        self.assertIsNone(limits.classify(picker, "codex"))

    def test_kimi_needs_the_idle_composer_and_the_hint_right_below_the_error(self):
        quota = frame("kimi-limited-quota")
        self.assertEqual("limited", limits.classify(quota, "kimi").kind)
        # A dialog replaced the composer: no verdict from a pane nobody can read.
        self.assertIsNone(limits.classify(quota.split(" ╭")[0] + "\n   ▶ 1. Approve once\n", "kimi"))
        # Without the hint row the error row cannot be told from a tool's output.
        no_hint = "\n".join(row for row in quota.splitlines() if "If this persists" not in row)
        self.assertIsNone(limits.classify(no_hint, "kimi"))
        # The same rows classify as nothing for the other tools.
        self.assertIsNone(limits.classify(quota, "claude"))
        self.assertIsNone(limits.classify(quota, "codex"))

    def test_unknown_tool_is_refused(self):
        with self.assertRaises(ValueError):
            limits.classify("x", "aider")

    def test_tail_hash_follows_the_last_twenty_rows(self):
        base = "\n".join(f"row {i}" for i in range(30))
        self.assertEqual(limits.tail_hash(base), limits.tail_hash("different top\n" + base + "\n\n"))
        self.assertNotEqual(limits.tail_hash(base), limits.tail_hash(base + "\nrow 30"))


class Cli(unittest.TestCase):
    def test_classify_prints_json(self):
        done = subprocess.run([sys.executable, str(ROOT / "lib" / "limits.py"), "classify", "--tool", "codex"],
                              input=frame("codex-limited-exited").encode("utf-8"), capture_output=True)
        self.assertEqual(0, done.returncode, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual(("limited", True), (result["kind"], result["exited"]))
        self.assertEqual(limits.tail_hash(frame("codex-limited-exited")), result["tail"])
        done = subprocess.run([sys.executable, str(ROOT / "lib" / "limits.py"), "classify", "--tool", "kimi"],
                              input=frame("kimi-limited-quota").encode("utf-8"), capture_output=True)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual("limited", json.loads(done.stdout)["kind"])
        done = subprocess.run([sys.executable, str(ROOT / "lib" / "limits.py"), "classify", "--tool", "claude"],
                              input=frame("claude-grep").encode("utf-8"), capture_output=True)
        self.assertIsNone(json.loads(done.stdout)["kind"])


if __name__ == "__main__":
    unittest.main()
