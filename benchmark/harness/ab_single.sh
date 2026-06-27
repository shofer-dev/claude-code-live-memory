#!/usr/bin/env bash
# A/B re-run #2. No OS timeout (max-turns is the only, clean, bound). Detects API
# failures -> INVALID. Acceptance verifies the feature ACTUALLY exists (no-op fails).
set -uo pipefail
WT=/tmp/pilot/shofer
RUNS=/tmp/pilot/runs5
MODEL=claude-sonnet-4-6
LMCFG='{"mcpServers":{"live-memory":{"type":"http","url":"http://127.0.0.1:7711/mcp"}}}'
mkdir -p "$RUNS"

APPEND_SYS="When working in this codebase you have a 'live_memory' companion via the ask_live_memory tool (when wired). For ANY question about how the existing code works, where something is defined, what calls what, how a subsystem fits together, or project conventions, ASK live_memory FIRST - before reading, grepping or globbing source yourself. Pass the absolute repo root as cwd. Read a file directly only when you need its exact current contents to edit it. Batch related questions. If the tool is not available, fall back to reading files."
FEATURE_PROMPT="Implement a feature in this shofer codebase (a TypeScript VS Code extension) at the cwd.

FEATURE: Add a new read-only tool named \`count_lines\` that takes one parameter \`path\` (a workspace-relative file path) and returns the number of lines in that file. Wire it through the codebase exactly the way existing simple read-only tools (e.g. \`read_file\`, \`list_files\`) are wired so the project type-checks and the tool is usable: add its name to the tool-name schema/type, add any required entries to the sibling maps/records keyed by tool name, create its tool class following the BaseTool pattern, register and dispatch it.

Requirements:
- \`pnpm check-types\` in src/ MUST pass with no errors after your change.
- Do not break existing tests. Deps are installed; do NOT run any install/build command.
- Keep it minimal and consistent with existing conventions. When done, list the files you changed."

parse() { python3 - "$1" <<'PY'
import json,sys
last=None; si=so=scr=scw=0
for ln in open(sys.argv[1],errors="ignore"):
    try: e=json.loads(ln)
    except: continue
    if e.get("type")=="result": last=e
    if e.get("type")=="assistant":
        u=e.get("message",{}).get("usage",{})
        si+=u.get("input_tokens",0); so+=u.get("output_tokens",0)
        scr+=u.get("cache_read_input_tokens",0); scw+=u.get("cache_creation_input_tokens",0)
u=(last or {}).get("usage",{})
print(u.get("input_tokens",si),u.get("output_tokens",so),u.get("cache_read_input_tokens",scr),u.get("cache_creation_input_tokens",scw),"complete" if last else "KILLED")
PY
}
apierr() { grep -qE 'ConnectionRefused|API Error|"is_error":true' "$1" && echo YES || echo no; }
reset_wt() { git -C "$WT" checkout -- . >/dev/null 2>&1; git -C "$WT" clean -fd >/dev/null 2>&1; }
stats()  { curl -s "http://127.0.0.1:7711/stats?cwd=$WT" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['inputTokens'],d['outputTokens'],d['cacheReadTokens'],d['cacheWriteTokens'],d['invocations'])"; }
accept() { ( cd "$WT/src"; pnpm check-types >/tmp/tc5.log 2>&1; tc=$?
  sp=$(pnpm vitest run list-files 2>&1 | grep -E "Tests +[0-9]" | tail -1)
  diff_n=$(git -C "$WT" diff --stat | tail -1)
  sch=$(grep -q "count_lines" "$WT/packages/types/src/tool.ts" 2>/dev/null && echo yes || echo NO)
  present=$([ -n "$(git -C "$WT" status --porcelain)" ] && [ "$sch" = yes ] && echo yes || echo NO-OP)
  echo "TSC rc=$tc | $sp | schema_has_count_lines=$sch | FEATURE=$present | $diff_n"); }
# NO os timeout; max-turns is the only (clean) bound.
runf() { ( cd "$WT" && claude -p "$FEATURE_PROMPT" --model "$MODEL" --append-system-prompt "$APPEND_SYS" \
    --dangerously-skip-permissions --max-turns 200 --output-format stream-json --verbose $2 ) >"$RUNS/$1.stream.jsonl" 2>"$RUNS/$1.err"; }

echo "###### ARM II - WITHOUT ######"; reset_wt
runf without ""
echo "premium: $(parse "$RUNS/without.stream.jsonl" | tee "$RUNS/without.tok") | api_error=$(apierr "$RUNS/without.stream.jsonl")"
echo "accept: $(accept | tee "$RUNS/without.accept")"

echo "###### ARM I - WITH (warmup + feature) ######"; reset_wt
curl -s -X POST http://127.0.0.1:7711/clear -d '{"all":true}' >/dev/null
read i0 o0 cr0 cw0 v0 <<<"$(stats)"
( cd "$WT" && claude -p "Use ask_live_memory once: question='Explore and understand this codebase and give a high-level summary of its architecture, main subsystems and conventions', cwd='$WT', timeout=200. Then report it." \
    --model "$MODEL" --mcp-config "$LMCFG" --allowedTools "mcp__live-memory__ask_live_memory" \
    --dangerously-skip-permissions --max-turns 6 --output-format json ) >"$RUNS/warmup.json" 2>"$RUNS/warmup.err"
read i1 o1 cr1 cw1 v1 <<<"$(stats)"
runf with "--mcp-config $LMCFG"
read i2 o2 cr2 cw2 v2 <<<"$(stats)"
echo "premium: $(parse "$RUNS/with.stream.jsonl" | tee "$RUNS/with.tok") | api_error=$(apierr "$RUNS/with.stream.jsonl")"
echo "accept: $(accept | tee "$RUNS/with.accept")"
echo "wu_in=$((i1-i0)) wu_cr=$((cr1-cr0)) wu_cw=$((cw1-cw0)) wu_out=$((o1-o0)) wu_inv=$((v1-v0))"  >"$RUNS/cheap.txt"
echo "ft_in=$((i2-i1)) ft_cr=$((cr2-cr1)) ft_cw=$((cw2-cw1)) ft_out=$((o2-o1)) ft_inv=$((v2-v1))" >>"$RUNS/cheap.txt"
cat "$RUNS/cheap.txt"

python3 - "$RUNS" <<'PY'
import sys
R=sys.argv[1]
def toks(f):
    a=open(f).read().split(); return [int(x) for x in a[:4]], a[4] if len(a)>4 else "?"
SON=dict(i=3,o=15,cr=.30,cw=3.75); HAI=dict(i=1,o=5,cr=.10,cw=1.25)
def cost(t,p): i,o,cr,cw=t; return (i*p['i']+o*p['o']+cr*p['cr']+cw*p['cw'])/1e6
wo,wos=toks(f"{R}/without.tok"); wi,wis=toks(f"{R}/with.tok")
ch={k:int(v) for kv in open(f"{R}/cheap.txt") for k,v in (x.split('=') for x in kv.split())}
print("\n================ A/B (post cache-fix, run #2) ================")
print(f"{'PREMIUM (Sonnet)':20}{'in':>8}{'out':>8}{'cacheR':>10}{'cacheW':>9}{'$imp':>9}  status")
print(f"{'  without':20}{wo[0]:>8}{wo[1]:>8}{wo[2]:>10}{wo[3]:>9}{cost(wo,SON):>9.4f}  {wos}")
print(f"{'  with':20}{wi[0]:>8}{wi[1]:>8}{wi[2]:>10}{wi[3]:>9}{cost(wi,SON):>9.4f}  {wis}")
print(f"\n{'CHEAP (Haiku)':20}{'in':>8}{'out':>8}{'cacheR':>10}{'cacheW':>9}{'$imp':>9}  inv")
wu=[ch['wu_in'],ch['wu_out'],ch['wu_cr'],ch['wu_cw']]; ft=[ch['ft_in'],ch['ft_out'],ch['ft_cr'],ch['ft_cw']]
print(f"{'  warm-up':20}{wu[0]:>8}{wu[1]:>8}{wu[2]:>10}{wu[3]:>9}{cost(wu,HAI):>9.4f}  {ch['wu_inv']}")
print(f"{'  feature':20}{ft[0]:>8}{ft[1]:>8}{ft[2]:>10}{ft[3]:>9}{cost(ft,HAI):>9.4f}  {ch['ft_inv']}")
print(f"\nNET (one feature): premium saved ${cost(wo,SON)-cost(wi,SON):+.4f} | cheap added (feat) ${cost(ft,HAI):.4f} (+warmup ${cost(wu,HAI):.4f})")
print(f"GATE feature invocations={ch['ft_inv']} -> {'VALID' if ch['ft_inv']>0 else 'INVALID (LM unused)'}")
PY
echo "###### DONE ######"
