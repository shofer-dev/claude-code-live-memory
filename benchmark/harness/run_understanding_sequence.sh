#!/usr/bin/env bash
# UNDERSTANDING-BOUND *SEQUENCE* A/B — the regime where compounding should show.
# A series of DISTINCT, read-only comprehension questions about different shofer
# subsystems, asked in order. WITH-arm: Live Memory persists across the whole sequence
# (cleared once, then warms), so later questions reuse what earlier ones taught it.
# WITHOUT-arm: each question is a fresh cold agent that re-reads shofer. Hypothesis:
# WITH-arm premium reading (read_tok) trends DOWN across the sequence while WITHOUT stays
# flat — the cumulative divergence single-question runs can't show. Read-only ⇒ no build
# needed ⇒ a source-only worktree; the metric is the mechanism (read_tok / lm_calls /
# turns), answers are not graded.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; AN="$HERE/analyze.py"
SH=${SHOFER_DIR:-/home/alsterg/Projects/arkware.ai/extensions/shofer}
BASE=32cdefcba07ee9afde9bf65b373a75531f015d96
WT=${WT:-/tmp/pilot/uwt}
RUNS=${RUNS:-/tmp/pilot/useq}
MODEL=${MODEL:-claude-sonnet-4-6}
LMCFG='{"mcpServers":{"live-memory":{"type":"http","url":"http://127.0.0.1:7711/mcp"}}}'
HANG_GUARD=${HANG_GUARD:-1200}
mkdir -p "$RUNS"

SYS="When working in this codebase you have a 'live_memory' companion via the ask_live_memory tool (when wired). For ANY question about how the existing code works, where something is defined, what calls what, or how a subsystem fits together, ASK live_memory FIRST - before reading/grepping/globbing source yourself. Pass the absolute repo root as cwd. If the tool is not available, fall back to reading files."

# Distinct, comprehension-heavy questions spanning different subsystems (F1..F4-shaped),
# read-only. Ordered so later ones can reuse earlier structural knowledge.
QUESTIONS=(
  "WITHOUT modifying files, trace the COMPLETE path a native tool call takes from the raw assistant API response to a tool's execute(). Numbered list; name each file + function/class."
  "Where and how are tool names validated against a schema? Name the schema and the file, and what base class all tools extend + the method they implement."
  "How does the tool auto-approval / permission system decide whether a given tool call is allowed? Name the files and the decision flow."
  "How is conversation/session state persisted to disk, and what triggers a save? Name the modules and the format."
  "How is cancellation/interruption propagated through a run (abort signals, child processes, tool state)? Name the files and the mechanism."
  "How does the provider/model layer route a request and handle failover across models? Name the abstraction and where model selection happens."
)

TO(){ [ "$HANG_GUARD" -gt 0 ] && echo "timeout $HANG_GUARD" || echo ""; }
reset(){ git -C "$WT" checkout -- . >/dev/null 2>&1; git -C "$WT" clean -fd >/dev/null 2>&1; }
stats(){ curl -s "http://127.0.0.1:7711/stats?cwd=$WT"|python3 -c "import json,sys;d=json.load(sys.stdin);print(d['inputTokens'],d['outputTokens'],d['cacheReadTokens'],d['cacheWriteTokens'],d['invocations'])" 2>/dev/null || echo "0 0 0 0 0"; }
ask(){ ( cd "$WT" && $(TO) claude -p "$1" --model "$MODEL" --append-system-prompt "$SYS" --strict-mcp-config \
    --dangerously-skip-permissions --max-turns 120 --output-format stream-json --verbose $2 ) >"$3" 2>/dev/null; }
row(){ python3 -c "import json;d=json.load(open('$1'));print('$2,$3,'+','.join(str(d[k]) for k in ['turns','read_calls','read_tok','lm_calls','lm_tok','pin','pout','pcr','pcw','status','api_fail']))"; }

# source-only worktree at the pinned base (read-only task → no pnpm build)
[ -d "$WT/src" ] || git -C "$SH" worktree add --detach "$WT" "$BASE" 2>/dev/null || true
[ -d "$WT/src" ] || { echo "FATAL: could not create worktree at $WT (need $SH + commit $BASE)"; exit 1; }

CSV="$RUNS/results.csv"
echo "arm,qi,turns,read_calls,read_tok,lm_calls,lm_tok,pin,pout,pcr,pcw,status,api_fail" > "$CSV"

echo "###### WITHOUT (cold agent per question, no Live Memory) ######"; reset
for i in "${!QUESTIONS[@]}"; do
  out="$RUNS/wo_q${i}.jsonl"; ask "${QUESTIONS[$i]}" "" "$out"
  python3 "$AN" "$out" > "$RUNS/wo_q${i}.json"
  row "$RUNS/wo_q${i}.json" without "$i" >> "$CSV"
  echo "  [without] q$i -> $(python3 -c "import json;d=json.load(open('$RUNS/wo_q${i}.json'));print('read_tok',d['read_tok'],'turns',d['turns'])")"
done

echo "###### WITH (Live Memory persists + accumulates across the sequence) ######"; reset
curl -s -X POST http://127.0.0.1:7711/clear -d "{\"cwd\":\"$WT\"}" >/dev/null
echo "  warm-up explore query..."
( cd "$WT" && $(TO) claude -p "Use ask_live_memory once: question='Explore and understand this codebase and give a high-level summary of its architecture, main subsystems and conventions', cwd='$WT', timeout=240. Then report it." \
    --model "$MODEL" --mcp-config "$LMCFG" --strict-mcp-config --allowedTools "mcp__live-memory__ask_live_memory" \
    --dangerously-skip-permissions --max-turns 6 --output-format json ) >"$RUNS/wi_warmup.json" 2>/dev/null
for i in "${!QUESTIONS[@]}"; do
  out="$RUNS/wi_q${i}.jsonl"; ask "${QUESTIONS[$i]}" "--mcp-config $LMCFG" "$out"
  python3 "$AN" "$out" > "$RUNS/wi_q${i}.json"
  row "$RUNS/wi_q${i}.json" with "$i" >> "$CSV"
  echo "  [with] q$i -> $(python3 -c "import json;d=json.load(open('$RUNS/wi_q${i}.json'));print('read_tok',d['read_tok'],'lm_calls',d['lm_calls'],'turns',d['turns'])")"
done

echo "###### DONE -> $CSV ######"; column -t -s, "$CSV"
