"""Run the design ideas from docs/research.md section 10 against xrpl-go slices.

Usage: TYPESAFE_API_KEY=... python3 experiments/run_ideas.py slices.json slices_rpc.json slices_websocket.json out.json
Requires only the standard library.
"""
import json, os, re, sys, time, urllib.request, concurrent.futures as cf

API = "https://api.typesafe.ai/v1/systemone"
KEY = os.environ["TYPESAFE_API_KEY"]
MODEL = "jev-latest"
USAGE = {"requests": 0, "input_tokens": 0}

def ask(state, questions, retries=4):
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    for i in range(retries):
        req = urllib.request.Request(API, body, {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.load(r)
                USAGE["requests"] += 1; USAGE["input_tokens"] += out["usage"]["input_tokens"]
                return out["answers"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and i < retries - 1:
                time.sleep(2 ** i); continue
            raise RuntimeError(f"{e.code}: {e.read()[:300]}")

def pmap(fn, items, workers=8):
    with cf.ThreadPoolExecutor(workers) as ex:
        return list(ex.map(fn, items))

tx, rpc, ws = [json.load(open(p)) for p in sys.argv[1:4]]
slices = sorted(tx["slices"], key=lambda s: s["type"])
results = {}

# ---------- E1: AST-generated per-field validation Nouls, all 56 types ----------
def e1(s):
    v = s["methods"].get("Validate", "")
    fields = [f["name"] for f in (s["fields"] or [])]
    if not fields: return None
    qs = {f"checks_{f}": {"type": "noul", "instructions": f"Does `function` validate the struct field `{f}` (with a helper call, an IsValid method, or an explicit check on its value)?"} for f in fields}
    a = ask({"struct_fields": fields, "function": v}, qs)
    return {"type": s["type"], "fields": {f: round(a[f"checks_{f}"]["noul"], 2) for f in fields},
            "static_mentions": {f: bool(re.search(r"\b" + re.escape(f) + r"\b", v)) for f in fields}}
results["E1_per_field_validation"] = [r for r in pmap(e1, slices) if r]

# ---------- E2: convention mining: Flatten uses constant vs literal ----------
def e2(batch):
    fns = {s["type"]: s["methods"].get("Flatten", "") for s in batch}
    qs = {f"{t}": {"type": "noul", "instructions": f"In `functions.{t}`, is the TransactionType entry set from a typed constant's String() method (e.g. `XxxTx.String()`) rather than from a raw string literal?"} for t in fns}
    a = ask({"functions": fns}, qs)
    out = []
    for s in batch:
        truth_literal = bool(re.search(r'\["TransactionType"\]\s*=\s*"', fns[s["type"]]))
        out.append({"type": s["type"], "uses_constant_noul": round(a[s["type"]]["noul"], 2), "truth_uses_constant": not truth_literal})
    return out
batches = [slices[i:i+7] for i in range(0, len(slices), 7)]
results["E2_convention_mining_flatten"] = [r for b in pmap(e2, batches) for r in b]

# ---------- E3: error-path test coverage, semantic vs grep ----------
def e3(s):
    errs = s["sentinels_returned"]; t = s["tests"].get(f"Test{s['type']}_Validate", "")
    if not errs or not t: return None
    qs = {f"covers_{e}": {"type": "noul", "instructions": f"Does `test` contain a case whose expected error is `{e}`, or that exercises the input condition that makes `function` return it?"} for e in errs}
    a = ask({"function": s["methods"]["Validate"], "test": t}, qs)
    return {"type": s["type"], "errors": {e: {"noul": round(a[f"covers_{e}"]["noul"], 2), "grep": e in t} for e in errs}}
results["E3_error_path_coverage"] = [r for r in pmap(e3, slices) if r]

# ---------- E4: self-consistency via paraphrase on E1 ----------
def e4(s):
    v = s["methods"].get("Validate", ""); fields = [f["name"] for f in (s["fields"] or [])]
    if not fields: return None
    qs = {}
    for f in fields:
        qs[f"a_{f}"] = {"type": "noul", "instructions": f"Is there any code path in `function` that checks the value of `{f}` before returning true?"}
        qs[f"b_{f}"] = {"type": "noul", "instructions": f"Would an invalid `{f}` be rejected by `function`?", "criteria": {"true": "Some check on this field can make the function return false", "false": "The field is never inspected"}}
    a = ask({"struct_fields": fields, "function": v}, qs)
    return {"type": s["type"], "fields": {f: [round(a[f"a_{f}"]["noul"], 2), round(a[f"b_{f}"]["noul"], 2)] for f in fields}}
results["E4_paraphrase"] = [r for r in pmap(e4, slices) if r]

# ---------- E5: anchor-relative Choice against Payment.Validate ----------
anchor = next(s for s in slices if s["type"] == "Payment")["methods"]["Validate"]
CONV = "Validate() calls BaseTx.Validate() first, checks every struct field with the designated helper (IsAmount, IsPaths, addresscodec.IsValidAddress, typecheck.IsHex, <typed>.IsValid()), returns (false, ErrXxx sentinel) on failure and (true, nil) at the end, never panics, uses named tf* constants for flags."
def e5(s):
    if s["type"] == "Payment": return None
    a = ask({"conventions": CONV, "A": anchor, "B": s["methods"]["Validate"]},
            {"cmp": {"type": "choice", "instructions": "Which Validate method follows `conventions` more completely?",
                     "criteria": {"A_better": "A follows the conventions more completely", "B_better": "B follows the conventions more completely", "equivalent": "Both follow them to the same degree"}}})
    c = a["cmp"]
    return {"type": s["type"], "choice": c["choice"], "confidence": round(c["confidence"], 2), "p": {k: round(v, 2) for k, v in c["probabilities"].items()}}
results["E5_anchor_relative"] = [r for r in pmap(e5, slices) if r]

# ---------- E6: duplicate sentinel detection websocket -> rpc ----------
rpc_names = {s["name"]: s["message"] for s in rpc["sentinels"]}
def e6(batch):
    qs = {}
    for s in batch:
        crit = {n: m for n, m in rpc_names.items()}; crit["none"] = "No rpc sentinel describes the same failure"
        qs[s["name"]] = {"type": "choice", "instructions": {"question": f"Which rpc sentinel (by name and message) describes the same failure as websocket sentinel `{s['name']}` with message `{s['message']}`?"}, "criteria": crit}
    a = ask({"websocket_sentinels": [{"name": s["name"], "message": s["message"]} for s in batch]}, qs)
    return [{"ws": s["name"], "ws_msg": s["message"], "match": a[s["name"]]["choice"], "confidence": round(a[s["name"]]["confidence"], 2), "same_name_exists": s["name"] in rpc_names} for s in batch]
wsb = [ws["sentinels"][i:i+10] for i in range(0, len(ws["sentinels"]), 10)]
results["E6_duplicate_sentinels"] = [r for b in pmap(e6, wsb) for r in b]

results["usage"] = USAGE
json.dump(results, open(sys.argv[4], "w"), indent=1)
print(json.dumps(USAGE))
