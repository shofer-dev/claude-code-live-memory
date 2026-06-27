#!/usr/bin/env bash
# UNDERSTANDING-BOUND A/B: a read-only trace task (synthesize across ~4k lines, no edits)
# -> the without-arm must carry lots of exploration in its window; the with-arm asks
# live-memory. This is the shape where saved reading is NOT backfilled by edit context.
# Acceptance = the ANSWER traces the real path (>=3 key identifiers + substantive length).
set -uo pipefail
P=${1:-4}; K=${2:-12}
BASEWT=/tmp/pilot/pwt; RUNS=${RUNS:-/tmp/pilot/understanding}
HERE="$(cd "$(dirname "$0")" && pwd)"; AN="$HERE/analyze.py"
MODEL=claude-sonnet-4-6
LMCFG='{"mcpServers":{"live-memory":{"type":"http","url":"http://127.0.0.1:7711/mcp"}}}'
HANG_GUARD=${HANG_GUARD:-2700}
mkdir -p "$RUNS"
SYS="When working in this codebase you have a 'live_memory' companion via the ask_live_memory tool (when wired). For ANY question about how the existing code works, where something is defined, what calls what, or how a subsystem fits together, ASK live_memory FIRST - before reading/grepping/globbing source yourself. Pass the absolute repo root as cwd. If the tool is not available, fall back to reading files."
PROMPT="In this shofer codebase (a TypeScript VS Code extension) at the cwd, WITHOUT modifying any files, trace the COMPLETE code path a native tool call takes, from the raw assistant API response to the moment a specific tool's execute() method runs. Produce a numbered list; for EACH step name the exact file path and the function/class/method involved. Also state explicitly: (a) where and how tool names are validated against a schema (name the schema and the file), and (b) what base class all tools extend and the method they must implement. Be specific and grounded in the actual code. Output only the trace; do not modify any files."

TO(){ [ "$HANG_GUARD" -gt 0 ] && echo "timeout $HANG_GUARD" || echo ""; }
reset(){ git -C "$1" checkout -- . >/dev/null 2>&1; git -C "$1" clean -fd >/dev/null 2>&1; }
stats(){ curl -s "http://127.0.0.1:7711/stats?cwd=$1"|python3 -c "import json,sys;d=json.load(sys.stdin);print(d['inputTokens'],d['outputTokens'],d['cacheReadTokens'],d['cacheWriteTokens'],d['invocations'])"; }
ask(){ ( cd "$1" && $(TO) claude -p "$PROMPT" --model "$MODEL" --append-system-prompt "$SYS" --strict-mcp-config \
    --dangerously-skip-permissions --max-turns 120 --output-format stream-json --verbose $2 ) >"$3" 2>/dev/null; }
grade(){ python3 - "$1" <<'PY'
import json,sys
last=None
for ln in open(sys.argv[1],errors="ignore"):
    try:e=json.loads(ln)
    except:continue
    if e.get("type")=="result": last=e
txt=str((last or {}).get("result","") or "").lower()
keys=["nativetoolcallparser","presentassistantmessage","basetool","toolnamesschema"]
hits=sum(1 for k in keys if k in txt)
print("yes" if (hits>=3 and len(txt)>400) else "NO")
PY
}
row(){ python3 -c "import json;d=json.load(open('$1'));print('$2,$3,'+','.join(str(d[k]) for k in ['turns','read_calls','read_tok','lm_calls','lm_tok','edit_calls','pin','pout','pcr','pcw','status','api_fail']))"; }

do_rep(){ local p=$1 k=$2 WT=$BASEWT$1 CSV="$RUNS/w$1.csv"
  reset "$WT"; ask "$WT" "" "$RUNS/r${k}_wo.jsonl"
  python3 "$AN" "$RUNS/r${k}_wo.jsonl" > "$RUNS/r${k}_wo.json"
  echo "$(row "$RUNS/r${k}_wo.json" $k without),$(grade "$RUNS/r${k}_wo.jsonl"),,,,," >> "$CSV"
  reset "$WT"; curl -s -X POST http://127.0.0.1:7711/clear -d "{\"cwd\":\"$WT\"}" >/dev/null
  read i0 o0 r0 w0 v0 <<<"$(stats "$WT")"
  ( cd "$WT" && $(TO) claude -p "Use ask_live_memory once: question='Explore and understand this codebase and give a high-level summary of its architecture, main subsystems and conventions', cwd='$WT', timeout=200. Then report it." \
      --model "$MODEL" --mcp-config "$LMCFG" --strict-mcp-config --allowedTools "mcp__live-memory__ask_live_memory" \
      --dangerously-skip-permissions --max-turns 6 --output-format json ) >/dev/null 2>&1
  ask "$WT" "--mcp-config $LMCFG" "$RUNS/r${k}_wi.jsonl"
  read i2 o2 r2 w2 v2 <<<"$(stats "$WT")"
  python3 "$AN" "$RUNS/r${k}_wi.jsonl" > "$RUNS/r${k}_wi.json"
  echo "$(row "$RUNS/r${k}_wi.json" $k with),$(grade "$RUNS/r${k}_wi.jsonl"),$((i2-i0)),$((r2-r0)),$((w2-w0)),$((o2-o0)),$((v2-v0))" >> "$CSV"
  echo "[w$p] rep $k done"
}
worker(){ local p=$1; for ((k=p; k<=K; k+=P)); do do_rep $p $k; done; }

for p in $(seq 1 $P); do WT=$BASEWT$p; [ -d "$WT/src" ] || bash "$HERE/setup_worktree.sh" "$WT" >/dev/null 2>&1; done
echo "=== launch $P workers x $K reps (understanding-bound trace task) ==="
for p in $(seq 1 $P); do worker $p & done
wait

echo "rep,arm,turns,read_calls,read_tok,lm_calls,lm_tok,edit_calls,pin,pout,pcr,pcw,status,api_fail,accept,ch_in,ch_cr,ch_cw,ch_out,ch_inv" > "$RUNS/results.csv"
cat "$RUNS"/w*.csv 2>/dev/null | sort -t, -k1 -n >> "$RUNS/results.csv"
echo "=== AGGREGATE (valid only) ==="; python3 - "$RUNS/results.csv" <<'PY'
import csv,sys,statistics as S
rows=list(csv.DictReader(open(sys.argv[1])))
SON=dict(i=3,o=15,cr=.30,cw=3.75)
def pc(r): return (int(r['pin'])*SON['i']+int(r['pout'])*SON['o']+int(r['pcr'])*SON['cr']+int(r['pcw'])*SON['cw'])/1e6
def ms(xs): return f"{S.mean(xs):.4g} ± {(S.stdev(xs) if len(xs)>1 else 0):.2g}"
def valid(r): return r['status']=='complete' and r['api_fail']=='no' and r['accept']=='yes'
for arm in ('without','with'):
  rs=[r for r in rows if r['arm']==arm and valid(r)]
  if not rs: print(f"{arm}: 0 valid"); continue
  print(f"\n{arm} (n={len(rs)})")
  print(f"  read_tok      : {ms([int(r['read_tok']) for r in rs])}")
  print(f"  output tok    : {ms([int(r['pout']) for r in rs])}")
  print(f"  cache_read tok: {ms([int(r['pcr']) for r in rs])}")
  print(f"  premium $     : {ms([pc(r) for r in rs])}")
print(f"\ninvalid: {sum(1 for r in rows if not valid(r))}/{len(rows)}")
PY
echo DONE
