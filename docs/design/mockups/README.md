# Mock-ups

The reference for the UI design in [../README.md](../README.md), accepted by the owner on
2026-10-31. Every example value, name and date in them is **synthetic**.

| Folder | What | Use it to |
|---|---|---|
| `png/` | `<Name>.png` (light) and `<Name>.dark.png` (dark), full page, at the board's width | See a screen. Reviewers compare UI PRs with these |
| `html/` | One standalone page per screen plus `ov.css`; links between screens work | Open in a browser, inspect sizes, spacing and markup; add `?theme=dark` for the dark theme |
| `src/` | The design canvas sources: one `.dc.html` per board, the shared `ov.css` (all tokens and component styles), `canvas.json` (board sizes and layout) | Read exact tokens, copy text and structure; change the design |

`src/` is byte-identical to the owner's canvas. A `.dc.html` file is the canvas's format: markup
with `{{holes}}`, `<sc-for>`, `<sc-if>` and `<dc-import>` (the navigation rail, `Rail.dc.html`),
and a small script whose `renderVals()` computes the synthetic chart paths. It needs the canvas
runtime (`support.js`, not in the repository), which is why `html/` exists.

## Regenerating

```bash
node docs/design/mockups/build.mjs        # src/ → html/ (no dependencies)
node docs/design/mockups/screenshot.mjs   # html/ → png/ (needs Playwright with Chromium, and network for the fonts)
```

## Changing the design

Only with the owner's go-ahead (rule 3 in [../README.md](../README.md)). Change the canvas (or
`src/`), keep the two identical, rerun both scripts, and commit `src/`, `html/` and `png/`
together with the change to `../README.md`.
