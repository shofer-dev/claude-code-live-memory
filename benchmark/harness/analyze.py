import json,sys
# Mechanism metrics from a claude -p stream-json transcript.
READ={"Read","Grep","Glob"}
def est(x):
    if isinstance(x,list): x="".join(b.get("text","") for b in x if isinstance(b,dict))
    return len(x or "")//4
def analyze(path):
    id2name={}; read_calls=read_tok=lm_calls=lm_tok=edit_calls=0; last=None
    for ln in open(path,errors="ignore"):
        try:e=json.loads(ln)
        except:continue
        t=e.get("type")
        if t=="result": last=e
        if t=="assistant":
            for b in e.get("message",{}).get("content",[]):
                if isinstance(b,dict) and b.get("type")=="tool_use":
                    id2name[b.get("id")]=b.get("name","")
        if t=="user":
            c=e.get("message",{}).get("content",[])
            if isinstance(c,list):
                for b in c:
                    if isinstance(b,dict) and b.get("type")=="tool_result":
                        nm=id2name.get(b.get("tool_use_id"),"")
                        tok=est(b.get("content",""))
                        if nm in READ: read_calls+=1; read_tok+=tok
                        elif "ask_live_memory" in nm: lm_calls+=1; lm_tok+=tok
                        elif nm in ("Edit","Write"): edit_calls+=1
    u=(last or {}).get("usage",{})
    # API failure = the run ENDED in a connection error (not any transient mid-stream
    # tool/API error, which agents routinely recover from).
    res=str((last or {}).get("result","") or "")
    api_fail="YES" if (last and last.get("is_error") and ("API Error" in res or "ConnectionRefused" in res or "Unable to connect" in res)) else "no"
    return dict(turns=(last or {}).get("num_turns",0),
        read_calls=read_calls, read_tok=read_tok, lm_calls=lm_calls, lm_tok=lm_tok, edit_calls=edit_calls,
        pin=u.get("input_tokens",0), pout=u.get("output_tokens",0),
        pcr=u.get("cache_read_input_tokens",0), pcw=u.get("cache_creation_input_tokens",0),
        status="complete" if last else "KILLED", api_fail=api_fail)
if __name__=="__main__":
    print(json.dumps(analyze(sys.argv[1])))
