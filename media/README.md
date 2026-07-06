# Live Memory — launch / promo media

Source files + rendered assets for the launch visuals. Self-contained (system
monospace, no external assets); open the `.html` directly or re-render with the
commands below.

| Asset | Numbers | Use |
|---|---|---|
| **`live-memory.gif`** | **Real capture.** A genuine `ask_live_memory` reply (the answer text is verbatim model output against a warmed server → **`files_read=0`**), replayed deterministically via VHS. Source in [`demo/`](./demo). | Tweet-1 / README-hero **motion** asset — shows the agent answering from memory without re-reading. |
| **`explainer-card`** | **Real.** Benchmark bars (−61% / −57% / −25% / −22%) come straight from [`../benchmark/results/RESULTS.md`](../benchmark/results/RESULTS.md) (understanding-bound A/B, K=12, Fable-verified). | Lead **still** visual — how-it-works + the actual results. This is the proof asset. |
| **`stats-poster`** | **Illustrative.** The chrome is a real `/live-memory-stats` layout, but the values (173 questions, $0.0431, 261 files, 4d uptime, deepseek-v4-flash) are a **hand-authored mockup**, not measured. | "What the tool looks like" shot. Before using publicly, either relabel clearly as an example or replace with a **real** capture — run `/live-memory-stats` against a warmed instance. |

> **Honesty note.** `live-memory.gif` (real captured reply, files_read=0) and
> `explainer-card` (measured benchmark) carry real numbers. `stats-poster` is a
> design mock; do not present its numbers as benchmark results.

## Regenerate the GIF

Needs [`vhs`](https://github.com/charmbracelet/vhs) (+ `ttyd`, `ffmpeg`). From the plugin root:

```sh
vhs media/demo/live-memory.tape      # → media/live-memory.gif
```

`demo/ask_live_memory` prints the captured real reply; the `.tape` types the query
and replays it, so the render is deterministic (no live model call, no server).

## Regenerate the PNGs

Rendered at 2× with headless Chrome, then trimmed to an even margin:

```sh
PROF=$(mktemp -d)
google-chrome --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 --user-data-dir="$PROF" \
  --window-size=1240,1160 --screenshot=explainer-card.png "file://$PWD/explainer-card.html"
google-chrome --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 --user-data-dir="$PROF" \
  --window-size=1000,820 --screenshot=stats-poster.png "file://$PWD/stats-poster.html"
rm -rf "$PROF"

# even margin
convert explainer-card.png -bordercolor '#0a0c11' -fuzz 4% -trim +repage -bordercolor '#0a0c11' -border 60 explainer-card.png
convert stats-poster.png   -bordercolor '#07090d' -fuzz 4% -trim +repage -bordercolor '#07090d' -border 60 stats-poster.png
```

The banner wordmark is the shared `../commands/banner.txt` (ANSI-Shadow figlet),
also shown by `/live-memory-stats` and at the top of the main README.
