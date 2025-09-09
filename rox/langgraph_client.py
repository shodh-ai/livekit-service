# File: livekit-service/rox/langgraph_client.py
# rox/langgraph_client.py
"""
LangGraph client for communicating with the Brain service.
Handles task invocation and response parsing.
"""

import logging
import json
import os
import aiohttp
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

class LangGraphClient:
    def __init__(self, agent=None):
        # The URL should point to your new Student Tutor agent's base

        # Support both LANGGRAPH_TUTOR_URL and legacy LANGGRAPH_API_URL
        self.base_url = os.getenv("LANGGRAPH_TUTOR_URL") or os.getenv("LANGGRAPH_API_URL", "http://localhost:8001")
        self.timeout = aiohttp.ClientTimeout(total=120.0)
        self.vnc_http_url = os.getenv("VNC_LISTENER_HTTP_URL")  # e.g., http://localhost:8766
        # New: direct HTTP server on browser pod for on-demand screenshots
        self.browser_pod_http_url = os.getenv("BROWSER_POD_HTTP_URL")  # e.g., http://browser-pod:8777
        # Store reference to the RoxAgent to access rrweb_events_buffer
        self.agent = agent
        # Initialization logs for diagnostics
        logger.info(f"LangGraphClient base_url={self.base_url}")
        if self.browser_pod_http_url:
            logger.info(f"LangGraphClient BROWSER_POD_HTTP_URL set: {self.browser_pod_http_url}")
        elif self.vnc_http_url:
            logger.info(f"LangGraphClient VNC_LISTENER_HTTP_URL set: {self.vnc_http_url}")
        else:
            logger.info("LangGraphClient: No browser HTTP source configured; visual_context may be omitted.")

    async def _fetch_visual_context(self, session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
        """Optionally fetch a screenshot from the browser pod HTTP endpoint; fallback to VNC listener.

        Returns None if not configured or on failure.
        """
        url = None
        source = None
        if self.browser_pod_http_url:
            url = self.browser_pod_http_url.rstrip('/') + "/screenshot"
            source = "browser_pod"
        elif self.vnc_http_url:
            url = self.vnc_http_url.rstrip('/') + "/screenshot"
            source = "vnc_listener"
        else:
            logger.debug("No browser HTTP URL configured; skipping visual context fetch.")
            return None
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
                    logger.info(f"Fetched screenshot (no 'success' flag present) len={b64_len} preview={b64_preview}...")
                else:
                    logger.info(f"Fetched screenshot_b64 len={b64_len} preview={b64_preview}...")
                return {
                    "source": source or "browser_pod",
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
        
        # Base request body for all endpoints
        request_body = {
            "session_id": session_id,
            "student_id": user_id,
            "curriculum_id": curriculum_id,
            "current_lo_id": task.get("current_lo_id", None)
        }

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

                async with session.post(full_url, json=request_body) as response:
                    response.raise_for_status()
                    logger.info(f"LangGraphClient response status: {response.status}")

                    # Try to parse JSON, otherwise capture text
                    try:
                        response_data = await response.json()
                        logger.info(f"LangGraphClient response JSON: {json.dumps(response_data, ensure_ascii=False)}")
                    except Exception:
                        response_text = await response.text()
                        logger.info(f"LangGraphClient response text: {response_text}")
                        # If not JSON, we can't proceed as expected
                        return None
                    
                    # The actual LangGraph service returns {"delivery_plan": delivery_plan}
                    # We need to wrap it in the format the main agent loop expects
                    if response_data and "delivery_plan" in response_data:
                        logger.info(f"Received response from LangGraph service: delivery_plan with {len(response_data['delivery_plan'].get('actions', []))} actions")
                        
                        # Return in the format expected by main.py
                        return {
                            "delivery_plan": response_data["delivery_plan"],
                            "current_lo_id": task.get("current_lo_id")  # Preserve current_lo_id
                        }
                    else:
                        logger.warning("No delivery_plan received from LangGraph service")
                        return None
                        
        except aiohttp.ClientError as e:
            logger.error(f"HTTP error communicating with LangGraph: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error invoking LangGraph task: {e}", exc_info=True)
            return None
