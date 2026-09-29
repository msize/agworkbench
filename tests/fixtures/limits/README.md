# Usage-limit fixtures (#24)

`strings-codex.txt` and `strings-claude.txt` are what the installed binaries say, extracted on
2026-09-25 with:

```
python tests/fixtures/limits/extract.py \
  --codex  %APPDATA%\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe \
  --claude %USERPROFILE%\.local\bin\claude.exe
```

Each line is one printable run of bytes from the binary that contains a limit phrase. Some runs
are minified JavaScript (Claude Code) or several adjacent strings (Codex). That is expected: the
file is evidence, not a list of messages.

The `*.txt` frames are pane frames built around those strings. The limit row in every frame is
copied from the strings files, and `tests/test_limits.py` checks that. Claude composes
`You've hit your ${name}` from its limit-name table (`five_hour:"session limit"`, ...), so for those
rows the test checks the template and the name separately.

The layout around each row is **constructed**, not captured, from the shapes in the real frames in
`tests/frames.py`:
- Claude's composer between two rules, `●` items and `⎿` results;
- Codex's `›` composer and `•`/`└` history cells.

The Codex warning chooser (`codex-warning-chooser.txt`) quotes the rows the planner captured from
the real #68 pane on 2026-09-24. Two frames are cut from it, with no row retyped (#61):
`codex-warning-chooser-no-heads-up.txt` drops the `⚠` rows (the chooser a fresh pane shows), and
`codex-warning-chooser-in-tool-output.txt` is the whole frame printed by a `cat` inside Codex, with
the composer below it - the chooser counts only at the bottom of the pane. When a real limit frame is seen, add it here verbatim and keep the
constructed one only if it still adds a case.

## Kimi Code (#65)

`strings-kimi.txt` comes from the same script (`--kimi ~/.kimi-code/bin/kimi.exe`, Kimi Code 2.1.1,
2026-09-29). It holds the quota code and message patterns Kimi itself matches
(`KIMI_QUOTA_EXHAUSTED_*`), the way a session error is drawn (`showStatus(\`Error: ${message}\`)`
with `[${error.code}] ${error.message}`, then the report hint `If this persists, run
/export-debug-zip ...`), the retry label and the provider error codes.

A Kimi limit could not be provoked, so every `kimi-*.txt` frame is **synthesised**: the rows around
the error are the captured `../kimi/status-error.txt` frame (a status error really is glued to the
item above it, with no blank row), and the error and hint rows follow the templates above. Only the
provider's message around the quota words is invented, and `kimi-limited-usage.txt`'s wording
("weekly usage limit") is a guess at how a plan limit would read. When a real Kimi limit frame is
seen, add it here verbatim.
