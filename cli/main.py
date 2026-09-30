"""The `citadel` CLI's command surface (design doc §1, §6.1) -- Typer wiring.

Every command below does exactly one of two things: call `POST /login` and
cache the result, or load the cached session and call one of the other five
endpoints through `cli.client.CitadelClient`. There is no business logic here
-- state machines, policy, verification, all of it lives in `app/` and this
package never re-implements any of it.

Run as `python -m cli <command>` (see `cli/__init__.py`'s own docstring for
the full command list). `_make_client` is the one seam the test suite
overrides (`tests/test_cli.py`) to drive these same commands against an
in-process `TestClient(create_app())` instead of a real socket.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

import httpx
import typer

from cli import config
from cli import session as session_mod
from cli.client import CitadelAPIError, CitadelClient
from cli.session import NotLoggedIn, Session

app = typer.Typer(
    help="Citadel -- drive the vertical slice end to end (design doc section 1).",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=False,
)
admin_app = typer.Typer(help="Admin-only emergency controls (section 6.8).", no_args_is_help=True)
app.add_typer(admin_app, name="admin")


# --------------------------------------------------------------------------
# The one seam tests override, and the one place a real socket is opened.
# --------------------------------------------------------------------------
def _make_client(base_url: str) -> CitadelClient:
    return CitadelClient(base_url, timeout=config.REQUEST_TIMEOUT_SECONDS)


@contextmanager
def _client(base_url: str) -> Iterator[CitadelClient]:
    """Open a client, run the caller's calls, and turn any failure into a
    clean, one-line message and a non-zero exit -- never a raw traceback.
    This is what makes `admin disable-tool` as a non-admin, or any other
    rejected call, read as "a clear rejection", per this step's own brief."""
    client = _make_client(base_url)
    try:
        yield client
    except CitadelAPIError as exc:
        typer.secho(f"Error: {exc.code}: {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    except httpx.HTTPError as exc:
        typer.secho(
            f"Error: could not reach {base_url} ({exc}) -- is `citadel serve` (or "
            f"`python -m app.main`) running?",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None
    finally:
        client.close()


def _require_session() -> Session:
    try:
        return session_mod.load()
    except NotLoggedIn as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None


# --------------------------------------------------------------------------
# section 6.4 Identity
# --------------------------------------------------------------------------
@app.command()
def login(
    username: Optional[str] = typer.Option(None, "--username", "-u"),
    password: Optional[str] = typer.Option(None, "--password", "-p"),
    base_url: str = typer.Option(
        config.BASE_URL, "--base-url", help="Where the trusted-zone server is running."
    ),
) -> None:
    """section 1.1 step 1: prompt for username/password once, POST /login,
    and cache the returned session JWT for every later `citadel` invocation
    in this shell (section 6.1: "No other credential form exists")."""
    if username is None:
        username = typer.prompt("Username")
    if password is None:
        password = typer.prompt("Password", hide_input=True)

    with _client(base_url) as client:
        result = client.login(username, password)

    session_mod.save(
        Session(
            base_url=base_url,
            access_token=result.access_token,
            token_type=result.token_type,
            user_id=result.user_id,
            roles=result.roles,
            expires_at=result.expires_at,
        )
    )
    typer.echo(f"Logged in as {username} ({result.user_id}), roles={result.roles}")
    typer.echo(f"Session cached at {config.SESSION_PATH} -- expires {result.expires_at}")


# --------------------------------------------------------------------------
# section 6.2 Orchestrator
# --------------------------------------------------------------------------
@app.command()
def task(
    text: str = typer.Argument(..., help='The task text, e.g. the section 1.1 Pump P-101 request.'),
    classification: str = typer.Option(
        ..., "--classification", help="PUBLIC | INTERNAL | CONFIDENTIAL -- user-declared, authoritative."
    ),
) -> None:
    """`/task "<text>" --classification LEVEL` (section 1.1 step 2).

    `POST /task` is synchronous for this slice (section 6.2) -- it plans,
    runs the whole agent loop, and only then returns, so this can block for
    10-30s+ against a real model and a real sandbox container. There is no
    faster point at which a real `task_id` exists to print."""
    session = _require_session()
    typer.echo("Submitting task -- this runs the whole plan/agent loop synchronously " "and can take a while...")
    with _client(session.base_url) as client:
        body = client.submit_task(session.access_token, text, classification)

    typer.echo(f"task_id       : {body['task_id']}")
    typer.echo(f"status        : {body['status']}")
    typer.echo(f"agent_status  : {body['agent_status']}")
    typer.echo(f"artifact_id   : {body['artifact_id']}")
    if body.get("reason"):
        typer.echo(f"reason        : {body['reason']}")


@app.command()
def status(task_id: str) -> None:
    """`/status <task_id>` (section 6.1) -- GET /tasks/{id}, the
    Orchestrator's own view (never Control Plane or Data Plane, section 6.2 /
    C-005)."""
    session = _require_session()
    with _client(session.base_url) as client:
        body = client.get_status(session.access_token, task_id)

    typer.echo(f"task_id        : {body['task_id']}")
    typer.echo(f"user_id        : {body['user_id']}")
    typer.echo(f"classification : {body['classification']}")
    typer.echo(f"status         : {body['status']}")
    typer.echo(f"requirements   : {body['requirements']}")
    typer.echo(f"version        : {body['version']}")
    typer.echo(f"created_at     : {body['created_at']}")


@app.command()
def trace(task_id: str) -> None:
    """`/trace <task_id>` (section 1.1's closing line) -- the full ordered
    event log, printed exactly as `GET /tasks/{id}/trace`'s own `text` field
    renders it (`app.observability.format_trace`). A `TOOL_DENIED` or an
    emergency-control event reads as one more line in the same list, not as
    an error dump -- that uniformity is the point of section 1.2's demo."""
    session = _require_session()
    with _client(session.base_url) as client:
        body = client.get_trace(session.access_token, task_id)

    typer.echo(f"Trace for {task_id}")
    typer.echo(
        "(a TOOL_DENIED, a filtered EVIDENCE_RETRIEVED, or an emergency-control "
        "POLICY_DECISION are normal, audited outcomes below -- not errors.)"
    )
    typer.echo("")
    typer.echo(body["text"] or "(no events recorded for this task yet)")


# --------------------------------------------------------------------------
# section 6.10 Approval -- decision endpoint
# --------------------------------------------------------------------------
def _decide(id_or_task_id: str, decision: str, comment: Optional[str]) -> None:
    session = _require_session()
    with _client(session.base_url) as client:
        approval_id = client.resolve_approval_id(session.access_token, id_or_task_id)
        body = client.decide_approval(session.access_token, approval_id, decision, comment)

    typer.echo(f"approval_id     : {body['approval_id']}")
    typer.echo(f"decision        : {body['decision']}")
    typer.echo(f"task_id         : {body['task_id']}")
    typer.echo(f"task_status     : {body['task_status']}")
    if "artifact_status" in body:
        typer.echo(f"artifact_status : {body['artifact_status']}")
    if body.get("revision"):
        typer.echo(f"revision        : {body['revision']}")
    if body.get("reason"):
        typer.echo(f"reason          : {body['reason']}")


@app.command()
def approve(
    id_or_task_id: str = typer.Argument(..., help="An approval_id (APR...) or a task_id (T...)."),
    decision: str = typer.Option(
        "APPROVED", "--decision", help="APPROVED or REJECTED -- defaults to APPROVED."
    ),
    comment: Optional[str] = typer.Option(None, "--comment"),
) -> None:
    """`/approve <approval_id> --decision APPROVED|REJECTED [--comment "..."]`
    (section 6.1). `approver_id` is never sent -- it is derived server-side
    from the session (section 6.4), with no exception."""
    decision = decision.upper()
    if decision not in ("APPROVED", "REJECTED"):
        typer.secho(
            f"Error: --decision must be APPROVED or REJECTED, got {decision!r}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    _decide(id_or_task_id, decision, comment)


@app.command()
def reject(
    id_or_task_id: str = typer.Argument(..., help="An approval_id (APR...) or a task_id (T...)."),
    comment: Optional[str] = typer.Option(
        None, "--comment", help="Feedback appended to the one bounded revision (section 5.3)."
    ),
) -> None:
    """Reject an artifact -- `approve --decision REJECTED`'s own dedicated
    command, since section 5.3's revision path needs a way to trigger it
    from the CLI, not just from tests."""
    _decide(id_or_task_id, "REJECTED", comment)


# --------------------------------------------------------------------------
# section 6.8 Emergency control -- admin only
# --------------------------------------------------------------------------
@admin_app.command("disable-tool")
def disable_tool(tool_name: str) -> None:
    """`citadel admin disable-tool <tool_name>` (section 1.3) -- admin-role-
    only. A non-admin session gets one clean rejection line, not a stack
    trace: the 403 from `require_role(Role.ADMIN)` is caught by `_client`
    above like any other API error."""
    session = _require_session()
    with _client(session.base_url) as client:
        body = client.disable_tool(session.access_token, tool_name)

    typer.echo(f"tool                : {body['tool']}")
    typer.echo(f"disabled            : {body['disabled']}")
    typer.echo(f"previously_disabled : {body['previously_disabled']}")
    typer.echo(f"known_tool          : {body['known_tool']}")


# --------------------------------------------------------------------------
# The server itself -- see app/main.py's own docstring for why it lives there.
# --------------------------------------------------------------------------
@app.command()
def serve() -> None:
    """Start the real trusted-zone server (`app.main:create_app`, going
    through the same startup hook that registers the tool backends and
    ingests the demo RAG corpus) on `app.config.SERVER_HOST`/`SERVER_PORT`,
    seeding the section 1.1/1.2/1.3 demo personas first. Equivalent to
    `.venv/Scripts/python -m app.main`; offered here too so the whole demo
    is drivable through one tool."""
    from app.main import run_server

    run_server()


if __name__ == "__main__":  # pragma: no cover
    app()
