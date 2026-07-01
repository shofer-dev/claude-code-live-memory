"""#2 — FRESHNESS after edits (the passive-learning + auto-refresh claim).

Validates that live-memory reflects the CURRENT code, not a stale earlier read — the
"stays current as the repo changes" claim on the slide/README. Uses a self-contained
synthetic git repo in a scratch dir (so facts can be flipped precisely and nothing real
is touched), and exercises both change paths:

  A. agent-edit path  — PostToolUse: the new file bytes are teed to /notify (kind=edited,
                        contents), so the memory should update immediately, no re-read.
  B. out-of-band path — FileChanged: the file changes on disk and /notify (kind=changed,
                        no content) fires; the memory should mark it stale and re-read.

For each: warm the memory on the old fact, change the fact, ask again, and check the
answer reflects the NEW value and not the OLD one. Reports a stale-answer rate per path.

Usage:
  LM_BENCH_BASE=http://127.0.0.1:7712 LM_BENCH_SCRATCH=/tmp/lm-fresh \
    server/.venv/bin/python benchmark/harness/freshness.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.request
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE = os.environ.get("LM_BENCH_BASE", "http://127.0.0.1:7712")
SCRATCH = os.environ.get("LM_BENCH_SCRATCH", "/tmp/lm-freshness-repo")
TIMEOUT = 90


def _post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def write(rel, text):
    p = Path(SCRATCH) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def setup_repo():
    Path(SCRATCH).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=SCRATCH, check=False)
    (Path(SCRATCH) / ".git").exists() or subprocess.run(["git", "init"], cwd=SCRATCH, check=False)
    write("app/config.py",
          "# service configuration\n"
          "DEFAULT_TIMEOUT_SECONDS = 90\n"
          "MAX_RETRIES = 3\n"
          "CACHE_BACKEND = \"memory\"\n")
    write("app/auth.py",
          "def issue_token(user):\n"
          "    # tokens are valid for 3600 seconds\n"
          "    return {\"user\": user, \"ttl\": 3600}\n")


def tee(rel):
    """Simulate the agent editing a file: PostToolUse tees the new bytes."""
    content = (Path(SCRATCH) / rel).read_text(encoding="utf-8")
    return _post("/notify", {"kind": "edited", "cwd": SCRATCH,
                             "paths": [rel], "contents": {rel: content}})["applied"]


def notify_changed(rel):
    """Simulate an out-of-band change: FileChanged with no content."""
    return _post("/notify", {"kind": "changed", "event": "change", "cwd": SCRATCH, "paths": [rel]})["applied"]


async def ask(session, q):
    res = await session.call_tool("ask_live_memory", {"question": q, "cwd": SCRATCH, "timeout": TIMEOUT})
    text = res.content[0].text
    return text.split("\n\n---\n[live-memory]")[0].strip()


def reflects(ans, new, old):
    """Fresh iff the answer states the NEW value and not the stale OLD value."""
    a = ans.lower()
    return (new.lower() in a) and (old.lower() not in a)


async def main():
    setup_repo()
    _post("/clear", {"cwd": SCRATCH})
    results = []
    async with streamable_http_client(f"{BASE}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()

            # ---- Path A: agent-edit (tee content) ----
            tee("app/config.py")  # warm on the original
            a0 = await ask(s, "What is DEFAULT_TIMEOUT_SECONDS in app/config.py? Answer with the number.")
            write("app/config.py", (Path(SCRATCH) / "app/config.py").read_text().replace(
                "DEFAULT_TIMEOUT_SECONDS = 90", "DEFAULT_TIMEOUT_SECONDS = 120"))
            tee("app/config.py")  # agent edited it -> teed
            a1 = await ask(s, "What is DEFAULT_TIMEOUT_SECONDS in app/config.py right now? Answer with the number.")
            okA = reflects(a1, "120", "90")
            results.append(("A: agent-edit (tee) — timeout 90→120", okA, a1))

            # ---- Path A2: a value inside a function body, edited ----
            write("app/auth.py", (Path(SCRATCH) / "app/auth.py").read_text().replace("3600", "7200"))
            tee("app/auth.py")
            a2 = await ask(s, "In app/auth.py, how many seconds is a token valid for? Answer with the number.")
            okA2 = reflects(a2, "7200", "3600")
            results.append(("A: agent-edit (tee) — token ttl 3600→7200", okA2, a2))

            # ---- Path B: out-of-band change (FileChanged, no content) ----
            b0 = await ask(s, "What is MAX_RETRIES in app/config.py? Answer with the number.")
            write("app/config.py", (Path(SCRATCH) / "app/config.py").read_text().replace(
                "MAX_RETRIES = 3", "MAX_RETRIES = 7"))
            notify_changed("app/config.py")  # out-of-band: mark stale -> should re-read
            b1 = await ask(s, "What is MAX_RETRIES in app/config.py right now? Answer with the number.")
            okB = reflects(b1, "7", "3")
            results.append(("B: out-of-band (FileChanged) — retries 3→7", okB, b1))

            # ---- Path B2: string value changed out-of-band ----
            write("app/config.py", (Path(SCRATCH) / "app/config.py").read_text().replace(
                'CACHE_BACKEND = "memory"', 'CACHE_BACKEND = "redis"'))
            notify_changed("app/config.py")
            b2 = await ask(s, "What is CACHE_BACKEND set to in app/config.py right now? Answer with the value.")
            okB2 = reflects(b2, "redis", "memory")
            results.append(("B: out-of-band (FileChanged) — cache memory→redis", okB2, b2))

    print("\n================ FRESHNESS after edits ================")
    fresh = sum(ok for _, ok, _ in results)
    for label, ok, ans in results:
        print(f"  [{'FRESH' if ok else 'STALE'}] {label}")
        if not ok:
            print(f"          answer: {ans[:140].strip()}")
    print(f"\nfresh {fresh}/{len(results)}  ·  stale-answer rate {100*(len(results)-fresh)/len(results):.0f}%")
    print("(agent-edit paths should be fresh via the teed content; out-of-band via stale→re-read.)")


if __name__ == "__main__":
    asyncio.run(main())
