# rox/agent.py
import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Callable, Awaitable
from contextlib import asynccontextmanager

from livekit import rtc, agents
from livekit.agents.llm import LLM, ChatChunk, ChoiceDelta, ChatContext

try:
    from .config import get_settings
    from .langgraph_client import LangGraphClient
    from .frontend_client import FrontendClient
    from .browser_pod_client import BrowserPodClient
    from .request_context import set_request_context
except Exception:
    from config import get_settings
    from langgraph_client import LangGraphClient
    from frontend_client import FrontendClient
    from browser_pod_client import BrowserPodClient
    from request_context import set_request_context
from opentelemetry import metrics, trace

logger = logging.getLogger(__name__)

settings = get_settings()

# Metrics (best-effort)
try:
    meter = metrics.get_meter("rox.agent")
    _active_agents_counter = meter.create_up_down_counter(
        "rox.agents.active", unit="1", description="Active RoxAgent instances"
    )
    _tool_usage_counter = meter.create_counter(
        "rox.agent.tool.runs", unit="1", description="Tool executions"
    )
except Exception:
    _active_agents_counter = None
    _tool_usage_counter = None

tracer = trace.get_tracer(__name__)


class RoxAgent(agents.Agent):
    class TranscriptInterceptor(LLM):
        def __init__(self, outer: "RoxAgent", debounce_ms: int = 500):
            super().__init__()
            self._outer = outer
            self._debounce_ms = debounce_ms
            self._buffer: list[str] = []
            self._debounce_handle: Optional[asyncio.TimerHandle] = None

        def chat(self, *, chat_ctx: ChatContext = None, tools=None, tool_choice=None, **kwargs):
            return self._chat_ctx_mgr(chat_ctx)

        async def _flush(self):
            full_transcript = " ".join(self._buffer)
            self._buffer.clear()
            task_name = "student_spoke_or_acted"
            turn_payload = {
                "transcript": full_transcript,
                "current_context": {
                    "user_id": self._outer.user_id,
                    "session_id": self._outer.session_id,
                    "interaction_type": "speech",
                },
            }
            await self._outer.trigger_langgraph_task(
                task_name=task_name,
                json_payload=json.dumps(turn_payload),
                caller_identity=self._outer.caller_identity,
            )

        async def _stream_empty(self):
            yield ChatChunk(id=str(uuid.uuid4()), delta=ChoiceDelta(role="assistant", content=""))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def __anext__(self):
            raise StopAsyncIteration

        async def __aiter__(self):
            return self

        @asynccontextmanager
        async def _chat_ctx_mgr(self, chat_ctx: ChatContext):
            # extract transcript
            transcript = ""
            if chat_ctx:
                messages = getattr(chat_ctx, "_items", [])
                for msg in reversed(messages):
                    role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
                    if str(role).lower() == "user":
                        transcript = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
                        if isinstance(transcript, list):
                            transcript = " ".join(map(str, transcript))
                        transcript = str(transcript)
                        break
            if transcript:
                self._buffer.append(transcript)
            try:
                yield self._stream_empty()
            finally:
                pass

    def __init__(self, **kwargs):
        kwargs.setdefault("instructions", "You are Rox, an AI tutor.")
        super().__init__(**kwargs)

        # State
        self.user_id: Optional[str] = None
        self.session_id: Optional[str] = None
        # Dynamic HTTP endpoint for the per-room browser pod (set in entrypoint)
        self.browser_http_url: Optional[str] = None
        self.caller_identity: Optional[str] = None
        self.curriculum_id: str = "ai_business_expert_424d7f"
        self.current_lo_id: Optional[str] = None
        self._expected_user_input_type: str = "INTERRUPTION"
        self._interruption_pending: bool = False
        self._current_execution_cancelled: bool = False
        self._session_started: bool = False
        self._start_task_enqueued: bool = False
        self._start_task_enqueued_at: Optional[float] = None

        self._current_delivery_plan: Optional[List[Dict]] = None
        self._current_plan_index: int = 0
        self._interrupted_plan: Optional[List[Dict]] = None
        self._interrupted_plan_index: int = 0
        self._interrupted_plan_context: Optional[Dict] = None
        self._interruption_timestamp: Optional[float] = None
        self._is_paused: bool = False
        self._last_mic_enable_ts: Optional[float] = None

        self._processing_queue: asyncio.Queue = asyncio.Queue()
        self.llm_interceptor = self.TranscriptInterceptor(self)

        self.rrweb_events_buffer: List[Dict[str, Any]] = []
        self.live_events_buffer: List[Dict[str, Any]] = []
        self._langgraph_client = LangGraphClient(self)
        self._frontend_client = FrontendClient()
        self._browser_pod_client = BrowserPodClient()
        self._gemini_tts_client = None

        self._room: Optional[rtc.Room] = None
        self.agent_session: Optional[agents.AgentSession] = None
        self._initial_nav_sent: bool = False
        self._browser_join_cb_registered: bool = False

        try:
            if _active_agents_counter:
                _active_agents_counter.add(1)
        except Exception:
            pass
        self._shutdown_recorded = False

        # Build dispatch table
        self._action_handlers: Dict[str, Callable[..., Awaitable]] = self._build_action_handlers()

    def _build_action_handlers(self) -> Dict[str, Callable[..., Awaitable]]:
        return {
            # speech
            "speak": self._execute_speak,
            "speak_text": self._execute_speak,
            # ui
            "set_ui_state": self._execute_set_ui_state,
            "wait": self._execute_wait,
            "draw": self._execute_draw,
            # browser pod commands
            "browser_navigate": self._execute_browser_command,
            "browser_click": self._execute_browser_command,
            "browser_type": self._execute_browser_command,
            "browser_type_text": self._execute_browser_command,
            "web_click": self._execute_browser_command,
            "web_fill_input": self._execute_browser_command,
            "browser_highlight_element": self._execute_browser_command,
            "jupyter_run_cell": self._execute_browser_command,
            "jupyter_add_cell": self._execute_browser_command,
            "jupyter_update_cell": self._execute_browser_command,
            "jupyter_delete_cell": self._execute_browser_command,
            "vscode_add_line": self._execute_browser_command,
            "vscode_delete_line": self._execute_browser_command,
            "vscode_run_terminal_command": self._execute_browser_command,
            "vscode_highlight_add": self._execute_browser_command,
            "vscode_highlight_remove": self._execute_browser_command,
            "vscode_create_file": self._execute_browser_command,
            "n8n_upsert_workflow_from_asset": self._execute_browser_command,
            # jupyter/frontend helpers
            "jupyter_create_new_cell": self._execute_frontend_action,
            "jupyter_scroll_to_cell": self._execute_frontend_action,
            "jupyter_click_pyodide": self._execute_frontend_action,
            "highlight_cell_for_doubt_resolution": self._execute_frontend_action,
            # annotations / feedback / control
            "clear_all_annotations": self._execute_clear_all_annotations,
            "focus_on_block": self._execute_focus_on_block,
            "highlight_elements": self._execute_highlight_elements,
            "give_student_control": self._execute_give_student_control,
            "take_ai_control": self._execute_take_ai_control,
            "show_feedback": self._execute_show_feedback,
            "suggested_responses": self._execute_suggested_responses,
            "show_suggested_responses": self._execute_suggested_responses,
            "SUGGESTED_RESPONSES": self._execute_suggested_responses,
            # expectation / listening
            "listen": self._execute_listen,
            "START_LISTENING_VISUAL": self._execute_start_listening_visual,
            "STOP_LISTENING_VISUAL": self._execute_stop_listening_visual,
            "prompt_for_student_action": self._execute_prompt_for_student_action,
            # excalidraw canvas
            "clear_canvas": self._execute_frontend_action,
            "update_elements": self._execute_frontend_action,
            "remove_highlighting": self._execute_frontend_action,
            "highlight_elements_advanced": self._execute_frontend_action,
            "modify_elements": self._execute_frontend_action,
            "capture_screenshot": self._execute_capture_screenshot,
            "get_canvas_elements": self._execute_get_canvas_elements,
            "set_generating": self._execute_frontend_action,
            "capture_canvas_screenshot": self._execute_frontend_action,
            # visual generation
            "display_visual_aid": self._execute_display_visual_aid,
            "add_excalidraw_block": self._execute_add_excalidraw_block,
            "update_excalidraw_block": self._execute_update_excalidraw_block,
            # rrweb replay
            "replay_parsed_rrweb_actions": self._execute_rrweb_replay,
            "trigger_rrweb_replay": self._execute_trigger_rrweb_replay,
            # get content
            "get_block_content": self._execute_get_block_content,
        }

    def _ensure_dispatch_table(self) -> None:
        if not hasattr(self, "_action_handlers") or not isinstance(self._action_handlers, dict):
            self._action_handlers = self._build_action_handlers()

    async def processing_loop(self):
        logger.info("Conductor's main processing loop started")
        while True:
            try:
                task = await self._processing_queue.get()
                # Determine task name for logging and tracing
                task_name: Optional[str] = None
                try:
                    if isinstance(task, dict):
                        task_name = str(task.get("task_name") or task.get("task") or "")
                except Exception:
                    task_name = None

                # Propagate session/user context into logging contextvars
                try:
                    set_request_context(self.session_id, self.user_id)
                except Exception:
                    pass

                span_name = f"agent_task.{task_name or 'unknown_task'}"
                with tracer.start_as_current_span(span_name) as task_span:
                    # Attach attributes for Tempo/Grafana
                    try:
                        task_span.set_attribute("session_id", self.session_id)
                        task_span.set_attribute("student_id", self.user_id)
                        task_span.set_attribute("task.name", task_name or "unknown_task")
                        try:
                            task_span.set_attribute("task.payload", json.dumps(task))
                        except Exception:
                            pass
                    except Exception:
                        pass

                    # Structured log of the incoming task
                    try:
                        logger.info(
                            f"Processing agent task: {task_name or 'unknown_task'}",
                            extra={"json_payload": json.dumps(task)},
                        )
                    except Exception:
                        logger.info(f"Processing agent task: {task_name or 'unknown_task'}")

                    # Attach current LO for the brain
                    try:
                        if isinstance(task, dict):
                            task.setdefault("current_lo_id", self.current_lo_id)
                    except Exception:
                        pass

                    # Single-start semantics: mark and dedupe start task
                    if task_name == "start_tutoring_session":
                        if self._session_started:
                            logger.info("[processing_loop] Ignoring duplicate start_tutoring_session (already started)")
                            self._processing_queue.task_done()
                            continue
                        # First time start: mark flags
                        try:
                            self._session_started = True
                            # Clear enqueued flag now that we're executing start
                            self._start_task_enqueued = False
                        except Exception:
                            pass

                    # Call brain
                    response = None
                    try:
                        response = await self._langgraph_client.invoke_langgraph_task(
                            task, self.user_id, self.curriculum_id, self.session_id
                        )
                    except Exception as e:
                        logger.error(f"LangGraph invocation failed: {e}", exc_info=True)
                    if not response:
                        self._processing_queue.task_done()
                        continue
                    # Update context
                    new_lo = response.get("current_lo_id")
                    if new_lo:
                        self.current_lo_id = new_lo
                    # Execute delivery plan
                    delivery_plan = response.get("delivery_plan", {})
                    actions = delivery_plan.get("actions", []) if isinstance(delivery_plan, dict) else []
                    if actions:
                        await self._execute_toolbelt(actions)
                    self._processing_queue.task_done()
            except Exception:
                logger.debug("processing_loop recovered from error", exc_info=True)
                await asyncio.sleep(0.05)

    async def _execute_toolbelt(self, toolbelt: List[Dict[str, Any]]):
        with tracer.start_as_current_span("execute_toolbelt", attributes={"actions.count": len(toolbelt)}):
            logger.info(f"Executing toolbelt with {len(toolbelt)} actions")
            optimized_toolbelt = self._optimize_speech_actions(toolbelt)
            self._current_delivery_plan = list(optimized_toolbelt)
            NON_BLOCKING_TOOLS = {"display_visual_aid", "add_excalidraw_block", "update_excalidraw_block"}
            for i, action in enumerate(optimized_toolbelt):
                self._current_plan_index = i
                if self._is_paused:
                    return
                if self._current_execution_cancelled:
                    self._current_execution_cancelled = False
                    return
                tool_name = action.get("tool_name")
                parameters = action.get("parameters", {})
                try:
                    coro = self._execute_single_action(tool_name, parameters)
                    if tool_name in NON_BLOCKING_TOOLS:
                        asyncio.create_task(coro)
                        continue
                    result = await coro
                    if isinstance(result, tuple) and len(result) == 2:
                        control, value = result
                        if control == "break":
                            break
                        if control == "return":
                            return value
                except Exception as e:
                    try:
                        if _tool_usage_counter:
                            _tool_usage_counter.add(0, {"tool_name": str(tool_name), "status": "error"})
                    except Exception:
                        pass
                    logger.error(f"Blocking action '{tool_name}' failed: {e}", exc_info=True)

    async def _execute_single_action(self, tool_name: str, parameters: Dict[str, Any]):
        try:
            if _tool_usage_counter:
                _tool_usage_counter.add(1, {"tool_name": str(tool_name)})
        except Exception:
            pass
        self._ensure_dispatch_table()
        handler = self._action_handlers.get(tool_name)
        if not handler:
            logger.warning(f"Unknown tool_name: {tool_name}")
            return (None, None)
        # Call the handler, passing tool_name only if it's expected
        import inspect
        sig = inspect.signature(handler)
        if 'tool_name' in sig.parameters:
            return await handler(tool_name, parameters)
        return await handler(parameters)

    async def _execute_add_excalidraw_block(self, parameters: Dict[str, Any]):
        raw = parameters or {}
        out_params: Dict[str, Any] = {}
        if raw.get("id"):
            out_params["id"] = raw.get("id")
        elif raw.get("block_id"):
            out_params["id"] = raw.get("block_id")
        if raw.get("summary") is not None:
            out_params["summary"] = raw.get("summary")
        elif raw.get("title") is not None:
            out_params["summary"] = raw.get("title")
        elements = raw.get("elements")
        if elements is None:
            elements = raw.get("initial_elements")
        if elements is not None:
            out_params["elements"] = elements
        if self._frontend_client and self._room and self.caller_identity:
            try:
                await self._frontend_client.execute_visual_action(self._room, self.caller_identity, "add_excalidraw_block", out_params)
            except Exception as e:
                logger.error(f"[ADD_BLOCK] Failed to forward add_excalidraw_block: {e}", exc_info=True)
        else:
            logger.warning(f"Frontend client not available - would add_excalidraw_block: {out_params}")
        return (None, None)

    async def _execute_update_excalidraw_block(self, parameters: Dict[str, Any]):
        raw = parameters or {}
        out_params: Dict[str, Any] = {}
        bid = raw.get("id") or raw.get("block_id") or raw.get("blockId")
        if bid:
            out_params["id"] = bid
        elems = raw.get("elements")
        if elems is None:
            elems = raw.get("modifications")
        if elems is not None:
            out_params["elements"] = elems
        if self._frontend_client and self._room and self.caller_identity:
            try:
                await self._frontend_client.execute_visual_action(self._room, self.caller_identity, "update_excalidraw_block", out_params)
            except Exception as e:
                logger.error(f"[UPDATE_BLOCK] Failed to forward update_excalidraw_block: {e}", exc_info=True)
        else:
            logger.warning(f"Frontend client not available - would update_excalidraw_block: {out_params}")
        return (None, None)

    def _optimize_speech_actions(self, toolbelt: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not toolbelt:
            return toolbelt
        optimized: List[Dict[str, Any]] = []
        buf: List[str] = []
        def flush():
            nonlocal buf
            if buf:
                optimized.append({"tool_name": "speak", "parameters": {"text": " ".join(buf)}})
                buf = []
        for action in toolbelt:
            t = action.get("tool_name")
            if t in ("speak", "speak_text"):
                text = action.get("parameters", {}).get("text", "")
                if isinstance(text, str) and text.strip():
                    buf.append(text.strip())
            else:
                flush()
                optimized.append(action)
        flush()
        return optimized

    # --- Individual Handler Methods ---
    async def _execute_speak(self, parameters: Dict[str, Any]):
        text = parameters.get("text", "")
        cleaned_text = text.strip()
        if cleaned_text and self.agent_session:
            try:
                if self._frontend_client and self._room and self.caller_identity:
                    speaker_label = (
                        getattr(self, "agent_display_name", None)
                        or getattr(self, "agent_identity", None)
                        or "AI Tutor"
                    )
                    envelope = {"type": "transcript", "text": cleaned_text, "speaker": speaker_label}
                    await self._frontend_client._send_data(self._room, self.caller_identity, envelope)
            except Exception:
                logger.debug("[SPEAK] Failed to send agent transcript to frontend (non-fatal)", exc_info=True)
            try:
                playback_handle = await self.agent_session.say(cleaned_text, allow_interruptions=True)
                await playback_handle
                logger.info(f"TTS completed for: {cleaned_text[:50]}...")
            except Exception as e:
                logger.error(f"TTS operation failed for '{cleaned_text[:50]}...': {e}", exc_info=True)
        else:
            logger.warning("No text to speak or agent_session not available")
        return (None, None)

    async def _execute_set_ui_state(self, parameters: Dict[str, Any]):
        if self._frontend_client:
            await self._frontend_client.set_ui_state(self._room, self.caller_identity, parameters)
        else:
            logger.info(f"Frontend client not available - would set UI state: {parameters}")
        return (None, None)

    async def _execute_wait(self, parameters: Dict[str, Any]):
        ms = parameters.get("ms") or parameters.get("milliseconds") or 0
        try:
            ms_val = float(ms)
        except Exception:
            ms_val = 0.0
        ms_val = max(0.0, min(ms_val, 600000.0))
        await asyncio.sleep(ms_val / 1000.0)
        return (None, None)

    async def _execute_draw(self, parameters: Dict[str, Any]):
        if self._frontend_client:
            await self._frontend_client.execute_visual_action(self._room, self.caller_identity, "draw", parameters)
        else:
            logger.warning(f"Frontend client not available - would execute draw: {parameters}")
        return (None, None)

    async def _execute_browser_command(self, tool_name: str, parameters: Dict[str, Any]):
        browser_pod_identity = f"browser-bot-{self.session_id}" if self.session_id else None
        if self._browser_pod_client and self._room and browser_pod_identity:
            try:
                await self._browser_pod_client.send_browser_command(self._room, browser_pod_identity, tool_name, parameters)
                logger.info(f"Sent {tool_name} to browser pod {browser_pod_identity}")
            except Exception as e:
                logger.error(f"Failed to send {tool_name} to browser pod: {e}")
        else:
            logger.warning("Browser Pod client or room/session_id not available; cannot send browser action")
        return (None, None)

    async def _execute_frontend_action(self, tool_name: str, parameters: Dict[str, Any]):
        if self._frontend_client:
            await self._frontend_client.execute_visual_action(self._room, self.caller_identity, tool_name, parameters)
        else:
            logger.warning(f"Frontend client not available - would execute {tool_name} with parameters: {parameters}")
        return (None, None)

    async def _execute_clear_all_annotations(self, parameters: Dict[str, Any]):
        if self._frontend_client:
            await self._frontend_client.clear_all_annotations(self._room, self.caller_identity)
        return (None, None)

    async def _execute_focus_on_block(self, parameters: Dict[str, Any]):
        raw = parameters or {}
        out_params = {}
        bid = raw.get("id") or raw.get("block_id") or raw.get("blockId")
        if bid:
            out_params["id"] = bid
        if self._frontend_client and self._room and self.caller_identity:
            try:
                await self._frontend_client.execute_visual_action(self._room, self.caller_identity, "focus_on_block", out_params)
            except Exception as e:
                logger.error(f"[FOCUS_BLOCK] Failed to forward focus_on_block: {e}", exc_info=True)
        return (None, None)

    async def _execute_highlight_elements(self, parameters: Dict[str, Any]):
        if self._frontend_client:
            await self._frontend_client.highlight_elements(
                self._room,
                self.caller_identity,
                element_ids=parameters.get("element_ids", []),
                highlight_type=parameters.get("highlight_type", "attention"),
                duration_ms=parameters.get("duration_ms", 3000),
            )
        return (None, None)

    async def _execute_give_student_control(self, parameters: Dict[str, Any]):
        if self._frontend_client:
            await self._frontend_client.give_student_control(self._room, self.caller_identity, message=parameters.get("message", ""))
        return (None, None)

    async def _execute_take_ai_control(self, parameters: Dict[str, Any]):
        if self._frontend_client:
            await self._frontend_client.take_ai_control(self._room, self.caller_identity, message=parameters.get("message", ""))
        return (None, None)

    async def _execute_show_feedback(self, parameters: Dict[str, Any]):
        if self._frontend_client:
            await self._frontend_client.show_feedback(
                self._room,
                self.caller_identity,
                message=parameters.get("message", ""),
                feedback_type=parameters.get("type", "info"),
                duration_ms=parameters.get("duration_ms", 5000),
            )
        return (None, None)

    async def _execute_suggested_responses(self, parameters: Dict[str, Any]):
        if self._frontend_client:
            try:
                await self._frontend_client.send_suggested_responses(
                    self._room,
                    self.caller_identity,
                    suggestions=parameters.get("suggestions"),
                    title=parameters.get("title") or parameters.get("prompt"),
                    group_id=parameters.get("group_id"),
                    responses=parameters.get("responses"),
                )
            except Exception as e:
                logger.error(f"Failed to send suggested responses: {e}", exc_info=True)
        return (None, None)

    async def _execute_listen(self, parameters: Dict[str, Any]):
        self._expected_user_input_type = "INTERRUPTION"
        logger.info("[LISTEN] Mic control suppressed; expectation set to INTERRUPTION")
        return (None, None)

    async def _execute_start_listening_visual(self, parameters: Dict[str, Any]):
        logger.info("[START_LISTENING_VISUAL] Suppressed (mic control removed)")
        return (None, None)

    async def _execute_stop_listening_visual(self, parameters: Dict[str, Any]):
        logger.info("[STOP_LISTENING_VISUAL] Suppressed (mic control removed)")
        return (None, None)

    async def _execute_prompt_for_student_action(self, parameters: Dict[str, Any]):
        prompt_text = parameters.get("prompt_text", "")
        if prompt_text and self.agent_session:
            await self.agent_session.say(text=prompt_text, allow_interruptions=True)
        self._expected_user_input_type = "SUBMISSION"
        return ("break", None)

    async def _execute_capture_screenshot(self, parameters: Dict[str, Any]):
        if self._frontend_client:
            result = await self._frontend_client.execute_visual_action(self._room, self.caller_identity, "capture_screenshot", parameters)
            return ("return", result)
        return ("return", None)

    async def _execute_get_canvas_elements(self, parameters: Dict[str, Any]):
        if self._frontend_client:
            result = await self._frontend_client.execute_visual_action(self._room, self.caller_identity, "get_canvas_elements", parameters)
            return ("return", result)
        return ("return", [])

    async def _execute_display_visual_aid(self, parameters: Dict[str, Any]):
        if not self._frontend_client:
            logger.warning(f"Frontend client not available - would forward display_visual_aid: {parameters}")
            return (None, None)
        try:
            raw_params = parameters or {}
            topic_context = str(raw_params.get("topic_context", "") or "").strip()
            raw_prompt = raw_params.get("text_to_visualize")
            if raw_prompt is None:
                raw_prompt = raw_params.get("prompt")
            if raw_prompt is None:
                prompt = ""
            elif isinstance(raw_prompt, (dict, list)):
                prompt = json.dumps(raw_prompt, ensure_ascii=False)
            else:
                prompt = str(raw_prompt)
            prompt = prompt.strip()

            def _has_meaningful_text(s: str) -> bool:
                return any(ch.isalnum() for ch in s)

            if not prompt or not _has_meaningful_text(prompt):
                warn_msg = "Unable to generate a diagram: empty or non-meaningful prompt."
                logger.warning(f"[display_visual_aid] {warn_msg} params={raw_params}")
                await self._frontend_client.show_feedback(
                    self._room,
                    self.caller_identity,
                    feedback_type="error",
                    message=warn_msg,
                    duration_ms=5000,
                )
                return (None, None)

            MAX_PROMPT_LEN = 8000
            if len(prompt) > MAX_PROMPT_LEN:
                logger.info(f"[display_visual_aid] Truncating prompt from {len(prompt)} to {MAX_PROMPT_LEN} chars")
                prompt = prompt[:MAX_PROMPT_LEN]

            clean_payload = {"prompt": prompt, "topic_context": topic_context}
            ok = await self._frontend_client.execute_visual_action(
                self._room,
                self.caller_identity,
                "GENERATE_VISUALIZATION",
                clean_payload,
            )
            if ok:
                logger.info("[display_visual_aid] Dispatched sanitized prompt to frontend for visualization (DataChannel)")
            else:
                logger.error("[display_visual_aid] DataChannel dispatch failed for GENERATE_VISUALIZATION")
        except Exception:
            logger.error("[display_visual_aid] Failed to process/forward visualization request", exc_info=True)
            try:
                await self._frontend_client.show_feedback(
                    self._room,
                    self.caller_identity,
                    feedback_type="error",
                    message="Visualization failed due to an internal error.",
                    duration_ms=5000,
                )
            except Exception:
                pass
        return (None, None)

    async def _execute_rrweb_replay(self, parameters: Dict[str, Any]):
        try:
            curriculum_id = parameters.get("curriculum_id")
            lo_id = parameters.get("lo_id")
            delay_ms = parameters.get("delay_ms", 500)
            if not curriculum_id or not lo_id:
                logger.warning("Missing curriculum_id or lo_id for replay_parsed_rrweb_actions")
                return (None, None)
            browser_identity = None
            for pid, participant in getattr(self._room, "remote_participants", {}).items():
                ident = str(getattr(participant, "identity", "") or pid)
                if ident.startswith("browser-bot-"):
                    browser_identity = ident
                    break
            if not browser_identity:
                logger.warning("No browser-bot participant found for replay_parsed_rrweb_actions")
                return (None, None)
            command = {
                "type": "replay_rrweb_sequence_from_brum",
                "curriculum_id": curriculum_id,
                "lo_id": lo_id,
                "delay_ms": delay_ms,
            }
            await self._room.local_participant.publish_data(json.dumps(command).encode("utf-8"), destination_identities=[browser_identity])
        except Exception as e:
            logger.error(f"Failed to replay parsed rrweb actions: {e}", exc_info=True)
        return (None, None)

    async def _execute_trigger_rrweb_replay(self, parameters: Dict[str, Any]):
        """
        Handler for trigger_rrweb_replay action from kamikaze.
        Triggers an rrweb replay on the frontend using the events_url.

        Supports RAG-controlled playback with optional start_timestamp and play_duration_ms.
        """
        try:
            events_url = parameters.get("events_url")
            if not events_url:
                logger.warning("[trigger_rrweb_replay] Missing events_url parameter")
                return (None, None)

            # Extract RAG-controlled playback parameters
            start_timestamp = parameters.get("start_timestamp")
            play_duration_ms = parameters.get("play_duration_ms")

            if self._frontend_client and self._room and self.caller_identity:
                log_msg = f"[trigger_rrweb_replay] Triggering replay: {events_url}"
                if start_timestamp is not None:
                    log_msg += f" | start_timestamp={start_timestamp}ms"
                if play_duration_ms is not None:
                    log_msg += f" | duration={play_duration_ms}ms"
                logger.info(log_msg)

                await self._frontend_client.trigger_rrweb_replay(
                    self._room,
                    self.caller_identity,
                    events_url,
                    start_timestamp=start_timestamp,
                    play_duration_ms=play_duration_ms
                )
            else:
                logger.warning(f"[trigger_rrweb_replay] Frontend client not available - would trigger: {events_url}")
        except Exception as e:
            logger.error(f"[trigger_rrweb_replay] Failed to trigger rrweb replay: {e}", exc_info=True)
        return (None, None)

    async def _execute_get_block_content(self, parameters: Dict[str, Any]):
        block_id = str(parameters.get("block_id") or parameters.get("id") or "").strip()
        if not block_id:
            logger.warning("[get_block_content] Missing block_id parameter; skipping")
            return (None, None)
        if self._frontend_client and self._room and self.caller_identity:
            try:
                content = await self._frontend_client.get_block_content_from_frontend(self._room, self.caller_identity, block_id)
                if content is None:
                    logger.error(f"[get_block_content] Frontend did not return content for ID: {block_id}")
                else:
                    followup_task = {
                        "task_name": "handle_response",
                        "caller_identity": self.caller_identity,
                        "interaction_type": "block_content",
                        "block_id": block_id,
                        "block_content": content,
                        "transcript": f"[block_content:{block_id}]",
                    }
                    await self._processing_queue.put(followup_task)
            except Exception as e:
                logger.error(f"[get_block_content] Error while requesting block content: {e}", exc_info=True)
        else:
            logger.warning("[get_block_content] Frontend client, room, or caller_identity not available; cannot fetch content")
        return (None, None)

    # --- Public helpers used by entrypoint orchestration ---
    async def trigger_langgraph_task(self, task_name: str, json_payload: str, caller_identity: str):
        try:
            payload = json.loads(json_payload) if isinstance(json_payload, str) else (json_payload or {})
        except Exception:
            payload = {}
        task = {"task_name": task_name, "caller_identity": caller_identity, **(payload if isinstance(payload, dict) else {})}
        await self._processing_queue.put(task)

    async def optimistic_interrupt_ack(self, message: Optional[str] = None):
        try:
            self._is_paused = True
            self._current_execution_cancelled = True
            if self.agent_session:
                try:
                    self.agent_session.interrupt()
                except Exception:
                    pass
                try:
                    ack_text = (message or "Yes — go ahead, what's your doubt?").strip()
                    if ack_text:
                        handle = await self.agent_session.say(ack_text, allow_interruptions=True)
                        async def _wait_short():
                            try:
                                await asyncio.wait_for(handle, timeout=2.0)
                            except Exception:
                                try:
                                    handle.interrupt()
                                except Exception:
                                    pass
                        asyncio.create_task(_wait_short())
                except Exception:
                    pass
            self._expected_user_input_type = "INTERRUPTION"
        except Exception:
            logger.debug("optimistic_interrupt_ack failed (non-fatal)", exc_info=True)

    async def handle_interruption(self, task: Dict[str, Any]):
        try:
            self._is_paused = True
            self._current_execution_cancelled = True
            if self.agent_session:
                try:
                    self.agent_session.interrupt()
                except Exception:
                    pass
            enriched = dict(task)
            enriched["interaction_type"] = "interruption"
            enriched["_already_forwarded"] = True
            await self._processing_queue.put(enriched)
        except Exception:
            logger.debug("handle_interruption failed (non-fatal)", exc_info=True)

    def _clear_interrupted_plan_context(self):
        self._interrupted_plan = None
        self._interrupted_plan_index = 0
        self._interrupted_plan_context = None

    async def _resume_interrupted_plan(self):
        if not self._interrupted_plan:
            return
        remaining = self._interrupted_plan[self._interrupted_plan_index:]
        if not remaining:
            return
        self._current_delivery_plan = remaining
        self._current_plan_index = 0
        await self._execute_toolbelt(remaining)

    async def cleanup(self):
        if not getattr(self, "_shutdown_recorded", False):
            try:
                if _active_agents_counter:
                    _active_agents_counter.add(-1)
            except Exception:
                pass
            self._shutdown_recorded = True
