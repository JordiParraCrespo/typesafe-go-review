"""Round four: new uses of TypeSafe, each with its own answer key.

H1 spec traceability   XLS "Data Verification" condition -> enforced in Validate? -> tested?
H2 test-label check    does a "fail - X" case's expected error match X? (mutation-scored)
H3 doc-comment check   does a doc comment describe its function? (mutation-scored)
H4 change triage       classify a commit's diff as feat/fix/refactor/docs/test/chore (vs its own prefix)
H5 changelog check     does a CHANGELOG entry describe the diff it ships with? (swap-scored)

Usage: TYPESAFE_API_KEY=... python3 run_ideas4.py <xrpl-go dir> slices.json xls_dataverif.json <from-sha> out.json
"""
import json, os, re, sys, glob, random, subprocess, time, urllib.request, concurrent.futures as cf
API = "https://api.typesafe.ai/v1/systemone"; KEY = os.environ["TYPESAFE_API_KEY"]; USAGE = {"requests": 0, "input_tokens": 0}
def ask(state, questions, retries=4):
    body = json.dumps({"state": state, "model": "jev-latest", "questions": questions}).encode()
    for i in range(retries):
        req = urllib.request.Request(API, body, {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.load(r); USAGE["requests"] += 1; USAGE["input_tokens"] += out["usage"]["input_tokens"]; return out["answers"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and i < retries - 1: time.sleep(2 ** i); continue
            raise RuntimeError(f"{e.code}: {e.read()[:300]}")
def pmap(fn, items, w=8):
    with cf.ThreadPoolExecutor(w) as ex: return list(ex.map(fn, items))
def chunks(xs, n): return [xs[i:i+n] for i in range(0, len(xs), n)]
random.seed(7)
REPO, SL, XLS, FROM, OUT = sys.argv[1:6]
S = {s["type"]: s for s in json.load(open(SL))["slices"]}
TX = f"{REPO}/xrpl/transaction"
R = {}

# all test functions in the package, so shared files like account_identity_test.go are visible
TESTFUNCS = []
for f in glob.glob(f"{TX}/*_test.go"):
    src = open(f).read()
    for m in re.finditer(r"^func (Test\w+)\(t \*testing\.T\) \{\n(.*?)^\}\n", src, re.S | re.M):
        TESTFUNCS.append((os.path.basename(f), m.group(1), m.group(0)))
def tests_for(t):
    # own tests by name, plus any test that builds the type as a literal
    lit = re.compile(r"[&\s(\[{,]" + re.escape(t) + r"\{")
    picked = [body for _, name, body in TESTFUNCS if name.startswith("Test" + t) or lit.search(body)]
    return "\n\n".join(picked)

# ---------- H1: spec traceability ----------
X = json.load(open(XLS))
def h1(t):
    conds = X[t]["conds"]; s = S[t]; v = s["methods"].get("Validate", ""); tests = tests_for(t)
    crit = {"true": "The Go code rejects inputs meeting this condition, directly or through a helper", "false": "The Go code never checks this condition"}
    enf = ask({"go_validate": v}, {f"e{i}": {"type": "noul", "instructions": {"question": "Does `go_validate` reject a transaction that meets this spec failure condition?", "spec_condition": c}, "criteria": crit} for i, c in enumerate(conds)})
    tq = {f"t{i}": {"type": "noul", "instructions": {"question": "Does `tests` contain a case that builds a transaction meeting this spec failure condition and expects validation to fail?", "spec_condition": c}} for i, c in enumerate(conds)}
    tst = ask({"tests": tests[:120000]}, tq) if tests else {k: {"noul": 0.0} for k in tq}
    return {"type": t, "spec": X[t]["spec"], "rows": [{"cond": c, "enforced": round(enf[f"e{i}"]["noul"], 2), "tested": round(tst[f"t{i}"]["noul"], 2)} for i, c in enumerate(conds)]}
R["H1_spec_traceability"] = pmap(h1, [t for t in X if t in S and S[t]["methods"].get("Validate")])
R["H1_missing_go_types"] = sorted(t for t in X if t not in S)

# ---------- H2: test-case label consistency, mutation scored ----------
cases = []
for t, s in S.items():
    body = s["tests"].get(f"Test{t}_Validate", "")
    parts = re.split(r'\n\s*\{\s*\n\s*name:\s*', body)
    for p in parts[1:]:
        m = re.match(r'"(fail[^"]*)"', p)
        if not m: continue
        chunk = "name: " + p[:1500].split("\n\t\t},")[0]
        errs = [e for e in re.findall(r"\bErr[A-Z]\w*", chunk)]
        if len(set(errs)) != 1: continue
        others = [e for e in (s["sentinels_returned"] or []) if e != errs[0]]
        cases.append({"type": t, "name": m.group(1), "chunk": chunk, "err": errs[0], "alt": random.choice(others) if others else None})
random.shuffle(cases); cases = [c for c in cases if c["alt"]][:150]
items = [(f"o{i}", c["chunk"], False, c) for i, c in enumerate(cases)] + [(f"m{i}", re.sub(r"\b" + c["err"] + r"\b", c["alt"], c["chunk"]), True, c) for i, c in enumerate(cases)]
random.shuffle(items)
def h2(batch):
    a = ask({"cases": {k: ch for k, ch, _, _ in batch}}, {k: {"type": "noul", "instructions": f"In `cases.{k}`, does the expected error match the failure described by the case name?", "criteria": {"true": "The error names the same problem the case name describes", "false": "The error describes a different problem than the case name"}} for k, _, _, _ in batch})
    return [{"key": k, "mutant": mut, "match": round(a[k]["noul"], 2), "type": c["type"], "name": c["name"], "err": c["alt"] if mut else c["err"]} for k, _, mut, c in batch]
R["H2_test_labels"] = [r for b in pmap(h2, chunks(items, 10)) for r in b]

# ---------- H3: doc comments, mutation scored ----------
docs = []
for f in glob.glob(f"{TX}/*.go"):
    if f.endswith("_test.go"): continue
    src = open(f).read()
    for m in re.finditer(r"((?:^//[^\n]*\n)+)(^func (?:\([^)]*\) )?([A-Z]\w*)\([^\n]*\{\n.*?^\}\n)", src, re.S | re.M):
        doc = " ".join(l[2:].strip() for l in m.group(1).strip().splitlines()); name = m.group(3)
        if len(doc) > 40 and name not in ("TxType", "Flatten", "Validate") and not name.startswith("Set") and len(m.group(2)) < 3000:
            docs.append({"file": os.path.basename(f), "name": name, "doc": doc, "code": m.group(2)})
random.shuffle(docs); docs = docs[:120]
items = []
for i, d in enumerate(docs):
    other = docs[(i + 7) % len(docs)]
    fake = re.sub(r"^\w+", d["name"], other["doc"])       # keep the name right, so only the meaning is wrong
    items.append((f"o{i}", d["doc"], d["code"], False, d)); items.append((f"m{i}", fake, d["code"], True, d))
random.shuffle(items)
def h3(batch):
    a = ask({"items": {k: {"doc_comment": doc, "code": code} for k, doc, code, _, _ in batch}}, {k: {"type": "noul", "instructions": f"Does `items.{k}.doc_comment` accurately describe what `items.{k}.code` does?"} for k, _, _, _, _ in batch})
    return [{"key": k, "mutant": mut, "accurate": round(a[k]["noul"], 2), "file": d["file"], "name": d["name"], "doc": doc} for k, doc, _, mut, d in batch]
R["H3_doc_comments"] = [r for b in pmap(h3, chunks(items, 8)) for r in b]

# ---------- H4: change triage vs conventional-commit prefix ----------
TYPES = {"feat": "Adds new functionality or a new API", "fix": "Corrects wrong behaviour", "refactor": "Restructures code without changing behaviour", "docs": "Changes documentation only", "test": "Adds or changes tests only", "chore": "Maintenance: dependencies, release bookkeeping, synced reference files, tooling", "ci": "Changes CI configuration"}
log = subprocess.run(["git", "-C", REPO, "log", "--no-merges", "--format=%H %s", f"{FROM}..HEAD"], capture_output=True, text=True).stdout.splitlines()
commits = []
for line in log:
    sha, subj = line.split(" ", 1); m = re.match(r"(\w+)(\([^)]*\))?!?:", subj)
    if m and m.group(1) in TYPES:
        stat = subprocess.run(["git", "-C", REPO, "show", "--stat", "--format=", sha], capture_output=True, text=True).stdout[-3000:]
        patch = subprocess.run(["git", "-C", REPO, "show", "--format=", sha, "--", ".", ":(exclude)CHANGELOG.md"], capture_output=True, text=True).stdout[:6000]
        commits.append({"sha": sha[:8], "label": m.group(1), "subject": subj, "stat": stat, "patch": patch})
def h4(c):
    a = ask({"files_changed": c["stat"], "diff_excerpt": c["patch"]}, {"kind": {"type": "choice", "instructions": "What kind of change is this commit? Judge from the diff, not from any message.", "criteria": TYPES}})["kind"]
    return {"sha": c["sha"], "label": c["label"], "subject": c["subject"], "pred": a["choice"], "conf": round(a["confidence"], 2)}
R["H4_triage"] = pmap(h4, commits)

# ---------- H5: changelog entry vs its diff, swap scored ----------
pairs = []
for line in log:
    sha = line.split(" ", 1)[0]
    cl = subprocess.run(["git", "-C", REPO, "show", "--format=", sha, "--", "CHANGELOG.md"], capture_output=True, text=True).stdout
    added = [l[1:].strip() for l in cl.splitlines() if l.startswith("+") and not l.startswith("+++") and l[1:].strip().startswith("-")]
    if not added: continue
    patch = subprocess.run(["git", "-C", REPO, "show", "--format=", sha, "--", ".", ":(exclude)CHANGELOG.md"], capture_output=True, text=True).stdout[:6000]
    if patch.strip(): pairs.append({"sha": sha[:8], "entry": " ".join(added)[:1200], "patch": patch})
items = []
for i, p in enumerate(pairs):
    items.append((p, p["entry"], False))
    if len(pairs) > 1: items.append((p, pairs[(i + 1) % len(pairs)]["entry"], True))
def h5(it):
    p, entry, swapped = it
    a = ask({"changelog_entry": entry, "diff_excerpt": p["patch"]}, {"q": {"type": "noul", "instructions": "Does `changelog_entry` describe the change made in `diff_excerpt`?"}})
    return {"sha": p["sha"], "swapped": swapped, "describes": round(a["q"]["noul"], 2)}
R["H5_changelog"] = pmap(h5, items)

R["usage"] = USAGE
json.dump(R, open(OUT, "w"), indent=1); print(json.dumps(USAGE))
