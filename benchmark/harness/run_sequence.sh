#!/usr/bin/env bash
# COMPOUNDING SEQUENCE A/B (the real claim): a sequence of read-only tools added to
# shofer's tool-wiring machinery (F1 "schema-as-contract" regime), one per feature,
# by a FRESH COLD agent each time, on an ACCUMULATING worktree. The with-arm's Live
# Memory persists across the whole sequence (and passive ingestion warms it from each
# feature's own Read/Edit/Write), so later features reuse what earlier ones taught it;
# the without-arm re-explores the same wiring every feature. Hypothesis: with-arm
# per-feature premium cost DROPS as the sequence warms, while without stays flat →
# cumulative divergence (which single-feature runs can't show).
#
# Pinned worktrees (reproducible, isolated from the live extensions/shofer working tree):
#   without -> $WO_WT (default /tmp/pilot/pwt1), with -> $WI_WT (default /tmp/pilot/pwt2)
# Both reset to the pinned base at sequence start; the worktree then ACCUMULATES across
# features within the run. LM is cleared once at run start (clean slate), then persists.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; AN="$HERE/analyze.py"
WO_WT=${WO_WT:-/tmp/pilot/pwt1}
WI_WT=${WI_WT:-/tmp/pilot/pwt2}
RUNS=${RUNS:-/tmp/pilot/sequence}
MODEL=${MODEL:-claude-sonnet-4-6}
LMCFG='{"mcpServers":{"live-memory":{"type":"http","url":"http://127.0.0.1:7711/mcp"}}}'
HANG_GUARD=${HANG_GUARD:-1800}
SCHEMA=packages/types/src/tool.ts
mkdir -p "$RUNS"

APPEND_SYS="When working in this codebase you have a 'live_memory' companion via the ask_live_memory tool (when wired). For ANY question about how the existing code works, where something is defined, what calls what, how a subsystem fits together, or project conventions, ASK live_memory FIRST - before reading, grepping or globbing source yourself. Pass the absolute repo root as cwd. Read a file directly only when you need its exact current contents to edit it. Batch related questions. If the tool is not available, fall back to reading files."

# Each feature: "name|semantics". All are read-only tools taking one param `path`
# returning a number, wired exactly like the existing read_file/list_files tools.
FEATURES=(
  "count_lines|the number of lines in that file"
  "count_chars|the total number of characters (Unicode code points) in that file"
  "count_bytes|the size of that file in bytes"
  "count_words|the number of whitespace-separated words in that file"
)

feature_prompt() { # $1=name $2=semantics
  cat <<EOF
Implement a feature in this shofer codebase (a TypeScript VS Code extension) at the cwd.

FEATURE: Add a new read-only tool named \`$1\` that takes one parameter \`path\` (a workspace-relative file path) and returns $2. Wire it through the codebase exactly the way existing simple read-only tools (e.g. \`read_file\`, \`list_files\`) are wired so the project type-checks and the tool is usable: add its name to the tool-name schema/type, add any required entries to the sibling maps/records keyed by tool name, create its tool class following the BaseTool pattern, register and dispatch it.

Requirements:
- \`pnpm check-types\` in src/ MUST pass with no errors after your change.
- Do not break existing tests. Deps are installed; do NOT run any install/build command.
- Keep it minimal and consistent with existing conventions. When done, list the files you changed.
EOF
}

TO(){ [ "$HANG_GUARD" -gt 0 ] && echo "timeout $HANG_GUARD" || echo ""; }
stats(){ curl -s "http://127.0.0.1:7711/stats?cwd=$1"|python3 -c "import json,sys;d=json.load(sys.stdin);print(d['inputTokens'],d['outputTokens'],d['cacheReadTokens'],d['cacheWriteTokens'],d['invocations'])" 2>/dev/null || echo "0 0 0 0 0"; }
accept(){ # $1=wt $2=name -> "tc_rc|schema_has"
  local tc sch
  ( cd "$1/src" && pnpm check-types >/dev/null 2>&1 ); tc=$?
  grep -q "\"$2\"" "$1/$SCHEMA" 2>/dev/null && sch=yes || sch=NO
  echo "$tc|$sch"
}
row(){ # $1=json $2=arm $3=fidx $4=name $5=accept $6..=cheap(in cr cw out inv)
  python3 -c "import json;d=json.load(open('$1'));print('$2,$3,$4,'+','.join(str(d[k]) for k in ['turns','read_calls','read_tok','lm_calls','lm_tok','edit_calls','pin','pout','pcr','pcw','status','api_fail'])+',$5,${6:-},${7:-},${8:-},${9:-},${10:-}')"; }

reset_wt(){ git -C "$1" checkout -- . >/dev/null 2>&1; git -C "$1" clean -fd >/dev/null 2>&1; }

run_feature(){ # $1=wt $2=mcp_args $3=outfile
  ( cd "$1" && $(TO) claude -p "$4" --model "$MODEL" --append-system-prompt "$APPEND_SYS" --strict-mcp-config \
      --dangerously-skip-permissions --max-turns 200 --output-format stream-json --verbose $2 ) >"$3" 2>/dev/null
}

CSV="$RUNS/results.csv"
echo "arm,fidx,name,turns,read_calls,read_tok,lm_calls,lm_tok,edit_calls,pin,pout,pcr,pcw,status,api_fail,accept,ch_in,ch_cr,ch_cw,ch_out,ch_inv" > "$CSV"

echo "###### ARM: WITHOUT (cold agent per feature, no Live Memory) ######"
reset_wt "$WO_WT"
for i in "${!FEATURES[@]}"; do
  name="${FEATURES[$i]%%|*}"; sem="${FEATURES[$i]#*|}"; prompt="$(feature_prompt "$name" "$sem")"
  out="$RUNS/wo_f${i}.jsonl"; run_feature "$WO_WT" "" "$out" "$prompt"
  python3 "$AN" "$out" > "$RUNS/wo_f${i}.json"
  acc="$(accept "$WO_WT" "$name")"
  row "$RUNS/wo_f${i}.json" without "$i" "$name" "$acc" >> "$CSV"
  echo "  [without] f$i $name -> $(python3 -c "import json;d=json.load(open('$RUNS/wo_f${i}.json'));print('turns',d['turns'],'read_tok',d['read_tok'],'accept',\"$acc\")")"
done

echo "###### ARM: WITH (Live Memory persists across the sequence) ######"
reset_wt "$WI_WT"
curl -s -X POST http://127.0.0.1:7711/clear -d "{\"cwd\":\"$WI_WT\"}" >/dev/null
echo "  warm-up explore query..."
( cd "$WI_WT" && $(TO) claude -p "Use ask_live_memory once: question='Explore and understand this codebase and give a high-level summary of its architecture, main subsystems and conventions, especially how a read-only tool is defined and wired end-to-end', cwd='$WI_WT', timeout=240. Then report it." \
    --model "$MODEL" --mcp-config "$LMCFG" --strict-mcp-config --allowedTools "mcp__live-memory__ask_live_memory" \
    --dangerously-skip-permissions --max-turns 6 --output-format json ) >"$RUNS/wi_warmup.json" 2>/dev/null
for i in "${!FEATURES[@]}"; do
  name="${FEATURES[$i]%%|*}"; sem="${FEATURES[$i]#*|}"; prompt="$(feature_prompt "$name" "$sem")"
  read i0 o0 r0 w0 v0 <<<"$(stats "$WI_WT")"
  out="$RUNS/wi_f${i}.jsonl"; run_feature "$WI_WT" "--mcp-config $LMCFG" "$out" "$prompt"
  read i1 o1 r1 w1 v1 <<<"$(stats "$WI_WT")"
  python3 "$AN" "$out" > "$RUNS/wi_f${i}.json"
  acc="$(accept "$WI_WT" "$name")"
  row "$RUNS/wi_f${i}.json" with "$i" "$name" "$acc" $((i1-i0)) $((r1-r0)) $((w1-w0)) $((o1-o0)) $((v1-v0)) >> "$CSV"
  echo "  [with] f$i $name -> $(python3 -c "import json;d=json.load(open('$RUNS/wi_f${i}.json'));print('turns',d['turns'],'read_tok',d['read_tok'],'lm_calls',d['lm_calls'],'accept',\"$acc\")")"
done

echo "###### DONE -> $CSV ######"
column -t -s, "$CSV"
