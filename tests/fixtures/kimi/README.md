# Kimi Code frames (#65)

Captured, not written by hand: `agw.pane_text()` of a live Kimi Code 2.1.1 pane in agwinterm, started
the way `pane-implementer-kimi.ps1` starts it (`[Console]::OutputEncoding = UTF8`, `--yolo`,
`KIMI_CODE_NO_AUTO_UPDATE=1`), in a throwaway session in a temp directory. Without the UTF-8
console every glyph below arrives as cp437 mojibake (`ΓÇö` for `—`).

| file | what is on screen |
|---|---|
| `idle-fresh.txt` | first start after the trust dialog: welcome panel (itself a box), empty composer |
| `idle-after-turn.txt` | a finished turn, empty composer |
| `draft-wrapped.txt` | a typed draft that wraps onto a second, indented composer row |
| `burst.txt` | text and Return typed in one `session.type` call: the Return became a newline (paste-burst rule) |
| `running-thinking.txt` | a running turn: `⠹ Thinking… · Tip:` spinner row right above the empty composer |
| `running-tool.txt` | a running turn in a tool call: `🌗 · Tip:` spinner row above the composer |
| `approval.txt` | `--yolo` still asking: "Run this command?" with `▶ 1. Approve once`; the composer is gone |
| `trust-dialog.txt` | first start in an untrusted folder: "Trust this folder?"; no composer |
| `shell-mode.txt` | `!` typed: the box is titled `! shell mode` and the row starts with `!` |
| `status-error.txt` | a status error (`Error: Usage: /undo ...`) - drawn right under the previous item, no blank row |

The usage-limit frames in `../limits/kimi-*.txt` are synthesised from `status-error.txt`'s layout
and the strings in the binary; see `../limits/README.md`.
