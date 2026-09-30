"""Step 3 demo -- the foundation, shown rather than asserted.

    .venv/Scripts/python -m tests.demos.step3_foundation

Walks one task through the three state machines, records every move through
the single Observability writer, prints the resulting trace, then verifies the
hash chain and shows what happens when someone edits history behind the
writer's back.

Runs against a throwaway database; it never touches var/citadel.db.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_DEMO_DB = Path(tempfile.gettempdir()) / "citadel_step3_demo.db"
_DEMO_DB.unlink(missing_ok=True)
os.environ["CITADEL_DATABASE_URL"] = f"sqlite:///{_DEMO_DB.as_posix()}"

from sqlalchemy import select  # noqa: E402

from app import config, ids  # noqa: E402
from app.db import init_db  # noqa: E402
from app.db.engine import SessionLocal  # noqa: E402
from app.db.models import Agent, Approval, Artifact, Event, Task, User  # noqa: E402
from app.db.state_machines import (  # noqa: E402
    AgentStatus,
    ApprovalState,
    ArtifactStatus,
    Classification,
    Role,
    TaskStatus,
)
from app.db.transitions import (  # noqa: E402
    transition_agent,
    transition_approval,
    transition_artifact,
    transition_task,
)
from app.observability import (  # noqa: E402
    ChainBroken,
    EventType,
    append_event,
    format_trace,
    get_trace,
    get_writer,
    verify_chain,
)


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def main() -> None:
    rule("1. Schema")
    init_db.reset()
    print("  tables:          ", ", ".join(init_db.table_names()))
    print("  database:        ", config.DATABASE_URL)
    print("  hash strategy:   ", get_writer().strategy_name)
    print("  genesis hash:    ", config.GENESIS_HASH[:16] + "...")

    rule("2. One task through the three state machines")
    session = SessionLocal()

    engineer = User(
        user_id=ids.new_id(ids.USER),
        username="j.rao",
        roles=[Role.ENGINEER],
        clearance=Classification.CONFIDENTIAL,
        department="maintenance",
    )
    session.add(engineer)
    session.flush()

    task = Task(
        task_id=ids.new_id(ids.TASK),
        user_id=engineer.user_id,
        classification=Classification.CONFIDENTIAL,
        requirements={"needs_rag": True, "needs_document_generation": True},
    )
    session.add(task)
    session.flush()
    append_event(
        task.task_id, engineer.user_id, EventType.TASK_CREATED,
        {"classification": task.classification}, session=session,
    )

    agent = Agent(
        agent_id=ids.new_id(ids.AGENT),
        task_id=task.task_id,
        agent_type="researcher",
    )
    session.add(agent)
    session.flush()

    transition_task(session, task, TaskStatus.PLANNING, expected_version=task.version)
    append_event(task.task_id, agent.agent_id, EventType.PLAN_CREATED,
                 {"steps": ["rag.search", "python.execute", "generate_report"]},
                 session=session)

    transition_task(session, task, TaskStatus.RUNNING, expected_version=task.version)
    append_event(task.task_id, agent.agent_id, EventType.AGENT_STARTED,
                 {"agent_type": agent.agent_type}, session=session)

    # A denial, recorded as a first-class outcome (section 1.2) -- step 4 makes
    # this a real Policy Engine decision; here it is only the event shape.
    append_event(task.task_id, agent.agent_id, EventType.TOOL_DENIED,
                 {"tool": "rag.search", "code": "POLICY_DENIED",
                  "reason": "resource.acl disjoint from task.department"},
                 session=session)

    artifact = Artifact(
        artifact_id=ids.new_id(ids.ARTIFACT),
        task_id=task.task_id,
        type="maintenance_summary_report",
        provenance=["E001", "E002"],
    )
    session.add(artifact)
    session.flush()
    append_event(task.task_id, agent.agent_id, EventType.ARTIFACT_CREATED,
                 {"artifact_id": artifact.artifact_id}, session=session)

    transition_artifact(session, artifact, ArtifactStatus.CANDIDATE)
    transition_artifact(session, artifact, ArtifactStatus.VERIFIED)
    artifact.hash = "sha256-placeholder-until-step-8"
    append_event(task.task_id, agent.agent_id, EventType.ARTIFACT_VERIFIED,
                 {"checks_passed": 5}, session=session)

    approval = Approval(approval_id=ids.new_id(ids.APPROVAL), artifact_id=artifact.artifact_id)
    session.add(approval)
    session.flush()
    transition_approval(session, approval, ApprovalState.REVIEW_REQUIRED)
    transition_task(session, task, TaskStatus.WAITING_FOR_APPROVAL,
                    expected_version=task.version)
    append_event(task.task_id, engineer.user_id, EventType.APPROVAL_REQUESTED,
                 {"approval_id": approval.approval_id}, session=session)

    session.commit()
    print(f"  Task     {task.task_id}: {task.status} (version {task.version})")
    print(f"  Artifact {artifact.artifact_id}: {artifact.status}")
    print(f"  Approval {approval.approval_id}: {approval.state} / decision={approval.decision}")

    rule("3. The one transactional release (section 6.10) -- all three, one commit")
    approver = User(
        user_id=ids.new_id(ids.USER), username="a.khan", roles=[Role.APPROVER],
        clearance=Classification.CONFIDENTIAL, department="maintenance",
    )
    session.add(approver)
    session.flush()

    transition_approval(session, approval, ApprovalState.APPROVED)
    approval.approver_id = approver.user_id  # from the session, never a body
    transition_artifact(session, artifact, ArtifactStatus.APPROVED)
    transition_artifact(session, artifact, ArtifactStatus.RELEASED)
    transition_task(session, task, TaskStatus.COMPLETED, expected_version=task.version)
    transition_agent(session, agent, AgentStatus.SUCCESS)
    append_event(task.task_id, approver.user_id, EventType.APPROVAL_GRANTED,
                 {"approver_id": approver.user_id}, session=session)
    append_event(task.task_id, approver.user_id, EventType.ARTIFACT_RELEASED,
                 {"artifact_id": artifact.artifact_id}, session=session)
    session.commit()

    print(f"  Task     -> {task.status}")
    print(f"  Artifact -> {artifact.status}")
    print(f"  Approval -> {approval.state} (decision={approval.decision}, "
          f"approver={approval.approver_id})")

    rule("4. RELEASED is terminal")
    try:
        transition_artifact(session, artifact, ArtifactStatus.CANDIDATE)
    except Exception as exc:
        session.rollback()
        print(f"  refused: {exc}")

    rule(f"5. /trace {task.task_id}")
    print(format_trace(get_trace(task.task_id)))

    rule("6. Hash chain")
    print(f"  verify_chain({task.task_id}) -> {verify_chain(task.task_id)}")

    print("\n  Now tampering with history directly in the table, bypassing the writer:")
    print("  relabelling the TOOL_DENIED event as TOOL_EXECUTED ...")
    tamper = SessionLocal()
    row = tamper.execute(
        select(Event).where(Event.event_type == EventType.TOOL_DENIED)
    ).scalar_one()
    print(f"    {row.event_id}  {row.event_type} -> TOOL_EXECUTED")
    row.event_type = EventType.TOOL_EXECUTED
    tamper.commit()
    tamper.close()

    try:
        verify_chain(task.task_id)
        print("  !! chain still verified -- the audit trail would not be trustworthy")
    except ChainBroken as exc:
        print(f"  chain broken, as it must be: {exc}")

    session.close()
    print(f"\n  (demo database: {_DEMO_DB})\n")


if __name__ == "__main__":
    main()
