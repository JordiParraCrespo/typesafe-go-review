import json, os, re, sys, urllib.request
KEY=os.environ["TYPESAFE_API_KEY"]
def ask(state,q):
    r=urllib.request.Request("https://api.typesafe.ai/v1/systemone",json.dumps({"state":state,"model":"jev-latest","questions":q}).encode(),{"Authorization":"Bearer "+KEY,"Content-Type":"application/json"})
    o=json.load(urllib.request.urlopen(r,timeout=90)); return o["answers"],o["usage"]["input_tokens"]
S=json.load(open('slices_new.json')); V={s["type"]:s["methods"]["Validate"] for s in S["slices"]}; SENT={s["type"]:s["sentinels_returned"] for s in S["slices"]}
def preflight_checks(cpp):
    m=re.search(r"::preflight\(PreflightContext const& ctx\)\s*\{(.*?)\n\}\n", cpp, re.S)
    body=m.group(1).splitlines(); checks=[]
    for i,l in enumerate(body):
        r=re.search(r"return (tem\w+)", l)
        if not r: continue
        j=i
        while j>0 and "if (" not in body[j] and "if(" not in body[j]: j-=1
        k=j
        while k>0 and body[k-1].strip().startswith("//"): k-=1
        checks.append({"code": r.group(1), "snippet": "\n".join(x.strip() for x in body[k:i+1])})
    return checks
out={}; tokens=0
for t,cppfile in [("Payment","rippled/Payment.cpp"),("CheckCreate","rippled/CheckCreate.cpp"),("LoanSet","rippled/LoanSet.cpp")]:
    checks=preflight_checks(open(cppfile).read())
    qs={}
    for i,c in enumerate(checks):
        qs[f"enf_{i}"]={"type":"noul","instructions":{"question":"Does `go_validate` reject the same input condition that this rippled preflight check rejects?","rippled_check":c["snippet"],"rippled_result":c["code"]},"criteria":{"true":"go_validate contains a check that returns an error for the same condition (possibly via a helper such as IsAmount, IsPaths, IsFlagEnabled)","false":"go_validate never inspects this condition, or the condition concerns ledger state or amendments that client-side validation cannot see"}}
        qs[f"which_{i}"]={"type":"choice","instructions":{"question":"Which Go sentinel does `go_validate` return for the condition in `rippled_check`?","rippled_check":c["snippet"]},"criteria":{**{s:None for s in SENT[t]},"helper_error":"an error produced inside a helper such as IsAmount/IsPaths","none":"not enforced in Go"}}
    a,tk=ask({"go_validate":V[t]},qs); tokens+=tk
    rows=[]
    for i,c in enumerate(checks):
        rows.append({"tem":c["code"],"cond":c["snippet"].splitlines()[-2] if len(c["snippet"].splitlines())>1 else c["snippet"],"enforced":round(a[f"enf_{i}"]["noul"],2),"go_sentinel":a[f"which_{i}"]["choice"],"conf":round(a[f"which_{i}"]["confidence"],2)})
    out[t]=rows
    print(f"=== {t}: {len(checks)} preflight rejections, {tk} tokens")
    for r in rows: print(f"  {r['enforced']:.2f} {r['tem']:26s} -> {r['go_sentinel']:40s} ({r['conf']})  | {r['cond'][:90]}")
json.dump(out,open("preflight_xcheck_out.json","w"),indent=1); print("tokens",tokens)
