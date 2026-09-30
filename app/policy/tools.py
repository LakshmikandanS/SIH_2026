"""The canonical tool names, defined once.

Every tool named anywhere in the design doc appears here, and this is the only
module in `app/` that spells one out as a literal string. Two reasons, both
load-bearing:

  * §6.7's rule list matches on these names, so a typo in one of them is a
    silent authorization change. A constant makes it an ImportError instead.
  * `tests/test_security.py::test_no_code_path_invokes_a_tool_outside_the_gateway`
    greps `app/` for these names and allows them only here, in
    `app/policy/engine.py`, and under `app/tool_gateway/`. That is how "no code
    path exists for a tool to be invoked without going through the Tool
    Gateway" stays true as steps 5-8 land: a later step imports `Tool.X` and
    registers a backend, and never writes the string itself.

They live in `app.policy` rather than `app.tool_gateway` because the Policy
Engine must not depend on the gateway -- the gateway calls policy, never the
reverse.
"""

from __future__ import annotations


class Tool:
    #: §6.7 -- explicitly ALLOWed tools (subject to the checks above them).
    RAG_SEARCH = "rag.search"
    PYTHON_EXECUTE = "python.execute"
    GENERATE_REPORT = "generate_report"

    #: §6.7 -- unconditionally denied, checked before anything else can match.
    HOST_SHELL = "host.shell"

    #: §6.7 -- routed to a human instead of executed. In this slice the actual
    #: release happens through §6.10's approval endpoint, not as a tool call.
    ARTIFACT_RELEASE = "artifact.release"

    #: The fake tool of the §8 step-4 row ("tested with a fake tool that just
    #: echoes"). Deliberately NOT in the allow set below: invoking it under its
    #: own name proves the default-DENY tail of the rule chain, while binding
    #: the echo backend to one of the three real names proves the ALLOW path.
    ECHO = "echo"


#: The literal `{"rag.search", "python.execute", "generate_report"}` of §6.7's
#: second-to-last rule. Membership is what ALLOWs; nothing else does.
ALLOWED_TOOLS: frozenset[str] = frozenset(
    {Tool.RAG_SEARCH, Tool.PYTHON_EXECUTE, Tool.GENERATE_REPORT}
)
