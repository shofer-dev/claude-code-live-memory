# Live Memory — launch / promo media

Source HTML + rendered PNGs for the launch visuals. Self-contained (system
monospace, no external assets); open the `.html` directly or re-render with the
command below.

| Asset | Numbers | Use |
|---|---|---|
| **`explainer-card`** | **Real.** Benchmark bars (−61% / −57% / −25% / −22%) come straight from [`../benchmark/results/RESULTS.md`](../benchmark/results/RESULTS.md) (understanding-bound A/B, K=12, Fable-verified). | Lead visual — how-it-works + the actual results. This is the proof asset. |
| **`stats-poster`** | **Illustrative.** The chrome is a real `/live-memory-stats` layout, but the values (173 questions, $0.0431, 261 files, 4d uptime, deepseek-v4-flash) are a **hand-authored mockup**, not measured. | "What the tool looks like" shot. Before using publicly, either relabel clearly as an example or replace with a **real** capture — run `/live-memory-stats` against a warmed instance. |

> **Honesty note.** Only `explainer-card` carries measured numbers. `stats-poster`
> is a design mock; do not present its numbers as benchmark results.

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
