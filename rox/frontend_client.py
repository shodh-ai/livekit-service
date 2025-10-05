# File: livekit-service/rox/frontend_client.py
# rox/frontend_client.py
"""
Frontend client for communicating with the UI via RPC calls.
Handles UI state changes and visual actions.
"""

import logging
import uuid
import base64
from typing import Dict, Any, Optional, List
from livekit import rtc
from generated.protos import interaction_pb2
from utils.ui_action_factory import build_ui_action_request

logger = logging.getLogger(__name__)

class FrontendClient:
    """Client for sending RPC commands to the frontend UI."""
    
    def __init__(self):
        self.rpc_method_name = "rox.interaction.ClientSideUI/PerformUIAction"

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

            # Send the RPC
            async def _do_rpc():
                return await room.local_participant.perform_rpc(
                    destination_identity=identity,
                    method=self.rpc_method_name,
                    payload=base64_encoded_payload,
                )

            if timeout_sec and timeout_sec > 0:
                import asyncio
                response_payload_str = await asyncio.wait_for(_do_rpc(), timeout=timeout_sec)
            else:
                response_payload_str = await _do_rpc()

            # Parse the response
            response_bytes = base64.b64decode(response_payload_str)
            response_pb = interaction_pb2.ClientUIActionResponse()
            response_pb.ParseFromString(response_bytes)

            logger.info(f"UI action response from '{identity}': Success={response_pb.success}")
            return response_pb

        except Exception as e:
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
        response = await self._send_rpc(room, identity, "SET_UI_STATE", params)
        return response is not None and response.success

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
        response = await self._send_rpc(room, identity, action_type, params)
        return response is not None and response.success

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
        params: Dict[str, Any] = {}
        if suggestions:
            params["suggestions"] = suggestions
        if responses:
            params["responses"] = responses
        if title:
            params["title"] = title
        if group_id:
            params["group_id"] = group_id

        response = await self._send_rpc(room, identity, "SUGGESTED_RESPONSES", params)
        return response is not None and response.success

    async def trigger_rrweb_replay(self, room: rtc.Room, identity: str, events_url: str) -> bool:
        """
        Send an RPC command to the frontend to start an rrweb replay from a URL.
        """
        params = {"events_url": events_url}
        response = await self._send_rpc(room, identity, "RRWEB_REPLAY", params)
        return response is not None and response.success

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
        params = {
            "action": "GET_BLOCK_CONTENT",
            "block_id": block_id,
        }
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
        params = {"element_id": element_id}
        response = await self._send_rpc(room, identity, "HIGHLIGHT_ELEMENT", params)
        return response is not None and response.success

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
        params = {
            "feedback_type": feedback_type,
            "message": message,
            "duration_ms": duration_ms
        }
        response = await self._send_rpc(room, identity, "SHOW_FEEDBACK", params)
        return response is not None and response.success

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
        
        response = await self._send_rpc(room, identity, "GENERATE_VISUALIZATION", params)
        return response is not None and response.success

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
        params = {
            "element_ids": element_ids,
            "highlight_type": highlight_type,
            "duration_ms": duration_ms
        }
        response = await self._send_rpc(room, identity, "HIGHLIGHT_ELEMENTS", params)
        return response is not None and response.success

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
        params = {"message": message}
        response = await self._send_rpc(room, identity, "GIVE_STUDENT_CONTROL", params)
        return response is not None and response.success

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
        params = {"message": message}
        response = await self._send_rpc(room, identity, "TAKE_AI_CONTROL", params)
        return response is not None and response.success

    async def clear_all_annotations(self, room: rtc.Room, identity: str) -> bool:
        """
        Clear all visual annotations and highlights.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            
        Returns:
            True if successful, False otherwise
        """
        params = {}
        response = await self._send_rpc(room, identity, "CLEAR_ALL_ANNOTATIONS", params)
        return response is not None and response.success

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
        # Use the existing action types that the frontend already supports
        action_type = "START_LISTENING_VISUAL" if enabled else "STOP_LISTENING_VISUAL"
        params = {
            "message": message
        } if message else {}
        
        response = await self._send_rpc(room, identity, action_type, params)
        return response is not None and response.success

    # --- NEW CINEMATIC DEMO METHODS ---
    
    async def show_media_on_feed(self, room: rtc.Room, identity: str, media_type: str, url: str, caption: str = "") -> bool:
        """
        Display media (image/GIF/meme) directly in the conversational feed.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            media_type: Type of media ("image", "gif", "meme")
            url: Public URL of the media
            caption: Optional caption text
            
        Returns:
            True if successful, False otherwise
        """
        params = {
            "media_type": media_type,
            "url": url,
            "caption": caption
        }
        response = await self._send_rpc(room, identity, "SHOW_MEDIA_ON_FEED", params)
        return response is not None and response.success

    async def highlight_ui_element(self, room: rtc.Room, identity: str, selector: str, text: str) -> bool:
        """
        Highlight a UI element with a glowing border and tooltip.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            selector: CSS selector for the element
            text: Tooltip text to display
            
        Returns:
            True if successful, False otherwise
        """
        params = {
            "selector": selector,
            "text": text
        }
        response = await self._send_rpc(room, identity, "HIGHLIGHT_UI_ELEMENT", params)
        return response is not None and response.success

    async def play_audio_snippet(self, room: rtc.Room, identity: str, asset_id: str, start_time_ms: int = 0, duration_ms: int = 0) -> bool:
        """
        Play a segment of an audio file (e.g., expert's voice).
        
        Args:
            room: The LiveKit room
            identity: The client identity
            asset_id: GCS asset ID of the audio file
            start_time_ms: Start time in milliseconds
            duration_ms: Duration to play in milliseconds
            
        Returns:
            True if successful, False otherwise
        """
        params = {
            "asset_id": asset_id,
            "start_time_ms": start_time_ms,
            "duration_ms": duration_ms
        }
        response = await self._send_rpc(room, identity, "PLAY_AUDIO_SNIPPET", params)
        return response is not None and response.success

    async def end_session(self, room: rtc.Room, identity: str, final_message: str) -> bool:
        """
        End the demo session with a final message.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            final_message: Final message to display
            
        Returns:
            True if successful, False otherwise
        """
        params = {
            "final_message": final_message
        }
        response = await self._send_rpc(room, identity, "END_SESSION", params)
        return response is not None and response.success

    async def send_scene_metadata(self, room: rtc.Room, identity: str, metadata: Dict[str, Any]) -> bool:
        """
        Send scene metadata to frontend to signal end of scene and autoplay instructions.
        This is the critical signal that enables the autoplay engine.
        
        Args:
            room: The LiveKit room
            identity: The client identity
            metadata: Scene metadata including continue_mode, step_index, etc.
            
        Returns:
            True if successful, False otherwise
        """
        params = {
            "metadata": metadata
        }
        response = await self._send_rpc(room, identity, "SCENE_METADATA", params)
        return response is not None and response.success
