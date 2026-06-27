#!/usr/bin/env bash
# A/B replicates v2 — CORRECTED + INSTRUMENTED.
#  * --strict-mcp-config isolates live-memory per arm (without: none; with: only ours,
#    excluding the global live-memory@shofer PLUGIN that otherwise leaks into both arms).
#  * Mechanism metric (analyze.py): premium tokens the BUILDING agent spends reading the
#    codebase itself (Read/Grep/Glob tool-result tokens) + turns + read/edit/lm calls.
#  * Per-turn normalization in the aggregate.
# NOTE: claude -p exposes no temperature/seed flag, so sampling can't be pinned (lever #2
#       is infeasible); we rely on the mechanism metric + per-turn + replicates instead.
set -uo pipefail
WT=/tmp/pilot/shofer; RUNS=/tmp/pilot/reps2; K=${1:-4}
HERE="$(cd "$(dirname "$0")" && pwd)"; AN="$HERE/analyze.py"
MODEL=claude-sonnet-4-6
LMCFG='{"mcpServers":{"live-memory":{"type":"http","url":"http://127.0.0.1:7711/mcp"}}}'
mkdir -p "$RUNS"
SYS="When working in this codebase you have a 'live_memory' companion via the ask_live_memory tool (when wired). For ANY question about how the existing code works, where something is defined, what calls what, how a subsystem fits together, or conventions, ASK live_memory FIRST - before reading/grepping/globbing source yourself. Pass the absolute repo root as cwd. Read a file directly only when you need its exact contents to edit it. If the tool is not available, fall back to reading files."
P="Implement a feature in this shofer codebase (a TypeScript VS Code extension) at the cwd. FEATURE: Add a new read-only tool named count_lines that takes one parameter path (a workspace-relative file path) and returns the number of lines in that file. Wire it through exactly like existing simple read-only tools (read_file/list_files) so the project type-checks and the tool is usable: add its name to the tool-name schema/type, add required entries to sibling maps/records keyed by tool name, create its tool class following BaseTool, register and dispatch it. Requirements: pnpm check-types in src/ MUST pass; do not break tests; deps installed, do NOT run install/build; keep it minimal. List files changed when done."
err(){ grep -qE 'ConnectionRefused|API Error|"is_error":true' "$1" && echo YES||echo no; }
reset(){ git -C "$WT" checkout -- . >/dev/null 2>&1; git -C "$WT" clean -fd >/dev/null 2>&1; }
st(){ curl -s "http://127.0.0.1:7711/stats?cwd=$WT"|python3 -c "import json,sys;d=json.load(sys.stdin);print(d['inputTokens'],d['outputTokens'],d['cacheReadTokens'],d['cacheWriteTokens'],d['invocations'])"; }
ok(){ ( cd "$WT/src"; pnpm check-types >/dev/null 2>&1 && pnpm vitest run list-files >/dev/null 2>&1 && [ -n "$(git -C "$WT" status --porcelain)" ] && grep -q count_lines "$WT/packages/types/src/tool.ts" && echo yes||echo NO ); }
# BOTH arms: --strict-mcp-config. without: no --mcp-config -> zero LM. with: only ours.
feat(){ ( cd "$WT" && claude -p "$P" --model "$MODEL" --append-system-prompt "$SYS" --strict-mcp-config \
    --dangerously-skip-permissions --max-turns 200 --output-format stream-json --verbose $2 ) >"$1" 2>/dev/null; }
row(){ python3 -c "import json,sys;d=json.loads(open('$1.json').read());print('$2,$3,'+','.join(str(d[k]) for k in ['turns','read_calls','read_tok','lm_calls','lm_tok','edit_calls','pin','pout','pcr','pcw','status','api_fail']))"; }

echo "rep,arm,turns,read_calls,read_tok,lm_calls,lm_tok,edit_calls,pin,pout,pcr,pcw,status,apierr,accept,ch_in,ch_cr,ch_cw,ch_out,ch_inv" > "$RUNS/results.csv"
for k in $(seq 1 $K); do
  echo ">>> REP $k WITHOUT"; reset; feat "$RUNS/r${k}_wo.jsonl" ""
  python3 "$AN" "$RUNS/r${k}_wo.jsonl" > "$RUNS/r${k}_wo.json"
  echo "$(row "$RUNS/r${k}_wo" $k without),$(ok),,,,," >> "$RUNS/results.csv"
  echo ">>> REP $k WITH"; reset; curl -s -X POST http://127.0.0.1:7711/clear -d '{"all":true}' >/dev/null
  read i0 o0 r0 w0 v0 <<<"$(st)"
  ( cd "$WT" && claude -p "Use ask_live_memory once: question='Explore and understand this codebase and give a high-level summary of its architecture, main subsystems and conventions', cwd='$WT', timeout=200. Then report it." --model "$MODEL" --mcp-config "$LMCFG" --strict-mcp-config --allowedTools "mcp__live-memory__ask_live_memory" --dangerously-skip-permissions --max-turns 6 --output-format json ) >/dev/null 2>&1
  feat "$RUNS/r${k}_wi.jsonl" "--mcp-config $LMCFG"
  read i2 o2 r2 w2 v2 <<<"$(st)"
  python3 "$AN" "$RUNS/r${k}_wi.jsonl" > "$RUNS/r${k}_wi.json"
  echo "$(row "$RUNS/r${k}_wi" $k with),$(ok),$((i2-i0)),$((r2-r0)),$((w2-w0)),$((o2-o0)),$((v2-v0))" >> "$RUNS/results.csv"
done

echo "=== AGGREGATE (valid reps only) ==="; python3 - "$RUNS/results.csv" <<'PY'
import csv,sys,statistics as S
rows=[r for r in csv.DictReader(open(sys.argv[1]))]
SON=dict(i=3,o=15,cr=.30,cw=3.75); HAI=dict(i=1,o=5,cr=.10,cw=1.25)
def pc(r): return (int(r['pin'])*SON['i']+int(r['pout'])*SON['o']+int(r['pcr'])*SON['cr']+int(r['pcw'])*SON['cw'])/1e6
def ms(xs): return f"{S.mean(xs):.4g} ± {(S.stdev(xs) if len(xs)>1 else 0):.2g}"
def valid(r): return r['status']=='complete' and r['apierr']=='no' and r['accept']=='yes'
for arm in ('without','with'):
  rs=[r for r in rows if r['arm']==arm and valid(r)]
  if not rs: print(f"{arm}: no valid reps"); continue
  print(f"\n{arm}  (n={len(rs)})")
  print(f"  MECHANISM codebase read_tok : {ms([int(r['read_tok']) for r in rs])}   read_calls {ms([int(r['read_calls']) for r in rs])}  lm_calls {ms([int(r['lm_calls']) for r in rs])}")
  print(f"  turns                       : {ms([int(r['turns']) for r in rs])}")
  print(f"  premium $ (total)           : {ms([pc(r) for r in rs])}")
  print(f"  premium $ / turn            : {ms([pc(r)/max(1,int(r['turns'])) for r in rs])}")
  if arm=='with':
    print(f"  cheap $ (warmup+feature)    : {ms([(int(r['ch_in'])*HAI['i']+int(r['ch_out'])*HAI['o']+int(r['ch_cr'])*HAI['cr']+int(r['ch_cw'])*HAI['cw'])/1e6 for r in rs])}")
PY
echo "DONE"
