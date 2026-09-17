"""Round three: framing techniques for TypeSafe questions.
G1 pre-parsed facts vs raw source      G2 Choice over helpers instead of N Nouls
G3 answer stability vs question count  G4 structured Score levels vs plain strings
G5 diff framing (before/after/hunk)    G6 one Choice to rank 71 units
G7 line-anchored answers               G8 cheap screening Noul as a cascade stage
Usage: TYPESAFE_API_KEY=... python3 run_ideas3.py slices.json e1_results.json out.json
"""
import json, os, re, sys, time, difflib, urllib.request, concurrent.futures as cf
API = "https://api.typesafe.ai/v1/systemone"; KEY = os.environ["TYPESAFE_API_KEY"]; USAGE = {"requests": 0, "input_tokens": 0}
def ask(state, questions, retries=4):
    body = json.dumps({"state": state, "model": "jev-latest", "questions": questions}).encode()
    for i in range(retries):
        req = urllib.request.Request(API, body, {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.load(r); USAGE["requests"] += 1; USAGE["input_tokens"] += out["usage"]["input_tokens"]; return out["answers"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and i < retries - 1: time.sleep(2 ** i); continue
            raise RuntimeError(f"{e.code}: {e.read()[:300]}")
def pmap(fn, items, w=8):
    with cf.ThreadPoolExecutor(w) as ex: return list(ex.map(fn, items))

S = json.load(open(sys.argv[1])); slices = sorted(S["slices"], key=lambda s: s["type"])
E1 = {r["type"]: r["fields"] for r in json.load(open(sys.argv[2]))["E1_per_field_validation"]}   # baseline per-field nouls (raw source)
V = {s["type"]: s["methods"].get("Validate", "") for s in slices}
F = {s["type"]: [f["name"] for f in (s["fields"] or [])] for s in slices}
truth_unchecked = {t: {f for f, v in E1[t].items() if v < 0.3} for t in E1}   # verified by hand in sections 11/12
R = {}

# G1: pre-parsed facts instead of source
def calls_of(code): return sorted(set(re.findall(r"\b([A-Za-z_][\w.]*)\(([^()]*)\)", code)))
def g1(s):
    t = s["type"]; fields = F[t]
    if not fields: return None
    facts = {"receiver_fields": fields, "calls_in_validate": [f"{a}({b.strip()})" for a, b in calls_of(V[t]) if b.strip()], "conditions": re.findall(r"if ([^\n{]+)\{", V[t])}
    a = ask({"validate_facts": facts}, {f"c_{f}": {"type": "noul", "instructions": f"Based on `validate_facts`, is the field `{f}` validated (its value appears in a call argument or condition)?"} for f in fields})
    return {"type": t, "facts_unchecked": {f for f in fields if a[f"c_{f}"]["noul"] < 0.3}, "tokens_note": len(json.dumps(facts))}
r = [x for x in pmap(g1, slices) if x]
R["G1_facts_vs_source"] = {"agree": sum(x["facts_unchecked"] == truth_unchecked[x["type"]] for x in r), "n": len(r),
    "disagreements": [{"type": x["type"], "facts": sorted(x["facts_unchecked"]), "source": sorted(truth_unchecked[x["type"]])} for x in r if x["facts_unchecked"] != truth_unchecked[x["type"]]]}

# G2: one Choice per field over helper names (gives the fix hint)
HELPERS = ["IsAmount", "IsPaths", "IsPath", "IsMemo", "IsSigner", "IsAsset", "IsIssuedCurrency", "IsDomainID", "addresscodec.IsValidAddress", "typecheck.IsHex", "typecheck.IsUint32", "typecheck.IsString", "ValidateOptionalField", "IsValid()", "IsFlagEnabled", "explicit comparison (==, !=, <, >, len)", "not validated"]
def g2(s):
    t = s["type"]; fields = F[t]
    if not fields: return None
    a = ask({"function": V[t]}, {f"h_{f}": {"type": "choice", "instructions": f"Which helper or check validates the struct field `{f}` in `function`?", "criteria": {h: None for h in HELPERS}} for f in fields})
    return {"type": t, "per_field": {f: (a[f"h_{f}"]["choice"], round(a[f"h_{f}"]["confidence"], 2)) for f in fields}}
r = [x for x in pmap(g2, slices) if x]
agree = sum((x["per_field"][f][0] == "not validated") == (f in truth_unchecked[x["type"]]) for x in r for f in x["per_field"])
R["G2_choice_over_helpers"] = {"fields": sum(len(x["per_field"]) for x in r), "agree_with_truth": agree, "sample": {x["type"]: x["per_field"] for x in r if x["type"] in ("Payment", "TrustSet", "CheckCreate")}}

# G3: stability vs number of questions in one request
def g3(s):
    t = s["type"]; fields = F[t]
    if len(fields) < 3: return None
    base = {f"c_{f}": {"type": "noul", "instructions": f"Does `function` validate the struct field `{f}`?"} for f in fields}
    small = ask({"function": V[t]}, base)
    filler = {f"x{i}": {"type": "noul", "instructions": q} for i, q in enumerate(["Does the function call BaseTx.Validate first?", "Does the function ever panic?", "Does it return true, nil at the end?", "Does it use errors.New inline?", "Does it compare flags with numeric literals?", "Does it have a doc comment?", "Does it use a helper named IsAmount?", "Does it check an address with addresscodec?", "Does it validate a hash with IsHex?", "Does it use ValidateOptionalField?", "Is any check duplicated?", "Does it validate a URI?", "Does it check Expiration?", "Does it check Flags at all?", "Does it call a check* helper?", "Does it return a wrapped error?", "Does it use a switch?", "Does it loop over a slice?", "Does it mutate the receiver?", "Does it call Flatten?"] * 2)}
    big = ask({"function": V[t]}, {**base, **filler})
    return {"type": t, "n_small": len(base), "n_big": len(base) + len(filler), "max_abs_delta": max(abs(small[k]["noul"] - big[k]["noul"]) for k in base), "flips": sum((small[k]["noul"] >= .5) != (big[k]["noul"] >= .5) for k in base)}
r = [x for x in pmap(g3, slices[:30]) if x]
R["G3_question_count"] = {"n": len(r), "flips": sum(x["flips"] for x in r), "worst_delta": max(x["max_abs_delta"] for x in r), "mean_max_delta": round(sum(x["max_abs_delta"] for x in r) / len(r), 3)}

# G4: structured Score levels (description + example) vs plain strings
PLAIN = ["Errors ignored, swallowed, or panics", "Errors returned but with ad hoc inline strings and some checks missing", "Consistently checked and returned; mostly sentinel Err* with minor deviations", "Every error path checked and returned, only package-level sentinel Err* variables"]
STRUCT = [{"description": PLAIN[0], "example": "if err != nil { panic(err) }  // or: _ = f()"}, {"description": PLAIN[1], "example": 'return false, errors.New("bad amount")'}, {"description": PLAIN[2], "example": "return false, ErrInvalidAmount  // plus one errors.New elsewhere"}, {"description": PLAIN[3], "example": "if ok, err := IsAmount(p.Amount, \"Amount\", true); !ok { return false, err }"}]
def mutate_panic(c): return c.replace("return false, err\n", "panic(err)\n", 1)
def mutate_inline(c):
    m = re.search(r"return false, (Err\w+)", c); return c.replace(m.group(0), 'return false, errors.New("invalid field")', 1) if m else c
def g4(item):
    t, code, kind = item
    a = ask({"function": code}, {"plain": {"type": "score", "instructions": "Rate the error handling of `function`.", "criteria": PLAIN}, "structured": {"type": "score", "instructions": "Rate the error handling of `function`.", "criteria": STRUCT}})
    return {"type": t, "kind": kind, "plain": (round(a["plain"]["score"], 2), round(a["plain"]["confidence"], 2)), "structured": (round(a["structured"]["score"], 2), round(a["structured"]["confidence"], 2))}
items = []
for s in slices[:12]:
    items += [(s["type"], V[s["type"]], "original"), (s["type"], mutate_panic(V[s["type"]]), "panic"), (s["type"], mutate_inline(V[s["type"]]), "inline")]
r = pmap(g4, items)
def sep(key): 
    o = [x[key][0] for x in r if x["kind"] == "original"]; p = [x[key][0] for x in r if x["kind"] == "panic"]; i = [x[key][0] for x in r if x["kind"] == "inline"]
    return {"mean_original": round(sum(o)/len(o), 2), "mean_panic": round(sum(p)/len(p), 2), "mean_inline": round(sum(i)/len(i), 2), "mean_conf": round(sum(x[key][1] for x in r)/len(r), 2)}
R["G4_structured_levels"] = {"plain": sep("plain"), "structured": sep("structured")}

# G5: diff framing: ask about the change, not the result
def g5(s):
    t = s["type"]; code = V[t]; fields = F[t]
    blocks = list(re.finditer(r"\n\tif [^\n]*\{\n(?:\t\t[^\n]*\n)+?\t\}\n", code))
    if len(blocks) < 3: return None
    b = blocks[len(blocks)//2]; removed_text = code[b.start():b.end()]; after = code[:b.start()] + "\n" + code[b.end():]
    hunk = "".join(difflib.unified_diff(code.splitlines(1), after.splitlines(1), "before", "after", n=2))
    q = {"removes_check": {"type": "noul", "instructions": "Does this change remove or weaken a validation check?"}, "safe": {"type": "noul", "instructions": "Is this change behaviour-preserving?"}}
    with_diff = ask({"diff": hunk}, q); with_pair = ask({"before": code, "after": after}, q); after_only = ask({"function": after}, {"removes_check": {"type": "noul", "instructions": "Is a validation check missing from this function compared with what its fields need?"}})
    return {"type": t, "removed": removed_text.strip().splitlines()[0], "diff_only": round(with_diff["removes_check"]["noul"], 2), "pair": round(with_pair["removes_check"]["noul"], 2), "after_only": round(after_only["removes_check"]["noul"], 2), "diff_tokens_vs_pair": None}
r = [x for x in pmap(g5, slices[:25]) if x]
R["G5_diff_framing"] = {"n": len(r), "detected_diff_only": sum(x["diff_only"] >= .5 for x in r), "detected_pair": sum(x["pair"] >= .5 for x in r), "detected_after_only": sum(x["after_only"] >= .5 for x in r), "rows": r[:6]}

# G6: rank all 71 in one Choice
opts = {s["type"]: None for s in slices}
a = ask({"validate_methods": {s["type"]: V[s["type"]] for s in slices}}, {"least": {"type": "choice", "instructions": "Which entry in `validate_methods` validates the smallest share of its struct's fields?", "criteria": opts}, "most": {"type": "choice", "instructions": "Which entry in `validate_methods` is the most complete and convention-following Validate?", "criteria": opts}})
ratio = {t: (len(F[t]) - len(truth_unchecked[t])) / len(F[t]) for t in truth_unchecked if F[t]}
top = sorted(a["least"]["probabilities"].items(), key=lambda kv: -kv[1])[:5]
R["G6_rank_in_one_call"] = {"least_choice": a["least"]["choice"], "least_conf": round(a["least"]["confidence"], 2), "least_top5": [(t, round(p, 2), round(ratio.get(t, 1), 2)) for t, p in top], "truth_lowest_ratio": sorted(ratio.items(), key=lambda kv: kv[1])[:5], "most_choice": a["most"]["choice"], "most_conf": round(a["most"]["confidence"], 2), "tokens": USAGE["input_tokens"]}

# G7: line-anchored answers
def g7(s):
    t = s["type"]; lines = V[t].splitlines(); numbered = {str(i+1): l for i, l in enumerate(lines)}
    checked = [f for f in F[t] if f not in truth_unchecked[t]][:3]
    if not checked: return None
    a = ask({"lines": numbered}, {f"L_{f}": {"type": "choice", "instructions": f"Which line id in `lines` contains the check that validates the field `{f}`?", "criteria": {k: None for k in numbered}} for f in checked})
    out = []
    for f in checked:
        ln = a[f"L_{f}"]["choice"]; ok = bool(re.search(r"\b" + f + r"\b", lines[int(ln)-1])) or (int(ln) > 1 and re.search(r"\b" + f + r"\b", lines[int(ln)-2]) is not None)
        out.append((f, ln, round(a[f"L_{f}"]["confidence"], 2), ok))
    return {"type": t, "answers": out}
r = [x for x in pmap(g7, slices[:30]) if x]
R["G7_line_anchoring"] = {"n": sum(len(x["answers"]) for x in r), "correct": sum(o[3] for x in r for o in x["answers"]), "sample": r[:4]}

# G8: screening Noul on the whole Validate, then per-field only on hits
def g8(s):
    t = s["type"]
    if not F[t]: return None
    a = ask({"struct_fields": F[t], "function": V[t]}, {"any_missing": {"type": "noul", "instructions": "Is at least one of `struct_fields` (other than DestinationTag) never validated by `function`?"}})
    return {"type": t, "screen": round(a["any_missing"]["noul"], 2), "truth": bool(truth_unchecked[t] - {"DestinationTag"})}
r = [x for x in pmap(g8, slices) if x]
R["G8_screening"] = {"n": len(r), "tp": sum(x["screen"] >= .5 and x["truth"] for x in r), "fp": sum(x["screen"] >= .5 and not x["truth"] for x in r), "fn": sum(x["screen"] < .5 and x["truth"] for x in r), "tn": sum(x["screen"] < .5 and not x["truth"] for x in r), "missed": [x["type"] for x in r if x["screen"] < .5 and x["truth"]]}

R["usage"] = USAGE
json.dump(R, open(sys.argv[3], "w"), indent=1, default=list); print(json.dumps(USAGE))
