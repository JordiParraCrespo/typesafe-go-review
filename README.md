# typesafe-go-review

Research and (soon) implementation of a Go code review CLI that scores every
function against the conventions of [xrpl-go](https://github.com/Peersyst/xrpl-go)
using [TypeSafe AI](https://typesafe.ai/)'s System One model (Jev), then rolls
the scores up into a repository health score.

- `docs/research.md`: the research report (TypeSafe API, feasibility
  experiment, reference tools, xrpl-go rule catalogue, proposed architecture and
  scoring model).
- `experiments/`: the exact TypeSafe requests and responses used in the
  feasibility experiment. No API key is stored in this repository; export
  `TYPESAFE_API_KEY` to reproduce:

```bash
curl -sS -X POST https://api.typesafe.ai/v1/systemone \
  -H "Authorization: Bearer $TYPESAFE_API_KEY" \
  -H "Content-Type: application/json" \
  -d @experiments/batch-request.json | jq .answers
```
