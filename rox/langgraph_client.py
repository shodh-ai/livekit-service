# File: livekit-service/rox/langgraph_client.py
# rox/langgraph_client.py
"""
LangGraph client for communicating with the Brain service.
Handles task invocation and response parsing.
"""

import logging
import json
import os
import asyncio
import aiohttp
import random
from typing import Dict, Any, List, Optional

try:
    from .config import get_settings
except Exception:
    from config import get_settings

logger = logging.getLogger(__name__)

class LangGraphClient:
    def __init__(self, agent=None):
        # The URL should point to your new Student Tutor agent's base

        # Support both LANGGRAPH_TUTOR_URL and legacy LANGGRAPH_API_URL
        settings = get_settings()
        self.base_url = (
            settings.LANGGRAPH_TUTOR_URL
            or settings.LANGGRAPH_API_URL
            or settings.MY_CUSTOM_AGENT_URL
            or "http://localhost:8001"
        )

        # Configurable HTTP timeouts
        _sock_connect = (
            float(settings.LANGGRAPH_SOCK_CONNECT_TIMEOUT)
            if settings.LANGGRAPH_SOCK_CONNECT_TIMEOUT is not None
            else float(settings.LANGGRAPH_CONNECT_TIMEOUT)
        )
        self.timeout = aiohttp.ClientTimeout(
            total=float(settings.LANGGRAPH_TOTAL_TIMEOUT),
            connect=float(settings.LANGGRAPH_CONNECT_TIMEOUT),
            sock_connect=_sock_connect,
            sock_read=float(settings.LANGGRAPH_READ_TIMEOUT),
        )

        # Retry configuration
        self.max_retries = int(settings.LANGGRAPH_MAX_RETRIES)
        self.backoff_base = float(settings.LANGGRAPH_BACKOFF_BASE)
        self.vnc_http_url = settings.VNC_LISTENER_HTTP_URL  # e.g., http://localhost:8766
        # New: direct HTTP server on browser pod for on-demand screenshots
        self.browser_pod_http_url = settings.BROWSER_POD_HTTP_URL  # e.g., http://browser-pod:8777
        # Store reference to the RoxAgent to access rrweb_events_buffer
        self.agent = agent
        # Controls to minimize payload size
        self.include_visual_context = bool(settings.LANGGRAPH_INCLUDE_VISUAL_CONTEXT)
        self.attach_buffers = bool(settings.LANGGRAPH_ATTACH_BUFFERS)
        # Initialization logs for diagnostics
        logger.info(
            f"LangGraphClient base_url={self.base_url} (timeout total={self.timeout.total}s, connect={self.timeout.connect}s, sock_connect={self.timeout.sock_connect}s, sock_read={self.timeout.sock_read}s, retries={self.max_retries})"
        )
        if self.browser_pod_http_url and self.include_visual_context:
            logger.info(f"LangGraphClient BROWSER_POD_HTTP_URL set: {self.browser_pod_http_url}")
        elif self.vnc_http_url and self.include_visual_context:
            logger.info(f"LangGraphClient VNC_LISTENER_HTTP_URL set: {self.vnc_http_url}")
        else:
            logger.info("LangGraphClient: No browser HTTP source configured; visual_context may be omitted.")

    async def _fetch_visual_context(self, session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
        """Optionally fetch a screenshot from the browser pod HTTP endpoint using the
        dynamic URL attached to the RoxAgent instance.

        Returns None if not configured or on failure.
        """
        if not self.include_visual_context:
            logger.debug("Visual context disabled via LANGGRAPH_INCLUDE_VISUAL_CONTEXT=false")
            return None

        # CHANGE: Use the dynamic URL from the agent instance, not static env configuration.
        base_url = None
        try:
            if getattr(self, "agent", None) is not None:
                base_url = getattr(self.agent, "browser_http_url", None)
        except Exception:
            base_url = None

        if not base_url:
            logger.debug("No dynamic browser_http_url found on agent; skipping visual context fetch.")
            return None

        url = base_url.rstrip("/") + "/screenshot"

        try:
            logger.debug(f"Fetching visual context from: {url}")
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"/screenshot fetch non-200: {resp.status}")
                    return None
                data = await resp.json()
                logger.debug(f"/screenshot response keys: {list(data.keys())}")
                success = data.get("success")
                if success is False:
                    logger.warning("/screenshot returned success=false; skipping visual context.")
                    return None
                b64 = data.get("screenshot_b64")
                if not b64:
                    logger.info("/screenshot returned no screenshot_b64; skipping visual context.")
                    return None
                b64_len = len(b64)
                b64_preview = b64[:60]
                if success is None:
                    logger.info(
                        f"Fetched screenshot (no 'success' flag present) len={b64_len} preview={b64_preview}..."
                    )
                else:
                    logger.info(
                        f"Fetched screenshot_b64 len={b64_len} preview={b64_preview}..."
                    )
                return {
                    "source": "browser_pod",
                    "screenshot_b64": b64,
                    "page_url": data.get("url"),
                    "captured_at": data.get("timestamp"),
                }
        except Exception as e:
            logger.warning(f"Failed to fetch visual context: {e}")
            return None


    # --- SIGNATURE CHANGE ---
    async def invoke_langgraph_task(self, task: Dict, user_id: str, curriculum_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Invoke a task with LangGraph and return the delivery plan.
        
        Args:
            task: The task dictionary containing task_name and other parameters
            user_id: The student identifier
            curriculum_id: The curriculum identifier for the current course
            session_id: The session identifier
            
        Returns:
            The response dictionary containing delivery_plan, or None if failed.
        """
        logger.info(f"Invoking LangGraph with task: {task.get('task_name')}")
        
        safe_session_id = str(session_id or "")
        safe_student_id = str(user_id or "")
        safe_curriculum_id = str(curriculum_id or "")

        # Base request body for all endpoints
        request_body = {
            "session_id": safe_session_id,
            "student_id": safe_student_id,
            "curriculum_id": safe_curriculum_id,
            "current_lo_id": task.get("current_lo_id", None)
        }

        # Pass-through enriched context from frontend/session if provided
        try:
            if task.get("restored_feed_summary") is not None:
                rfs = task.get("restored_feed_summary")
                # Normalize to list to match Kamikaze's expected schema (List[Dict])
                if isinstance(rfs, dict) and "blocks" in rfs and isinstance(rfs["blocks"], list):
                    request_body["restored_feed_summary"] = rfs["blocks"]
                elif isinstance(rfs, list):
                    request_body["restored_feed_summary"] = rfs
                else:
                    # Fallback: coerce single object into a single-element list
                    request_body["restored_feed_summary"] = [rfs]
        except Exception:
            pass
        try:
            if task.get("block_content") is not None:
                request_body["block_content"] = task.get("block_content")
            if task.get("block_id") is not None:
                request_body["block_id"] = task.get("block_id")
        except Exception:
            pass

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                # Route to appropriate endpoint based on task type
                if task.get("task_name") == "start_tutoring_session":
                    endpoint = "/start_session"
                    # For session start, use base request body (no student_input needed)
                elif task.get("interaction_type") == "interruption" or task.get("task_name") in ["handle_interruption", "student_mic_button_interrupt", "student_wants_to_interrupt"]:
                    endpoint = "/handle_interruption"
                    # Interruption payload
                    interrupt_text = task.get("transcript") or request_body.get("student_input") or "[Student interrupted]"
                    request_body["student_input"] = interrupt_text
                    # Pass through interrupted_plan_context if provided
                    if task.get("interrupted_plan_context"):
                        request_body["interrupted_plan_context"] = task.get("interrupted_plan_context")
                    # Compose visual_context from task and optional VNC screenshot
                    vc = task.get("visual_context") or {}
                    vnc_vc = await self._fetch_visual_context(session)
                    if vnc_vc:
                        # Merge, task-provided fields take precedence
                        merged = {**vnc_vc, **vc}
                        request_body["visual_context"] = merged
                    elif vc:
                        request_body["visual_context"] = vc
                else:
                    endpoint = "/handle_response"
                    # Add student_input for regular response
                    request_body["student_input"] = task.get("transcript", "")
                    # Compose visual_context from task and optional VNC screenshot
                    vc = task.get("visual_context") or {}
                    vnc_vc = await self._fetch_visual_context(session)
                    if vnc_vc:
                        merged = {**vnc_vc, **vc}
                        request_body["visual_context"] = merged
                    elif vc:
                        request_body["visual_context"] = vc
                
                # --- ROBUSTNESS CHANGE: Construct URL safely ---
                full_url = f"{self.base_url}{endpoint}"

                # Ensure session_id is propagated to the Student Tutor service
                # so that conversational context is maintained across turns.
                if session_id:
                    request_body["session_id"] = session_id

                # Log outgoing request for correlation (mask large base64 fields)
                try:
                    safe_body = dict(request_body)
                    vc = safe_body.get("visual_context")
                    if isinstance(vc, dict):
                        vc_masked = dict(vc)
                        b64 = vc_masked.get("screenshot_b64") or ""
                        if b64:
                            vc_masked["screenshot_b64"] = f"[len={len(b64)} preview={b64[:60]}...]"
                        vc_masked["has_screenshot_b64"] = bool(b64)
                        safe_body["visual_context"] = vc_masked
                    logger.info(f"LangGraphClient POST {full_url} body: {json.dumps(safe_body, ensure_ascii=False)}")
                except Exception:
                    # Fallback to repr if JSON serialization fails
                    logger.info(f"LangGraphClient POST {full_url} body(repr): {request_body!r}")

                # Attach buffered rrweb events if available (imprinter mode)
                if self.attach_buffers:
                    try:
                        if getattr(self, "agent", None) is not None:
                            events = getattr(self.agent, "rrweb_events_buffer", None)
                            if isinstance(events, list) and len(events) > 0:
                                logger.info(f"Attaching {len(events)} rrweb events to LangGraph request.")
                                request_body["rrweb_events"] = list(events)
                                # Clear buffer after attaching
                                events.clear()
                    except Exception:
                        logger.debug("Failed to attach rrweb_events to request body", exc_info=True)

                # Attach live monitoring events for Kamikaze if present
                if self.attach_buffers:
                    try:
                        if getattr(self, "agent", None) is not None:
                            live_events = getattr(self.agent, "live_events_buffer", None)
                            if isinstance(live_events, list) and len(live_events) > 0:
                                rr = [e.get("event_payload") for e in live_events if e.get("source") == "rrweb" and e.get("event_payload") is not None]
                                vc = [e.get("event_payload") for e in live_events if e.get("source") == "vscode" and e.get("event_payload") is not None]
                                if rr:
                                    request_body["raw_rrweb_events"] = rr
                                if vc:
                                    request_body["raw_vscode_events"] = vc
                                logger.info(f"Attached live events: rrweb={len(rr)} vscode={len(vc)}")
                                live_events.clear()
                    except Exception:
                        logger.debug("Failed to attach live events to request body", exc_info=True)

                # Prepare trace headers if present
                headers = None
                try:
                    trace_id = task.get("trace_id") if isinstance(task, dict) else None
                    if trace_id:
                        headers = {"X-Trace-Id": str(trace_id)}
                        logger.info(f"[TRACE] Propagating trace_id={trace_id} to {full_url}")
                except Exception:
                    headers = None

                # POST with retry logic on timeouts and transient errors
                last_error: Optional[Exception] = None
                for attempt in range(1, self.max_retries + 1):
                    try:
                        logger.info(
                            f"POST attempt {attempt}/{self.max_retries} -> {full_url}"
                        )
                        async with session.post(full_url, json=request_body, headers=headers) as response:
                            status = response.status
                            if 400 <= status < 500:
                                # Do not retry on client errors; capture body for diagnosis
                                try:
                                    err_text = await response.text()
                                except Exception:
                                    err_text = "<no body>"
                                logger.error(f"LangGraphClient 4xx response {status} from {full_url}: {err_text}")
                                return None
                            # For 5xx and others, raise for retry handling
                            response.raise_for_status()
                            logger.info(
                                f"LangGraphClient response status: {status}"
                            )

                            # Try to parse JSON, otherwise capture text
                            try:
                                response_data = await response.json()
                                logger.info(
                                    f"LangGraphClient response JSON: {json.dumps(response_data, ensure_ascii=False)}"
                                )
                            except Exception:
                                response_text = await response.text()
                                logger.info(
                                    f"LangGraphClient response text (non-JSON): {response_text}"
                                )
                                # If not JSON, we can't proceed as expected
                                return None

                            # The actual LangGraph service returns {"delivery_plan": delivery_plan}
                            # We need to wrap it in the format the main agent loop expects
                            if response_data and "delivery_plan" in response_data:
                                logger.info(
                                    f"Received response from LangGraph service: delivery_plan with {len(response_data['delivery_plan'].get('actions', []))} actions"
                                )

                                # Return in the format expected by main.py
                                return {
                                    "delivery_plan": response_data["delivery_plan"],
                                    "current_lo_id": task.get("current_lo_id"),  # Preserve current_lo_id
                                }
                            else:
                                logger.warning(
                                    "No delivery_plan received from LangGraph service"
                                )
                                return None
                    except asyncio.TimeoutError as e:
                        last_error = e
                        if attempt < self.max_retries:
                            delay = self.backoff_base * (2 ** (attempt - 1)) + random.random() * 0.5
                            logger.warning(
                                f"Timeout on attempt {attempt}/{self.max_retries}. Retrying in {delay:.2f}s..."
                            )
                            await asyncio.sleep(delay)
                            continue
                        else:
                            logger.error(
                                f"All retries exhausted due to timeout: {e}",
                            )
                            break
                    except aiohttp.ClientResponseError as e:
                        # Raised by raise_for_status(); treat 5xx as retryable, 4xx handled above
                        last_error = e
                        if 500 <= e.status < 600 and attempt < self.max_retries:
                            delay = self.backoff_base * (2 ** (attempt - 1)) + random.random() * 0.5
                            logger.warning(
                                f"Server error {e.status} on attempt {attempt}/{self.max_retries}. Retrying in {delay:.2f}s..."
                            )
                            await asyncio.sleep(delay)
                            continue
                        else:
                            logger.error(
                                f"Non-retryable HTTP error or retries exhausted: {e}"
                            )
                            break
                    except aiohttp.ClientError as e:
                        last_error = e
                        # For transient network errors, also retry
                        if attempt < self.max_retries:
                            delay = self.backoff_base * (2 ** (attempt - 1)) + random.random() * 0.5
                            logger.warning(
                                f"HTTP client error on attempt {attempt}/{self.max_retries}: {e}. Retrying in {delay:.2f}s..."
                            )
                            await asyncio.sleep(delay)
                            continue
                        else:
                            logger.error(
                                f"All retries exhausted due to HTTP error: {e}",
                            )
                            break

                if last_error:
                    logger.error(
                        f"Failed to communicate with LangGraph after {self.max_retries} attempts: {last_error}"
                    )
                return None
                        
        except aiohttp.ClientError as e:
            logger.error(f"HTTP error communicating with LangGraph: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error invoking LangGraph task: {e}", exc_info=True)
            return None
