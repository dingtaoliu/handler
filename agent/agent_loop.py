"""
AgentLoop — synchronous per-request agent execution loop.

One instance is created per request. Call run() with a pre-built AgentState
and it will:
  1. Send messages to the LLM
  2. If the LLM responds with tool calls, execute them and append results
  3. Repeat until the LLM returns a plain-text response or max_iterations
     is reached

The loop mutates the AgentState in place and returns it when done.
Steps accumulate in state.steps for the caller to inspect or persist.
"""

import json
import logging
from datetime import datetime
from typing import Iterator, Optional

from .agent_state import AgentState, AgentStep
from .providers.model_provider import ModelProvider, ProviderResponse, ToolCall
from .tools.tool_registry import ToolRegistry

try:
    import openai
    _RETRYABLE_EXCEPTIONS = (openai.APITimeoutError, openai.APIConnectionError)
except ImportError:
    _RETRYABLE_EXCEPTIONS = ()

_MAX_LLM_ATTEMPTS = 2

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    Synchronous agent event loop.

    Args:
        provider:  ModelProvider instance (OpenAI, Dummy, etc.)
        tools:     ToolRegistry containing all available tools
        session:   AgentSession for the current request (passed through to
                   tool.execute() so tools can access auth context, workspace, etc.)
    """

    def __init__(
        self,
        provider: ModelProvider,
        tools: ToolRegistry,
        session,  # AgentSession — typed as Any to avoid circular import
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.session = session

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def run(self, state: AgentState) -> AgentState:
        """
        Execute the agent loop (blocking).

        Drives _loop_stream and discards SSE events — state is mutated in place
        by the shared inner loop. Returns state when complete.
        """
        state.started_at = datetime.utcnow()
        try:
            for _ in self._loop_stream(state):
                pass
        except Exception as exc:
            logger.exception("Unhandled error in agent loop")
            state.phase = "error"
            state.final_response = f"An unexpected error occurred: {exc}"
        state.completed_at = datetime.utcnow()
        return state

    def run_stream(self, state: AgentState) -> Iterator[dict]:
        """
        Execute the agent loop, yielding SSE event dicts as the loop progresses.

        Event types emitted:
          step_start        — before each LLM call
          text_delta        — partial LLM text chunk (streaming, zero or more per step)
          tool_calls        — when the LLM requests tool execution
          tool_result       — after each individual tool executes
          chunk             — the final text response (single event, full content)
          approval_required — when a write tool needs human confirmation
          error             — on LLM failure or max-iterations exceeded

        The caller encodes each dict as ``data: <json>\\n\\n`` and flushes.
        """
        state.started_at = datetime.utcnow()
        try:
            yield from self._loop_stream(state)
        except Exception as exc:
            logger.exception("Unhandled error in agent stream loop")
            state.phase = "error"
            state.final_response = f"An unexpected error occurred: {exc}"
            yield {"type": "error", "error": str(exc)}
        state.completed_at = datetime.utcnow()

    # ──────────────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────────────

    def _loop_stream(self, state: AgentState) -> Iterator[dict]:
        """
        Core agent loop — yields SSE event dicts and mutates state in place.

        Consumed directly by run_stream(); run() drives it and discards events.
        This is the single source of truth for all loop logic.
        """
        config = self.session.config
        tool_schemas = self.tools.get_openai_schemas() if config.enable_tools else []
        timeout: Optional[float] = (
            config.llm_timeout if config.llm_timeout > 0 else None
        )

        while state.iteration < state.max_iterations:
            state.iteration += 1
            step = AgentStep(
                iteration=state.iteration,
                phase="planning",
                started_at=datetime.utcnow(),
            )

            yield {"type": "step_start", "iteration": state.iteration}
            logger.debug("Agent iteration %d/%d", state.iteration, state.max_iterations)

            # ── LLM streaming call (with retry) ───────────────────────────
            response: Optional[ProviderResponse] = None
            last_exc: Optional[Exception] = None
            for attempt in range(1, _MAX_LLM_ATTEMPTS + 1):
                text_emitted = False
                last_exc = None
                try:
                    for stream_event in self.provider.generate_stream(
                        messages=state.messages,
                        tools=tool_schemas if tool_schemas else None,
                        temperature=config.temperature,
                        max_tokens=config.max_tokens,
                        timeout=timeout,
                    ):
                        if stream_event["type"] == "text_delta":
                            text_emitted = True
                            yield {
                                "type": "text_delta",
                                "content": stream_event["content"],
                                "iteration": state.iteration,
                            }
                        elif stream_event["type"] == "done":
                            response = stream_event["response"]
                    break  # success — exit retry loop
                except _RETRYABLE_EXCEPTIONS as exc:
                    last_exc = exc
                    logger.warning(
                        "LLM call timed out on attempt %d/%d (iteration %d): %s",
                        attempt,
                        _MAX_LLM_ATTEMPTS,
                        state.iteration,
                        exc,
                    )
                    if attempt == _MAX_LLM_ATTEMPTS or text_emitted:
                        break  # exhausted retries or mid-stream — give up
                    # else: loop to next attempt
                except Exception as exc:
                    last_exc = exc
                    break  # non-retryable error — give up immediately

            if last_exc is not None:
                logger.error(
                    "LLM call failed on iteration %d: %s", state.iteration, last_exc
                )
                step.phase = "error"
                step.completed_at = datetime.utcnow()
                state.steps.append(step)
                state.phase = "error"
                state.final_response = f"LLM call failed: {last_exc}"
                yield {"type": "error", "error": state.final_response}
                return

            if response is None:
                # generate_stream() completed without a "done" event — shouldn't happen
                logger.error("generate_stream() ended without a done event")
                state.phase = "error"
                state.final_response = "LLM stream ended unexpectedly."
                yield {"type": "error", "error": state.final_response}
                return

            # ── Populate step from response ────────────────────────────────
            step.llm_content = response.content
            step.llm_model = response.model or self.provider.get_model_name()
            step.tokens_in = response.tokens_in or 0
            step.tokens_out = response.tokens_out or 0

            logger.info(
                "LLM invoked: model=%s  iteration=%d  tokens_in=%s  tokens_out=%s",
                step.llm_model,
                state.iteration,
                step.tokens_in,
                step.tokens_out,
            )

            if response.tokens_in:
                state.tokens_used += response.tokens_in
            if response.tokens_out:
                state.tokens_used += response.tokens_out

            # Append the assistant message to the running context.
            # arguments must be a JSON string per the OpenAI wire format.
            assistant_msg: dict = {"role": "assistant"}
            if response.content:
                assistant_msg["content"] = response.content
            if response.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ]
            state.messages.append(assistant_msg)

            # ── No tool calls → final response ────────────────────────────
            if not response.tool_calls:
                step.phase = "complete"
                step.completed_at = datetime.utcnow()
                state.steps.append(step)
                state.phase = "complete"
                state.final_response = response.content or ""
                logger.debug("Agent complete after %d iteration(s)", state.iteration)
                yield {"type": "chunk", "content": state.final_response}
                return

            # ── Tool calls requested ──────────────────────────────────────
            step.phase = "executing"
            step.tool_calls = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in response.tool_calls
            ]
            yield {
                "type": "tool_calls",
                "iteration": state.iteration,
                "calls": [
                    {"name": tc.name, "arguments": tc.arguments}
                    for tc in response.tool_calls
                ],
            }

            # ── Approval gate ─────────────────────────────────────────────
            if config.enable_approval and self._check_needs_approval(
                response.tool_calls
            ):
                step.phase = "approval"
                step.completed_at = datetime.utcnow()
                state.steps.append(step)
                state.needs_approval = True
                state.phase = "approval"
                state.final_response = None
                yield {
                    "type": "approval_required",
                    "iteration": state.iteration,
                    "calls": [{"name": tc.name} for tc in response.tool_calls],
                }
                return

            # ── Execute tool calls ────────────────────────────────────────
            tool_result_msgs: list[dict] = []
            for tc in response.tool_calls:
                tool = self.tools.get(tc.name)
                if tool is None:
                    logger.warning("LLM requested unknown tool: %s", tc.name)
                    error_msg = f"Unknown tool: {tc.name}"
                    step.tool_results.append(
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "result": None,
                            "error": error_msg,
                        }
                    )
                    tool_result_msgs.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": f"Error: {error_msg}",
                        }
                    )
                    yield {
                        "type": "tool_result",
                        "iteration": state.iteration,
                        "name": tc.name,
                        "success": False,
                        "error": error_msg,
                    }
                    continue

                logger.debug("Executing tool '%s' with args: %s", tc.name, tc.arguments)
                try:
                    result = tool.execute(session=self.session, **tc.arguments)
                except Exception as exc:
                    logger.exception("Tool '%s' raised an exception", tc.name)
                    error_msg = str(exc)
                    step.tool_results.append(
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "result": None,
                            "error": error_msg,
                        }
                    )
                    tool_result_msgs.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": f"Error executing {tc.name}: {exc}",
                        }
                    )
                    yield {
                        "type": "tool_result",
                        "iteration": state.iteration,
                        "name": tc.name,
                        "success": False,
                        "error": error_msg,
                    }
                    continue

                step.tool_results.append(
                    {
                        "id": tc.id,
                        "name": result.tool_name,
                        "result": result.result,
                        "error": result.error,
                    }
                )
                tool_msg = result.to_message()
                tool_msg["tool_call_id"] = tc.id
                tool_result_msgs.append(tool_msg)
                yield {
                    "type": "tool_result",
                    "iteration": state.iteration,
                    "name": tc.name,
                    "success": result.success,
                    "error": result.error,
                }

            state.messages.extend(tool_result_msgs)
            step.completed_at = datetime.utcnow()
            state.steps.append(step)

        # ── Max iterations reached ─────────────────────────────────────────
        logger.warning("Agent hit max_iterations (%d)", state.max_iterations)
        state.phase = "error"
        state.final_response = (
            "I was unable to complete the task within the allowed number of steps. "
            "Please try again or simplify your request."
        )
        yield {"type": "error", "error": state.final_response}

    def _check_needs_approval(self, tool_calls: list[ToolCall]) -> bool:
        """Return True if any of the requested tools require human approval."""
        for tc in tool_calls:
            tool = self.tools.get(tc.name)
            if tool and tool.requires_approval:
                return True
        return False
