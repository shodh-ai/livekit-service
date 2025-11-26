# File: livekit-service/rox/frontend_client.py
# rox/frontend_client.py
"""
Frontend client for communicating with the UI via RPC calls.
Handles UI state changes and visual actions.
"""

import logging
import uuid
import base64
import json
from typing import Dict, Any, Optional, List
from livekit import rtc
import asyncio

try:
    from .generated.protos import interaction_pb2
    from .utils.ui_action_factory import build_ui_action_request
    from .config import get_settings
except Exception:
    from rox.generated.protos import interaction_pb2
    from rox.utils.ui_action_factory import build_ui_action_request
    from rox.config import get_settings

logger = logging.getLogger(__name__)

class FrontendClient:
    """Client for sending RPC commands to the frontend UI."""
    
    def __init__(self):
        self.rpc_method_name = "rox.interaction.ClientSideUI/PerformUIAction"

    async def _send_data(self, room: rtc.Room, identity: str, envelope: Dict[str, Any]) -> bool:
        if not identity or not room:
            return False
        try:
            payload_bytes = json.dumps(envelope).encode("utf-8")
            try:
                logger.info(f"[B2F DATA SEND] to '{identity}' type={envelope.get('type')} action={envelope.get('action')}")
            except Exception:
                pass
            await room.local_participant.publish_data(
                payload_bytes,
                reliable=True,
                destination_identities=[identity],
            )
            return True
        except Exception as e:
            logger.error(f"[B2F DATA FAIL] to '{identity}': {e}", exc_info=True)
            return False

    async def _send_ui_action(self, room: rtc.Room, identity: str, action: str, params: Dict[str, Any]) -> bool:
        """Helper to send a standard UI action over the data channel."""
        envelope = {
            "type": "ui",
            "action": action,
            "parameters": params or {},
        }
        return await self._send_data(room, identity, envelope)

    async def _send_rpc(self, room: rtc.Room, identity: str, action_type: str, parameters: Dict[str, Any], timeout_sec: Optional[float] = None) -> Optional[interaction_pb2.ClientUIActionResponse]:
        """
        Send an RPC call to the frontend client.
        
        Args:
            room: The LiveKit room
            identity: The client identity to send to
            action_type: The type of UI action to perform
            parameters: Parameters for the action
            
        Returns:
            The response from the client, or None if failed
        """
        if not identity:
            logger.error("Client identity not provided for RPC call")
            return None

        if not room:
            logger.error("Room not provided for RPC call")
            return None

        try:
            logger.info(f"Sending UI action '{action_type}' to client '{identity}' with parameters: {parameters}")
            
            # Build the protobuf request using the factory
            request_pb = build_ui_action_request(action_type, parameters)
            request_pb.request_id = f"ui-{uuid.uuid4().hex[:8]}"

            # Serialize and encode the request
            payload_bytes = request_pb.SerializeToString()
            base64_encoded_payload = base64.b64encode(payload_bytes).decode("utf-8")

            # Send the RPC with retries and extended timeout
            settings = get_settings()
            try:
                default_timeout = float(settings.FRONTEND_RPC_TIMEOUT_SEC)
            except Exception:
                default_timeout = 15.0
            eff_timeout = float(timeout_sec) if (timeout_sec and timeout_sec > 0) else default_timeout

            async def _do_rpc_once():
                # Verify destination presence when available
                try:
                    if hasattr(room, "remote_participants"):
                        rp = getattr(room, "remote_participants", {})
                        if isinstance(rp, dict) and identity not in rp:
                            raise RuntimeError(f"destination '{identity}' not present")
                except Exception:
                    pass
                try:
                    return await room.local_participant.perform_rpc(
                        destination_identity=identity,
                        method=self.rpc_method_name,
                        payload=base64_encoded_payload,
                        response_timeout=eff_timeout,
                    )
                except TypeError:
                    # Compatibility: some FakeLocalParticipant stubs may not support response_timeout
                    return await room.local_participant.perform_rpc(
                        destination_identity=identity,
                        method=self.rpc_method_name,
                        payload=base64_encoded_payload,
                    )

            # Breadcrumb before send
            try:
                logger.info(f"[B2F RPC SEND] ID: {request_pb.request_id}, Action: '{action_type}' to client '{identity}'")
            except Exception:
                pass

            max_retries = 3
            last_exc: Optional[Exception] = None
            response_payload_str = None
            for attempt in range(max_retries):
                try:
                    response_payload_str = await _do_rpc_once()
                    break
                except Exception as e:
                    last_exc = e
                    msg = str(e).lower()
                    if ("timeout" in msg or "not present" in msg) and attempt < max_retries - 1:
                        await asyncio.sleep(1.0)
                        continue
                    raise
            if response_payload_str is None and last_exc:
                raise last_exc

            # Parse the response
            response_bytes = base64.b64decode(response_payload_str)
            response_pb = interaction_pb2.ClientUIActionResponse()
            response_pb.ParseFromString(response_bytes)

            logger.info(f"UI action response from '{identity}': Success={response_pb.success}")
            try:
                logger.info(f"[B2F RPC SUCCESS] ID: {request_pb.request_id}: Success={response_pb.success}")
            except Exception:
                pass
            return response_pb

        except Exception as e:
            try:
                rid = request_pb.request_id if 'request_pb' in locals() else 'unknown'
                logger.error(f"[B2F RPC FAIL] ID: {rid} - Failed to send UI action RPC to '{identity}': {e}")
            except Exception:
                pass
            logger.error(f"Failed to send UI action RPC to '{identity}': {e}", exc_info=True)
            return None

    async def set_ui_state(self, room: rtc.Room, identity: str, params: Dict[str, Any]) -> bool:
        """
        Set the UI state on the frontend using UPDATE_TEXT_CONTENT action.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            params: State parameters (e.g., {"mode": "drawing", "tool": "pen"})
            
        Returns:
            True if successful, False otherwise
        """
        return await self._send_ui_action(room, identity, "SET_UI_STATE", params)

    async def execute_visual_action(self, room: rtc.Room, identity: str, tool_name: str, params: Dict[str, Any]) -> bool:
        """
        Execute a visual action on the frontend.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            tool_name: The name of the tool/action (e.g., "draw", "browser_navigate")
            params: Action parameters
            
        Returns:
            True if successful, False otherwise
        """
        # Map tool names to action types
        action_type_map = {
            "draw": "DRAW_ACTION",
            "browser_navigate": "BROWSER_NAVIGATE",
            "browser_click": "BROWSER_CLICK", 
            "browser_type": "BROWSER_TYPE",
            "highlight_element": "HIGHLIGHT_ELEMENT",
            "show_overlay": "SHOW_OVERLAY",
            "hide_overlay": "HIDE_OVERLAY",
            # Jupyter setup
            "setup_jupyter": "SETUP_JUPYTER",
            # Excalidraw Canvas Actions
            "clear_canvas": "EXCALIDRAW_CLEAR_CANVAS",
            "update_elements": "EXCALIDRAW_UPDATE_ELEMENTS",
            "remove_highlighting": "EXCALIDRAW_REMOVE_HIGHLIGHTING",
            "highlight_elements_advanced": "EXCALIDRAW_HIGHLIGHT_ELEMENTS_ADVANCED",
            "modify_elements": "EXCALIDRAW_MODIFY_ELEMENTS",
            "capture_screenshot": "EXCALIDRAW_CAPTURE_SCREENSHOT",
            "get_canvas_elements": "EXCALIDRAW_GET_CANVAS_ELEMENTS",
            "set_generating": "EXCALIDRAW_SET_GENERATING",
            # Alias: allow backend to request generation from text without changing protos
            "GENERATE_VISUALIZATION_FROM_TEXT": "GENERATE_VISUALIZATION"
        }
        
        action_type = action_type_map.get(tool_name, tool_name.upper())
        return await self._send_ui_action(room, identity, action_type, params)

    async def send_suggested_responses(
        self,
        room: rtc.Room,
        identity: str,
        suggestions: Optional[List[Dict[str, Any]]] = None,
        title: Optional[str] = None,
        group_id: Optional[str] = None,
        responses: Optional[List[str]] = None,
    ) -> bool:
        """
        Send SUGGESTED_RESPONSES to the frontend. Supports both rich suggestions
        (list of {id, text, reason}) and legacy responses (list of strings).

        Args:
            room: LiveKit room
            identity: destination identity
            suggestions: list of dicts with id/text/reason
            title: optional title/header
            group_id: optional correlation id
            responses: legacy list of strings

        Returns:
            True if successful, False otherwise.
        """
        # Publish via DataChannel so the frontend's generic handler can deep-find metadata.suggested_responses
        try:
            # Normalize to either array of objects with id/text/reason or array of strings
            payload_items: Any
            if suggestions and isinstance(suggestions, list):
                norm_items: List[Dict[str, Any]] = []
                for idx, s in enumerate(suggestions):
                    try:
                        if isinstance(s, dict):
                            norm_items.append({
                                "id": s.get("id") or f"s_{int(asyncio.get_event_loop().time()*1000)}_{idx}",
                                "text": s.get("text") or str(s),
                                "reason": s.get("reason")
                            })
                        else:
                            norm_items.append({
                                "id": f"s_{int(asyncio.get_event_loop().time()*1000)}_{idx}",
                                "text": str(s)
                            })
                    except Exception:
                        pass
                payload_items = norm_items
            elif responses and isinstance(responses, list):
                payload_items = [str(r) for r in responses]
            else:
                payload_items = []

            envelope = {
                # type can be anything; frontend looks for metadata.suggested_responses anywhere
                "type": "ai",
                "metadata": {
                    "title": title or None,
                    "group_id": group_id or None,
                    "suggested_responses": payload_items,
                },
            }
            ok = await self._send_data(room, identity, envelope)
            if ok:
                try:
                    logger.info(
                        f"[B2F DATA] suggested_responses sent (title={title!r}, count={len(payload_items)})"
                    )
                except Exception:
                    pass
            else:
                logger.error("[B2F DATA] suggested_responses send failed")
            return ok
        except Exception as e:
            logger.error(f"Failed to send suggested responses over DataChannel: {e}", exc_info=True)
            return False

    async def trigger_rrweb_replay(
        self,
        room: rtc.Room,
        identity: str,
        events_url: str,
        start_timestamp: Optional[int] = None,
        play_duration_ms: Optional[int] = None
    ) -> bool:
        """
        Ask frontend to start an rrweb replay from a URL via DataChannel.

        Args:
            room: LiveKit room
            identity: Target participant identity
            events_url: URL to fetch rrweb events from
            start_timestamp: Optional timestamp (ms) to start playback from (RAG-controlled)
            play_duration_ms: Optional duration (ms) to play before auto-pausing (RAG-controlled)
        """
        params = {"events_url": events_url}

        # Add RAG-controlled playback parameters if provided
        if start_timestamp is not None:
            params["start_timestamp"] = start_timestamp
            logger.info(f"[FrontendClient] Sending rrweb with start_timestamp: {start_timestamp}ms")

        if play_duration_ms is not None:
            params["play_duration_ms"] = play_duration_ms
            logger.info(f"[FrontendClient] Sending rrweb with play_duration_ms: {play_duration_ms}ms")

        return await self._send_ui_action(room, identity, "RRWEB_REPLAY", params)

    async def get_block_content_from_frontend(self, room: rtc.Room, identity: str, block_id: str, timeout_sec: float = 15.0) -> Optional[Dict[str, Any]]:
        """
        Request the frontend to return the full JSON content of a whiteboard feed block.

        NOTE: This relies on the frontend interpreting a generic action with parameters
        { action: "GET_BLOCK_CONTENT", block_id } and returning a JSON string in the
        ClientUIActionResponse.message field.

        Returns the parsed dict on success, or None on failure/timeout.
        """
        if not block_id:
            logger.error("get_block_content_from_frontend called without block_id")
            return None
        # This endpoint requires a response payload. Keeping RPC for this method only.
        params = {"action": "GET_BLOCK_CONTENT", "block_id": block_id}
        try:
            logger.info(f"[RPC][Request] Fetching block content for id={block_id}")
            resp = await self._send_rpc(room, identity, "SET_UI_STATE", params, timeout_sec=timeout_sec)
            if not resp or not resp.success:
                logger.error(f"[RPC][Response] Failed to fetch block content for id={block_id}")
                return None
            raw = (resp.message or "").strip()
            if not raw:
                logger.warning(f"[RPC][Response] Empty message for block id={block_id}")
                return None
            try:
                import json
                data = json.loads(raw)
                logger.info(f"[RPC][Response] Received block content for id={block_id} (keys={list(data.keys())})")
                return data
            except Exception as pe:
                logger.error(f"[RPC][Response] Non-JSON or invalid message for block id={block_id}: {raw[:80]}... error={pe}")
                return None
        except Exception as e:
            logger.error(f"RPC error while requesting block content for id={block_id}: {e}", exc_info=True)
            return None

    async def highlight_element(self, room: rtc.Room, identity: str, element_id: str) -> bool:
        """
        Highlight a specific element on the frontend using HIGHLIGHT_TEXT_RANGES action.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            element_id: ID of the element to highlight
            
        Returns:
            True if successful, False otherwise
        """
        return await self._send_ui_action(room, identity, "HIGHLIGHT_ELEMENT", {"element_id": element_id})

    async def speak_with_highlight(self, room: rtc.Room, identity: str, text: str, highlight_words: Optional[Dict[str, str]] = None) -> bool:
        """
        Send text to be spoken with synchronized word highlighting.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            text: The text to speak
            highlight_words: Optional mapping of words to element IDs for highlighting
            
        Returns:
            True if successful, False otherwise
        """
        params = {
            "text": text,
            "highlight_words": highlight_words or {}
        }
        response = await self._send_rpc(room, identity, "SPEAK_WITH_HIGHLIGHT", params)
        return response is not None and response.success

    async def show_feedback(self, room: rtc.Room, identity: str, feedback_type: str, message: str, duration_ms: int = 3000) -> bool:
        """
        Show feedback message on the frontend using SHOW_ALERT action.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            feedback_type: Type of feedback ("info", "success", "warning", "error")
            message: The feedback message
            duration_ms: Duration to show the feedback in milliseconds
            
        Returns:
            True if successful, False otherwise
        """
        return await self._send_ui_action(room, identity, "SHOW_FEEDBACK", {
            "feedback_type": feedback_type,
            "message": message,
            "duration_ms": duration_ms,
        })

    async def generate_visualization(self, room: rtc.Room, identity: str, elements: list = None, prompt: str = None) -> bool:
        """
        Generate a professional visualization on the canvas.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            elements: Array of SkeletonElement objects for structured visualization (preferred)
            prompt: Description of the visualization to create (fallback for legacy support)
            
        Returns:
            True if successful, False otherwise
        """
        if elements:
            params = {"elements": elements}
        elif prompt:
            params = {"prompt": prompt}
        else:
            params = {"elements": []}
        
        return await self._send_ui_action(room, identity, "GENERATE_VISUALIZATION", params)

    async def highlight_elements(self, room: rtc.Room, identity: str, element_ids: list, highlight_type: str = "attention", duration_ms: int = 3000) -> bool:
        """
        Highlight specific UI elements.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            element_ids: List of element IDs to highlight
            highlight_type: Type of highlighting ("attention", "success", "error")
            duration_ms: Duration to show highlighting
            
        Returns:
            True if successful, False otherwise
        """
        return await self._send_ui_action(room, identity, "HIGHLIGHT_ELEMENTS", {
            "element_ids": element_ids,
            "highlight_type": highlight_type,
            "duration_ms": duration_ms,
        })

    async def give_student_control(self, room: rtc.Room, identity: str, message: str) -> bool:
        """
        Transfer control to the student with a message.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            message: Message to show when giving control
            
        Returns:
            True if successful, False otherwise
        """
        return await self._send_ui_action(room, identity, "GIVE_STUDENT_CONTROL", {"message": message})

    async def take_ai_control(self, room: rtc.Room, identity: str, message: str) -> bool:
        """
        AI regains control with a message.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            message: Message to show when taking control
            
        Returns:
            True if successful, False otherwise
        """
        return await self._send_ui_action(room, identity, "TAKE_AI_CONTROL", {"message": message})

    async def clear_all_annotations(self, room: rtc.Room, identity: str) -> bool:
        """
        Clear all visual annotations and highlights.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            
        Returns:
            True if successful, False otherwise
        """
        return await self._send_ui_action(room, identity, "CLEAR_ALL_ANNOTATIONS", {})

    async def set_mic_enabled(self, room: rtc.Room, identity: str, enabled: bool, message: str = "") -> bool:
        """
        Enable or disable the student's microphone from the backend.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            enabled: True to enable mic, False to disable
            message: Optional message to show to the student
            
        Returns:
            True if successful, False otherwise
        """
        # Feature removed: mic auto-enable/disable no longer dispatched to frontend
        try:
            logger.info(f"[MIC CONTROL SUPPRESSED] set_mic_enabled(enabled={enabled}) for '{identity}' skipped (feature removed)")
        except Exception:
            pass
        return True
