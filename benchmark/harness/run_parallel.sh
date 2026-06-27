#!/usr/bin/env bash
# Parallel A/B replicates. P workers, each in its OWN shofer worktree + its OWN
# live-memory workspace (keyed by cwd). Per-cwd clear (NOT clear_all) so workers
# don't wipe each other. Round-robin rep assignment; per-worker CSV; aggregate at end.
#   bash run_parallel.sh <P> <K>     (P concurrent workers, K total reps)
set -uo pipefail
P=${1:-4}; K=${2:-20}
BASEWT=/tmp/pilot/pwt; RUNS=${RUNS:-/tmp/pilot/parallel}
HERE="$(cd "$(dirname "$0")" && pwd)"; AN="$HERE/analyze.py"
MODEL=claude-sonnet-4-6
LMCFG='{"mcpServers":{"live-memory":{"type":"http","url":"http://127.0.0.1:7711/mcp"}}}'
HANG_GUARD=${HANG_GUARD:-2700}
mkdir -p "$RUNS"
SYS="When working in this codebase you have a 'live_memory' companion via the ask_live_memory tool (when wired). For ANY question about how the existing code works, where something is defined, what calls what, how a subsystem fits together, or conventions, ASK live_memory FIRST - before reading/grepping/globbing source yourself. Pass the absolute repo root as cwd. Read a file directly only when you need its exact contents to edit it. If the tool is not available, fall back to reading files."
FP="Implement a feature in this shofer codebase (a TypeScript VS Code extension) at the cwd. FEATURE: Add a new read-only tool named count_lines that takes one parameter path (a workspace-relative file path) and returns the number of lines in that file. Wire it through exactly like existing simple read-only tools (read_file/list_files) so the project type-checks and the tool is usable: add its name to the tool-name schema/type, add required entries to sibling maps/records keyed by tool name, create its tool class following BaseTool, register and dispatch it. Requirements: pnpm check-types in src/ MUST pass; do not break tests; deps installed, do NOT run install/build; keep it minimal. List files changed when done."

TO(){ [ "$HANG_GUARD" -gt 0 ] && echo "timeout $HANG_GUARD" || echo ""; }
reset(){ git -C "$1" checkout -- . >/dev/null 2>&1; git -C "$1" clean -fd >/dev/null 2>&1; }
stats(){ curl -s "http://127.0.0.1:7711/stats?cwd=$1"|python3 -c "import json,sys;d=json.load(sys.stdin);print(d['inputTokens'],d['outputTokens'],d['cacheReadTokens'],d['cacheWriteTokens'],d['invocations'])"; }
ok(){ ( cd "$1/src"; pnpm check-types >/dev/null 2>&1 && pnpm vitest run list-files >/dev/null 2>&1 && [ -n "$(git -C "$1" status --porcelain)" ] && grep -q count_lines "$1/packages/types/src/tool.ts" && echo yes||echo NO ); }
feat(){ local out=$1 WT=$2 extra=$3; ( cd "$WT" && $(TO) claude -p "$FP" --model "$MODEL" --append-system-prompt "$SYS" --strict-mcp-config \
    --dangerously-skip-permissions --max-turns 200 --output-format stream-json --verbose $extra ) >"$out" 2>/dev/null; }
row(){ python3 -c "import json;d=json.load(open('$1'));print('$2,$3,'+','.join(str(d[k]) for k in ['turns','read_calls','read_tok','lm_calls','lm_tok','edit_calls','pin','pout','pcr','pcw','status','api_fail']))"; }

do_rep(){ local p=$1 k=$2 WT=$BASEWT$1 CSV="$RUNS/w$1.csv"
  # WITHOUT
  reset "$WT"; feat "$RUNS/r${k}_wo.jsonl" "$WT" ""
  python3 "$AN" "$RUNS/r${k}_wo.jsonl" > "$RUNS/r${k}_wo.json"
  echo "$(row "$RUNS/r${k}_wo.json" $k without),$(ok "$WT"),,,,," >> "$CSV"
  # WITH (clear ONLY this cwd, warm-up, feature)
  reset "$WT"; curl -s -X POST http://127.0.0.1:7711/clear -d "{\"cwd\":\"$WT\"}" >/dev/null
  read i0 o0 r0 w0 v0 <<<"$(stats "$WT")"
  ( cd "$WT" && $(TO) claude -p "Use ask_live_memory once: question='Explore and understand this codebase and give a high-level summary of its architecture, main subsystems and conventions', cwd='$WT', timeout=200. Then report it." \
      --model "$MODEL" --mcp-config "$LMCFG" --strict-mcp-config --allowedTools "mcp__live-memory__ask_live_memory" \
      --dangerously-skip-permissions --max-turns 6 --output-format json ) >/dev/null 2>&1
  feat "$RUNS/r${k}_wi.jsonl" "$WT" "--mcp-config $LMCFG"
  read i2 o2 r2 w2 v2 <<<"$(stats "$WT")"
  python3 "$AN" "$RUNS/r${k}_wi.jsonl" > "$RUNS/r${k}_wi.json"
  echo "$(row "$RUNS/r${k}_wi.json" $k with),$(ok "$WT"),$((i2-i0)),$((r2-r0)),$((w2-w0)),$((o2-o0)),$((v2-v0))" >> "$CSV"
  echo "[w$p] rep $k done"
}
worker(){ local p=$1; for ((k=p; k<=K; k+=P)); do do_rep $p $k; done; }

echo "=== setup $P worktrees (sequential) ==="
for p in $(seq 1 $P); do WT=$BASEWT$p; [ -d "$WT/src" ] || bash "$HERE/setup_worktree.sh" "$WT" >/dev/null 2>&1; echo "  $WT ready"; done
echo "=== launch $P workers x $K reps ==="
for p in $(seq 1 $P); do worker $p & done
wait

echo "rep,arm,turns,read_calls,read_tok,lm_calls,lm_tok,edit_calls,pin,pout,pcr,pcw,status,api_fail,accept,ch_in,ch_cr,ch_cw,ch_out,ch_inv" > "$RUNS/results.csv"
cat "$RUNS"/w*.csv 2>/dev/null | sort -t, -k1 -n >> "$RUNS/results.csv"
echo "=== AGGREGATE (valid only) ==="; python3 - "$RUNS/results.csv" <<'PY'
import csv,sys,statistics as S
rows=list(csv.DictReader(open(sys.argv[1])))
SON=dict(i=3,o=15,cr=.30,cw=3.75); HAI=dict(i=1,o=5,cr=.10,cw=1.25)
def pc(r): return (int(r['pin'])*SON['i']+int(r['pout'])*SON['o']+int(r['pcr'])*SON['cr']+int(r['pcw'])*SON['cw'])/1e6
def ms(xs): return f"{S.mean(xs):.4g} ± {(S.stdev(xs) if len(xs)>1 else 0):.2g}"
def valid(r): return r['status']=='complete' and r['api_fail']=='no' and r['accept']=='yes'
for arm in ('without','with'):
  rs=[r for r in rows if r['arm']==arm and valid(r)]
  if not rs: print(f"{arm}: 0 valid"); continue
  print(f"\n{arm} (n={len(rs)})")
  print(f"  read_tok (mechanism): {ms([int(r['read_tok']) for r in rs])}")
  print(f"  turns               : {ms([int(r['turns']) for r in rs])}")
  print(f"  premium $           : {ms([pc(r) for r in rs])}")
  if arm=='with': print(f"  cheap $ (wu+feat)   : {ms([(int(r['ch_in'])*HAI['i']+int(r['ch_out'])*HAI['o']+int(r['ch_cr'])*HAI['cr']+int(r['ch_cw'])*HAI['cw'])/1e6 for r in rs])}")
print(f"\ninvalid: {sum(1 for r in rows if not valid(r))}/{len(rows)}")
PY
echo DONE
