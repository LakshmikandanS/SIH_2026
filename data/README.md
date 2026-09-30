# Demo corpus

Fixture documents for the §1 vertical-slice demo and the §9 RAG testing checklist.
Every document except `pump_p102_notes.txt` ships with a `<filename>.meta.json`
sidecar, which §6.9 makes **mandatory** — ingestion rejects a document without one,
with no default classification and no inferred ACL.

| Document | Classification | ACL | Role in the demo |
|---|---|---|---|
| `maintenance/pump_p101_history.txt` | CONFIDENTIAL | maintenance, engineering | The happy path's retrieval target (§1.1 step 7) |
| `maintenance/pump_p101_spec_sheet.txt` | INTERNAL | maintenance, engineering | Second legitimate hit, so ranking is meaningful |
| `finance/q3_finance_report.txt` | CONFIDENTIAL | finance | The §1.2 denial target — ACL disjoint from `department: maintenance` |
| `maintenance/pump_p102_notes.txt` | *(none — no sidecar)* | *(none)* | Ingestion must reject it outright (§9 RAG) |

## Notes for whoever wires ingestion

**Plain `.txt`, not `.pdf`.** §6.9's example path is a `.pdf`, but the sidecar pattern
is what that example is specifying, not the container format. Text files keep a PDF
parser out of the dependency list for a slice that gains nothing from one. If PDFs are
wanted later, the sidecar convention is unchanged.

**`pump_p101_history.txt` carries real, parseable dates on purpose.** §1.1 step 12 has
`python.execute` parse maintenance dates out of the retrieved text and compute
days-since-last-service. The most recent service is **2026-06-14**; that is the date
the computation should find. Do not reword the service record's date lines without
checking that step still works.

**Classification denial needs an INTERNAL task, not a new fixture.** The lattice in
`app/db/state_machines.py` is `PUBLIC < INTERNAL < CONFIDENTIAL`, so a CONFIDENTIAL
task can never trip `resource.classification > task.classification`. To exercise that
rule, run a task at INTERNAL against `pump_p101_history.txt` (CONFIDENTIAL). There is
no classification above CONFIDENTIAL to add, and inventing one would raise `ValueError`
from `Classification.rank`.
