#!/usr/bin/env bash
# K independent A/B replicates of the lead feature -> mean ± spread. No OS timeout.
set -uo pipefail
WT=/tmp/pilot/shofer; RUNS=/tmp/pilot/reps; K=${1:-4}
MODEL=claude-sonnet-4-6
LMCFG='{"mcpServers":{"live-memory":{"type":"http","url":"http://127.0.0.1:7711/mcp"}}}'
mkdir -p "$RUNS"
SYS="When working in this codebase you have a 'live_memory' companion via the ask_live_memory tool (when wired). For ANY question about how the existing code works, where something is defined, what calls what, how a subsystem fits together, or conventions, ASK live_memory FIRST - before reading/grepping/globbing source yourself. Pass the absolute repo root as cwd. Read a file directly only when you need its exact contents to edit it. If the tool is not available, fall back to reading files."
P="Implement a feature in this shofer codebase (a TypeScript VS Code extension) at the cwd. FEATURE: Add a new read-only tool named count_lines that takes one parameter path (a workspace-relative file path) and returns the number of lines in that file. Wire it through exactly like existing simple read-only tools (read_file/list_files) so the project type-checks and the tool is usable: add its name to the tool-name schema/type, add required entries to sibling maps/records keyed by tool name, create its tool class following BaseTool, register and dispatch it. Requirements: pnpm check-types in src/ MUST pass; do not break tests; deps installed, do NOT run install/build; keep it minimal. List files changed when done."
parse(){ python3 - "$1" <<'PY'
import json,sys
last=None
for ln in open(sys.argv[1],errors="ignore"):
  try:e=json.loads(ln)
  except:continue
  if e.get("type")=="result":last=e
u=(last or {}).get("usage",{})
print(u.get("input_tokens",0),u.get("output_tokens",0),u.get("cache_read_input_tokens",0),u.get("cache_creation_input_tokens",0),"complete" if last else "KILLED")
PY
}
err(){ grep -qE 'ConnectionRefused|API Error|"is_error":true' "$1" && echo YES||echo no; }
reset(){ git -C "$WT" checkout -- . >/dev/null 2>&1; git -C "$WT" clean -fd >/dev/null 2>&1; }
st(){ curl -s "http://127.0.0.1:7711/stats?cwd=$WT"|python3 -c "import json,sys;d=json.load(sys.stdin);print(d['inputTokens'],d['outputTokens'],d['cacheReadTokens'],d['cacheWriteTokens'],d['invocations'])"; }
feat(){ ( cd "$WT" && claude -p "$P" --model "$MODEL" --append-system-prompt "$SYS" --dangerously-skip-permissions --max-turns 200 --output-format stream-json --verbose $2 ) >"$1" 2>/dev/null; }
ok(){ ( cd "$WT/src"; pnpm check-types >/dev/null 2>&1 && pnpm vitest run list-files >/dev/null 2>&1 && [ -n "$(git -C "$WT" status --porcelain)" ] && grep -q count_lines "$WT/packages/types/src/tool.ts" && echo yes||echo NO ); }
echo "rep,arm,in,out,cacheR,cacheW,status,apierr,accept,ch_in,ch_cr,ch_cw,ch_out,ch_inv" > "$RUNS/results.csv"
for k in $(seq 1 $K); do
  echo ">>> REP $k WITHOUT"; reset; feat "$RUNS/r${k}_wo.jsonl" ""
  read a b c d s <<<"$(parse "$RUNS/r${k}_wo.jsonl")"
  echo "$k,without,$a,$b,$c,$d,$s,$(err "$RUNS/r${k}_wo.jsonl"),$(ok),,,,," >> "$RUNS/results.csv"
  echo ">>> REP $k WITH"; reset; curl -s -X POST http://127.0.0.1:7711/clear -d '{"all":true}' >/dev/null
  read i0 o0 r0 w0 v0 <<<"$(st)"
  ( cd "$WT" && claude -p "Use ask_live_memory once: question='Explore and understand this codebase and give a high-level summary of its architecture, main subsystems and conventions', cwd='$WT', timeout=200. Then report it." --model "$MODEL" --mcp-config "$LMCFG" --allowedTools "mcp__live-memory__ask_live_memory" --dangerously-skip-permissions --max-turns 6 --output-format json ) >/dev/null 2>&1
  feat "$RUNS/r${k}_wi.jsonl" "--mcp-config $LMCFG"
  read i2 o2 r2 w2 v2 <<<"$(st)"
  read a b c d s <<<"$(parse "$RUNS/r${k}_wi.jsonl")"
  echo "$k,with,$a,$b,$c,$d,$s,$(err "$RUNS/r${k}_wi.jsonl"),$(ok),$((i2-i0)),$((r2-r0)),$((w2-w0)),$((o2-o0)),$((v2-v0))" >> "$RUNS/results.csv"
done
echo "=== AGGREGATE ==="; python3 - "$RUNS/results.csv" <<'PY'
import csv,sys,statistics as S
rows=list(csv.DictReader(open(sys.argv[1])))
SON=dict(i=3,o=15,cr=.30,cw=3.75); HAI=dict(i=1,o=5,cr=.10,cw=1.25)
def pcost(r): return (int(r['in'])*SON['i']+int(r['out'])*SON['o']+int(r['cacheR'])*SON['cr']+int(r['cacheW'])*SON['cw'])/1e6
def valid(r): return r['status']=='complete' and r['apierr']=='no' and r['accept']=='yes'
for arm in ('without','with'):
  rs=[r for r in rows if r['arm']==arm and valid(r)]
  if not rs: print(f"{arm}: no valid reps"); continue
  costs=[pcost(r) for r in rs]; outs=[int(r['out']) for r in rs]
  print(f"{arm:8} n={len(rs)} premium$ mean={S.mean(costs):.4f} sd={(S.stdev(costs) if len(costs)>1 else 0):.4f}  out mean={S.mean(outs):.0f}")
  if arm=='with':
    ch=[(int(r['ch_in'])*HAI['i']+int(r['ch_out'])*HAI['o']+int(r['ch_cr'])*HAI['cr']+int(r['ch_cw'])*HAI['cw'])/1e6 for r in rs]
    print(f"         cheap$(feature only) mean={S.mean(ch):.4f}  inv mean={S.mean([int(r['ch_inv']) for r in rs]):.1f}")
PY
echo "DONE"
