"""The six foundation tables of design doc section 3.

Field names are copied from section 3 verbatim. Column *types* are this
module's choice (the contract calls them illustrative); state values are
stored as plain strings and validated by `app.db.state_machines`, which is the
single enforcement point -- so a Phase-2 state addition is a data edit there,
not a schema migration here.

Capability (section 6.5) and Evidence (section 6.9) are domain objects too,
but they are owned by `security-control-plane` and `data-plane-rag`
respectively and are deliberately not given storage here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow
from app.db.state_machines import (
    AgentStatus,
    ApprovalState,
    ArtifactStatus,
    TaskStatus,
)


class User(Base):
    """Owned by Control Plane (Identity). This table is created here; only
    Identity (step 4) writes to it."""

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    roles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    clearance: Mapped[str] = mapped_column(String(32), nullable=False)
    department: Mapped[str] = mapped_column(String(64), nullable=False)

    # Section 6.4: "a single local table of users (bcrypt-hashed passwords)".
    # Not part of the section 3 domain object (a User returned over the wire
    # never carries it); nullable so Identity owns populating it in step 4.
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    tasks: Mapped[list["Task"]] = relationship(back_populates="user")


class Task(Base):
    """Created by the Query Router; all state after that is owned by the
    Orchestrator (section 6.2, resolves C-005)."""

    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.user_id"), nullable=False
    )
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TaskStatus.CREATED
    )
    requirements: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    # Section 6.11 optimistic versioning. Distinct from Artifact.version, which
    # is a document revision number -- these are never the same counter.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    user: Mapped["User"] = relationship(back_populates="tasks")
    agent: Mapped[Optional["Agent"]] = relationship(
        back_populates="task", uselist=False
    )
    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="task")


class Agent(Base):
    """Exactly one row per task (section 3, BB-014). The unique constraint on
    task_id is what makes "no multi-agent delegation" a schema fact rather than
    a convention."""

    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("task_id", name="uq_agents_one_per_task"),)

    agent_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tasks.task_id"), nullable=False
    )
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AgentStatus.RUNNING
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    task: Mapped["Task"] = relationship(back_populates="agent")


class Artifact(Base):
    """Owned by the Artifact service (step 8)."""

    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tasks.task_id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ArtifactStatus.TEMP
    )
    hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provenance: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # Where the rendered file lives; the Verifier's first check is
    # file_exists_and_readable() (section 6.10), so the path must be recorded.
    path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    task: Mapped["Task"] = relationship(back_populates="artifacts")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="artifact")


class Approval(Base):
    """Owned by Control Plane; state changes owned solely by the single
    transactional decision endpoint (section 6.10).

    Two columns, not one: `state` is the section 4 state machine
    (NOT_REQUIRED -> REVIEW_REQUIRED -> APPROVED/REJECTED); `decision` is the
    section 3 domain field, null until a human decides. They agree in the
    terminal states by construction, and the decision endpoint sets both
    together in the same transaction.
    """

    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("artifacts.artifact_id"), nullable=False
    )
    # Nullable, and never defaulted: written once, from the verified session
    # JWT, at decision time (section 6.4 -- never from a request body).
    approver_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("users.user_id"), nullable=True
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ApprovalState.NOT_REQUIRED
    )
    decision: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    artifact: Mapped["Artifact"] = relationship(back_populates="approvals")


class Event(Base):
    """Append-only audit chain. The ONLY writer is `app.observability.writer`
    -- no other module inserts here (section 6.12).

    `seq` is the chain's true ordering key and the primary key; `event_id` is
    the section 3 display identifier derived from it (EVT0001). Ordering by an
    integer rather than a zero-padded string is what keeps /trace correct past
    the padding width.
    """

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_task_seq", "task_id", "seq"),)

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)

    # Nullable: system-scoped events (an admin DISABLE TOOL, section 6.8) have
    # no task. Task-scoped events -- all of the section 6.12 list -- set it.
    task_id: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, index=True
    )
    actor_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)


__all__ = ["User", "Task", "Agent", "Artifact", "Approval", "Event"]
