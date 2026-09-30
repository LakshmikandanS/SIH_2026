"""Step 4 demo -- the authorization spine, shown rather than asserted.

    .venv/Scripts/python -m tests.demos.step4_security

Walks one ALLOW and all four DENY paths through the *whole* Tool Gateway
(§6.6 Step A -> Step B -> Step C) against the fake `echo` tool, then prints
the event trace and verifies the hash chain across all of it.

This is §8's step 4 and C-004 made visible: ALLOW and DENY are proven with a
backend that has no behaviour of its own, *before* any real tool exists. If
the echo path authorizes correctly, the spine steps 5-8 inherit is correct.

Runs against a throwaway database; it never touches var/citadel.db.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_DEMO_DB = Path(tempfile.gettempdir()) / "citadel_step4_demo.db"
_DEMO_DB.unlink(missing_ok=True)
os.environ["CITADEL_DATABASE_URL"] = f"sqlite:///{_DEMO_DB.as_posix()}"

from fastapi.testclient import TestClient  # noqa: E402

from app.capability import CapabilityScope, issue_capability, issue_for_step  # noqa: E402
from app.db import init_db  # noqa: E402
from app.db.engine import SessionLocal  # noqa: E402
from app.db.models import Agent, Task, User  # noqa: E402
from app.db.state_machines import Classification, Role  # noqa: E402
from app.identity import create_user  # noqa: E402
from app.main import create_app  # noqa: E402
from app.observability import format_trace, get_trace, verify_chain  # noqa: E402
from app.policy import PolicyResource, Tool, get_registry  # noqa: E402
from app.tool_gateway import clear_backends, invoke, register_backend  # noqa: E402
from app.tool_gateway.backends.echo import echo  # noqa: E402

# The demo's two targets, straight out of §1.1 step 9 and §1.2.
MAINTENANCE_DOC = PolicyResource.build(
    "DOC-P101-HIST", "document", Classification.CONFIDENTIAL, ["maintenance", "engineering"]
)
FINANCE_DOC = PolicyResource.build(
    "DOC-Q3-FIN", "document", Classification.CONFIDENTIAL, ["finance"]
)


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def show(label: str, envelope: dict) -> None:
    """Print one §6.6 envelope the way the CLI eventually will."""
    if envelope["success"]:
        verdict = "ALLOW  -> executed"
    else:
        verdict = f"DENY   -> {envelope['error']['code']}"
    print(f"  {label:<44} {verdict}")
    if not envelope["success"]:
        print(f"  {'':<44} {envelope['error']['message']}")


def seed():
    """One engineer, one CONFIDENTIAL maintenance task, one agent -- plus the
    INTERNAL task used to exercise the classification rule, since the lattice
    tops out at CONFIDENTIAL and a CONFIDENTIAL task can never be out-ranked."""
    init_db.reset()
    session = SessionLocal()

    engineer = User(
        user_id="U123",
        username="j.rao",
        roles=[Role.ENGINEER],
        clearance=Classification.CONFIDENTIAL,
        department="maintenance",
    )
    session.add(engineer)
    session.flush()

    task = Task(
        task_id="T123",
        user_id=engineer.user_id,
        classification=Classification.CONFIDENTIAL,
        requirements={"needs_rag": True},
    )
    session.add(task)
    session.flush()
    session.add(Agent(agent_id="A123", task_id=task.task_id, agent_type="researcher"))

    internal = Task(
        task_id="T-INTERNAL",
        user_id=engineer.user_id,
        classification=Classification.INTERNAL,
        requirements={"needs_rag": True},
    )
    session.add(internal)
    session.flush()
    session.add(Agent(agent_id="A-INTERNAL", task_id=internal.task_id, agent_type="researcher"))

    create_user(
        session,
        username="s.mehta",
        password="admin-pw",
        roles=[Role.ADMIN],
        clearance=Classification.CONFIDENTIAL,
        department="security",
        user_id="U-ADMIN",
    )
    session.commit()
    return session


def main() -> None:
    session = seed()
    get_registry().clear()
    clear_backends()

    # The fake tool. §6.7 only ALLOWs the three real names, so the ALLOW path
    # is exercised under rag.search -- step 6 swaps in the real backend with
    # another register_backend call and changes nothing else.
    register_backend(Tool.RAG_SEARCH, echo)

    rule("1. The ALLOW path (§1.1 steps 7-11)")
    print("  Capability minted for this step only, then the concrete resource is named.\n")
    cap = issue_for_step("T123", "A123", Tool.RAG_SEARCH)
    print(f"  capability_id : {cap.capability.capability_id}")
    print(f"  operation     : {cap.capability.operation}")
    print(f"  expires_at    : {cap.capability.expires_at.isoformat()}  (5-minute TTL)")
    print()
    show(
        "rag.search on maintenance docs",
        invoke(
            capability_token=cap.token,
            tool=Tool.RAG_SEARCH,
            resource=MAINTENANCE_DOC,
            arguments={"query": "Pump P-101 maintenance history"},
        ),
    )

    rule("2. DENY -- unauthorized ACL / department (§1.2, the denial path)")
    print("  Same valid capability. Only the target changes: acl=['finance'],")
    print("  disjoint from the task's department='maintenance'.\n")
    cap2 = issue_for_step("T123", "A123", Tool.RAG_SEARCH)
    show(
        "rag.search on the Q3 finance report",
        invoke(
            capability_token=cap2.token,
            tool=Tool.RAG_SEARCH,
            resource=FINANCE_DOC,
            arguments={"query": "Q3 finance report"},
        ),
    )
    print("\n  The capability was valid. Policy said no. The tool never ran.")

    rule("3. DENY -- resource classified above the task")
    print("  An INTERNAL task reaching for a CONFIDENTIAL document.")
    print("  The lattice is PUBLIC < INTERNAL < CONFIDENTIAL, compared by rank.\n")
    cap3 = issue_for_step("T-INTERNAL", "A-INTERNAL", Tool.RAG_SEARCH)
    show(
        "INTERNAL task -> CONFIDENTIAL doc",
        invoke(
            capability_token=cap3.token,
            tool=Tool.RAG_SEARCH,
            resource=MAINTENANCE_DOC,
            arguments={"query": "Pump P-101 maintenance history"},
        ),
    )

    rule("4. DENY -- expired capability (§6.5)")
    print("  Minted with a negative TTL, so it is already past expiry when presented.\n")
    expired = issue_capability(
        task_id="T123",
        agent_id="A123",
        operation=Tool.RAG_SEARCH,
        scope=CapabilityScope(
            classification_max=Classification.CONFIDENTIAL, department="maintenance"
        ),
        ttl_seconds=-1,
    )
    show(
        "rag.search with a stale token",
        invoke(
            capability_token=expired.token,
            tool=Tool.RAG_SEARCH,
            resource=MAINTENANCE_DOC,
            arguments={"query": "Pump P-101 maintenance history"},
        ),
    )
    print("\n  There is no revocation list in this slice. The TTL is the only expiry,")
    print("  which is exactly why the emergency control below is enforced separately.")

    rule("5. DENY -- tool disabled by an administrator (§1.3, §6.8)")
    with TestClient(create_app()) as client:
        token = client.post(
            "/login", json={"username": "s.mehta", "password": "admin-pw"}
        ).json()["access_token"]
        response = client.post(
            f"/admin/tools/{Tool.RAG_SEARCH}/disable",
            json={"disabled": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        print(f"  admin disable-tool {Tool.RAG_SEARCH} -> HTTP {response.status_code}")

    print("\n  Now a capability minted BEFORE the disable, still well inside its TTL:\n")
    show(
        "rag.search with a live, valid token",
        invoke(
            capability_token=cap.token,
            tool=Tool.RAG_SEARCH,
            resource=MAINTENANCE_DOC,
            arguments={"query": "Pump P-101 maintenance history"},
        ),
    )
    print("\n  Central revocation beat an unexpired capability. That is the whole point")
    print("  of keeping capability and policy as two independent checks.")

    rule("6. The trace (§6.12)")
    print(format_trace(get_trace(None)))

    rule("7. The hash chain")
    verify_chain()
    events = get_trace(None)
    denials = [e for e in events if e.event_type == "TOOL_DENIED"]
    executed = [e for e in events if e.event_type == "TOOL_EXECUTED"]
    print(f"  {len(events)} events, chain verified unbroken.")
    print(f"  {len(executed)} TOOL_EXECUTED, {len(denials)} TOOL_DENIED.")
    print("\n  A denial is a first-class, recorded outcome -- not a crash, not a gap.")

    session.close()
    get_registry().clear()
    clear_backends()


if __name__ == "__main__":
    main()
