from __future__ import annotations

from handler.runtime.harness.base import RunEvent, RunEventKind
from handler.runtime.run import pending_approval_ids, transcript_message
from handler.runtime.types import Message, Role


def test_finished_tool_calls_carry_their_started_arguments():
    # A harness that reports arguments only when the call starts.
    events = [
        RunEvent(RunEventKind.TOOL_STARTED, {"id": "a", "tool": "t", "kind": "k",
                                             "input": {"q": 1}}),
        RunEvent(RunEventKind.TOOL_STARTED, {"id": "b", "tool": "t", "kind": "k", "input": {}}),
        RunEvent(RunEventKind.TOOL_FINISHED, {"id": "b", "tool": "t", "kind": "k",
                                              "output": "B", "status": "completed"}),
        RunEvent(RunEventKind.TOOL_FINISHED, {"id": "a", "tool": "t", "kind": "k",
                                              "output": "A", "status": "completed"}),
        RunEvent(RunEventKind.TOOL_FINISHED, {"id": "c", "tool": "t", "kind": "k",
                                              "input": "on finish", "output": "C"}),
    ]
    inputs: dict = {}
    messages = [m for ev in events if (m := transcript_message("t", "r", ev, inputs))]
    assert [m.role for m in messages] == [Role.TOOL] * 3
    assert [(m.blocks[0]["id"], m.blocks[0]["input"]) for m in messages] == [
        ("b", {}), ("a", {"q": 1}), ("c", "on finish")]
    assert [m.blocks[1]["output"] for m in messages] == ["B", "A", "C"]
    assert inputs == {}


def test_started_events_make_no_message_without_a_pairing_dict():
    ev = RunEvent(RunEventKind.TOOL_STARTED, {"id": "a", "tool": "t", "kind": "k", "input": 1})
    assert transcript_message("t", "r", ev) is None


def _tool(output) -> Message:
    return Message("t", Role.TOOL, [{"type": "tool_call", "id": "a"},
                                    {"type": "tool_result", "id": "a", "output": output}])


def test_pending_approval_ids_from_any_output_shape():
    aid = "apr_0123456789abcdef"
    pending = {"status": "pending_approval", "approval_id": aid}
    assert pending_approval_ids(_tool(pending)) == [aid]
    assert pending_approval_ids(_tool(f'{{"status": "pending_approval", "approval_id": "{aid}"}}')) \
        == [aid]
    assert pending_approval_ids(_tool([{"type": "text", "text": str(pending)}])) == [aid]
    # an id mentioned without a pending status, or in a non-tool message, is not a request
    assert pending_approval_ids(_tool({"approval_id": aid, "status": "executed"})) == []
    assert pending_approval_ids(Message("t", Role.ASSISTANT, [{"type": "text",
                                                               "text": f"pending_approval {aid}"}])) == []
