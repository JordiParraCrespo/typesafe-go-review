"""Second round of ideas: rule files as questions, mutation calibration,
before/after on git history, Flatten completeness.

Usage: TYPESAFE_API_KEY=... python3 experiments/run_ideas2.py <xrpl-go dir> slices.json out.json
"""
import json, os, re, subprocess, sys, time, urllib.request, concurrent.futures as cf

API = "https://api.typesafe.ai/v1/systemone"; KEY = os.environ["TYPESAFE_API_KEY"]
USAGE = {"requests": 0, "input_tokens": 0}
def ask(state, questions, retries=4):
    body = json.dumps({"state": state, "model": "jev-latest", "questions": questions}).encode()
    for i in range(retries):
        req = urllib.request.Request(API, body, {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.load(r); USAGE["requests"] += 1; USAGE["input_tokens"] += out["usage"]["input_tokens"]; return out["answers"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and i < retries - 1: time.sleep(2 ** i); continue
            raise RuntimeError(f"{e.code}: {e.read()[:300]}")
def pmap(fn, items, workers=8):
    with cf.ThreadPoolExecutor(workers) as ex: return list(ex.map(fn, items))

REPO = sys.argv[1]; S = json.load(open(sys.argv[2])); slices = sorted(S["slices"], key=lambda s: s["type"])
V = {s["type"]: s["methods"].get("Validate", "") for s in slices}
results = {}

# ---------- F1: rule files -> questions ----------
def parse_rule(path):
    t = open(path).read()
    title = re.search(r"^## (.+)$", t, re.M).group(1).strip()
    impact = re.search(r"\*\*Impact: (\w+)\*\*", t).group(1)
    inc = re.search(r"\*\*Incorrect:\*\*\s*```go\n(.*?)```", t, re.S).group(1).strip()
    cor = re.search(r"\*\*Correct:\*\*\s*```go\n(.*?)```", t, re.S).group(1).strip()
    why = re.search(r"Why this matters: (.+?)(?:\n\n|\Z)", t, re.S); why = why.group(1).strip() if why else ""
    return {"id": os.path.basename(path)[:-3], "title": title, "impact": impact, "incorrect": inc, "correct": cor, "why": why}

def rule_question(rule):
    return {"type": "noul", "instructions": {"rule": rule["title"], "question": "Does `code` exhibit the anti-pattern this rule describes?", "why": rule["why"][:400]},
            "criteria": {"true": {"description": "The code matches the Incorrect example's pattern", "example": rule["incorrect"]},
                         "false": {"description": "The code matches the Correct example's pattern or the rule does not apply", "example": rule["correct"]}}}

def go_funcs(path):
    src = open(path).read(); out = []
    for m in re.finditer(r"^func (?:\([^)]*\) )?(\w+)\([^\n]*\{$", src, re.M):
        start = m.start(); end = src.find("\n}\n", start); out.append((m.group(1), src[start:end + 2]))
    return out

rules = {r["id"]: r for r in map(parse_rule, [f"{REPO}/.agents/skills/go-errors/rules/error-wrapping-context.md", f"{REPO}/.agents/skills/go-errors/rules/error-panicking.md", f"{REPO}/.agents/skills/go-design/rules/function-receiver-types.md", f"{REPO}/.agents/skills/go-design/rules/code-documentation.md"])}

# F1a: %w wrapping on functions that use fmt.Errorf (ground truth: has %w or no error value passed)
cands = []
for root, _, files in os.walk(f"{REPO}/xrpl"):
    for f in files:
        if f.endswith(".go") and not f.endswith("_test.go") and "examples" not in root:
            for name, body in go_funcs(os.path.join(root, f)):
                if "fmt.Errorf" in body: cands.append((os.path.relpath(os.path.join(root, f), REPO), name, body))
def f1a(c):
    path, name, body = c
    a = ask({"code": body}, {"q": rule_question(rules["error-wrapping-context"])})
    errorf = re.findall(r'fmt\.Errorf\(([^\n]*)\)', body)
    truth_bad = any(("%v" in e or "%s" in e) and "err" in e and "%w" not in e for e in errorf) or any(re.search(r'errors\.New\("[^"]*"\)', l) for l in body.splitlines() if "err" in l and "return" in l and "Err" not in l)
    return {"file": path, "func": name, "noul": round(a["q"]["noul"], 2), "truth_antipattern": truth_bad}
results["F1a_wrap_with_w"] = pmap(f1a, cands)

# F1b: receiver consistency per type (ground truth from receivers in all methods)
def f1b(s):
    ms = list(s["methods"].values())
    if len(ms) < 3: return None
    recv = [re.match(r"func \((\w*) ?\*?(\w+)\)", m) for m in ms]
    names = {r.group(1) for r in recv if r}
    a = ask({"code": "\n\n".join(ms)}, {"q": rule_question(rules["function-receiver-types"])})
    return {"type": s["type"], "noul": round(a["q"]["noul"], 2), "receiver_names": sorted(names), "truth_antipattern": len([n for n in names if n]) > 1 or any(len(n) > 2 for n in names)}
results["F1b_receiver_consistency"] = [r for r in pmap(f1b, slices) if r]

# F1c: panic rule on all Validate methods (ground truth: none panic) + 3 mutated copies that do
def f1c(item):
    t, code, truth = item
    a = ask({"code": code}, {"q": rule_question(rules["error-panicking"])})
    return {"type": t, "noul": round(a["q"]["noul"], 2), "truth_antipattern": truth}
items = [(s["type"], V[s["type"]], False) for s in slices[:20]]
for s in slices[:3]:
    items.append((s["type"] + "+panic", V[s["type"]].replace("return false, err\n", "panic(err)\n", 1), True))
results["F1c_panic"] = pmap(f1c, items)

# ---------- F2: mutation-based calibration ----------
CRIT_ERR = ["Errors ignored, swallowed, or panics", "Errors returned but with ad hoc inline strings and some checks missing", "Consistently checked and returned; mostly sentinel Err* with minor deviations", "Every error path checked and returned, only package-level sentinel Err* variables"]
CRIT_VAL = ["Most fields unchecked or checked with naive comparisons", "Some fields validated with helpers, several fields skipped or checked ad hoc", "Most fields validated with the proper helpers, one or two gaps", "Every field validated with the designated helper and the base transaction validated first"]
def score_q():
    return {"err": {"type": "score", "instructions": "Rate the error handling of `function` per project convention.", "criteria": CRIT_ERR},
            "val": {"type": "score", "instructions": "Rate how completely `function` validates each field of the struct with the project helpers.", "criteria": CRIT_VAL}}
def mutations(code):
    out = {"original": code}
    blocks = list(re.finditer(r"\n\tif [^\n]*\{\n(?:\t\t[^\n]*\n)+?\t\}\n", code))
    if len(blocks) > 2:
        b = blocks[len(blocks) // 2]; out["drop_one_check"] = code[:b.start()] + "\n" + code[b.end():]
    m = re.search(r"return false, (Err\w+)", code)
    if m: out["inline_error"] = code.replace(m.group(0), 'return false, errors.New("invalid field")', 1)
    if "return false, err\n" in code: out["panic_on_err"] = code.replace("return false, err\n", "panic(err)\n", 1)
    out["swallow_error"] = re.sub(r"_, err := (\w+)\.BaseTx\.Validate\(\)\n\tif err != nil \{\n\t\treturn false, err\n\t\}", r"_, _ = \1.BaseTx.Validate()", code, 1)
    if out["swallow_error"] == code: del out["swallow_error"]
    return out
def f2(s):
    fields = [f["name"] for f in (s["fields"] or [])]
    res = {}
    for name, code in mutations(V[s["type"]]).items():
        qs = score_q(); qs.update({f"chk_{f}": {"type": "noul", "instructions": f"Does `function` validate the struct field `{f}`?"} for f in fields})
        a = ask({"struct_fields": fields, "function": code}, qs)
        res[name] = {"err": round(a["err"]["score"], 2), "err_conf": round(a["err"]["confidence"], 2), "val": round(a["val"]["score"], 2), "fields_checked": sum(a[f"chk_{f}"]["noul"] >= 0.5 for f in fields), "n_fields": len(fields)}
    return {"type": s["type"], "variants": res}
sample = [s for s in slices if s["type"] in ("Payment", "CheckCash", "TrustSet", "EscrowCreate", "AMMCreate", "NFTokenMint", "OfferCreate", "Clawback", "CredentialCreate", "VaultCreate")]
results["F2_mutation_calibration"] = pmap(f2, sample)

# ---------- F3: before/after on real git history ----------
def old_validate(file):
    src = subprocess.run(["git", "-C", REPO, "show", f"af7ba71:xrpl/transaction/{file}"], capture_output=True, text=True).stdout
    m = re.search(r"func \([^)]*\) Validate\(\) \(bool, error\) \{.*?\n\}", src, re.S); return m.group(0) if m else None
CONV = "Validate() calls BaseTx.Validate() first, checks every struct field with the designated helper, returns (false, ErrXxx) on failure and (true, nil) at the end, never panics."
def f3(item):
    t, file = item; old = old_validate(file); new = V[t]
    if not old or old == new: return {"type": t, "skipped": "no change"}
    q = {"cmp": {"type": "choice", "instructions": "Which version of the same Validate method follows `conventions` more completely?", "criteria": {"A_better": "A is more complete", "B_better": "B is more complete", "equivalent": "Same degree"}}}
    ab = ask({"conventions": CONV, "A": old, "B": new}, q)["cmp"]; ba = ask({"conventions": CONV, "A": new, "B": old}, q)["cmp"]
    return {"type": t, "old_first": (ab["choice"], round(ab["confidence"], 2)), "new_first": (ba["choice"], round(ba["confidence"], 2)), "new_wins_both_orders": ab["choice"] == "B_better" and ba["choice"] == "A_better"}
results["F3_before_after"] = pmap(f3, [("EscrowFinish", "escrow_finish.go"), ("NFTokenCreateOffer", "nftoken_create_offer.go"), ("NFTokenModify", "nftoken_modify.go"), ("MPTokenAuthorize", "mptoken_authorize.go"), ("OracleSet", "oracle_set.go"), ("Payment", "payment.go"), ("AccountSet", "account_set.go"), ("Clawback", "clawback.go")])

# ---------- F4: Flatten completeness ----------
def f4(s):
    fields = [f["name"] for f in (s["fields"] or [])]; fl = s["methods"].get("Flatten", "")
    if not fields or not fl: return None
    a = ask({"struct_fields": fields, "function": fl}, {f"w_{f}": {"type": "noul", "instructions": f"Does `function` write the struct field `{f}` into the returned map?"} for f in fields})
    return {"type": s["type"], "missing": [f for f in fields if a[f"w_{f}"]["noul"] < 0.3], "static_missing": [f for f in fields if f'"{f}"' not in fl]}
results["F4_flatten_completeness"] = [r for r in pmap(f4, slices) if r]

results["usage"] = USAGE
json.dump(results, open(sys.argv[3], "w"), indent=1); print(json.dumps(USAGE))
