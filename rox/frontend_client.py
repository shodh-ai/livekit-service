# File: livekit-service/rox/frontend_client.py
# rox/frontend_client.py
"""
Frontend client for communicating with the UI via RPC calls.
Handles UI state changes and visual actions.
"""

import logging
import uuid
import base64
from typing import Dict, Any, Optional
from livekit import rtc
from generated.protos import interaction_pb2
from utils.ui_action_factory import build_ui_action_request

logger = logging.getLogger(__name__)

class FrontendClient:
    """Client for sending RPC commands to the frontend UI."""
    
    def __init__(self):
        self.rpc_method_name = "rox.interaction.ClientSideUI/PerformUIAction"

    async def _send_rpc(self, room: rtc.Room, identity: str, action_type: str, parameters: Dict[str, Any]) -> Optional[interaction_pb2.ClientUIActionResponse]:
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

        try:
            logger.info(f"Sending UI action '{action_type}' to client '{identity}'")
            
            # Build the protobuf request using the factory
            request_pb = build_ui_action_request(action_type, parameters)
            request_pb.request_id = f"ui-{uuid.uuid4().hex[:8]}"

            # Serialize and encode the request
            payload_bytes = request_pb.SerializeToString()
            base64_encoded_payload = base64.b64encode(payload_bytes).decode("utf-8")

            # Send the RPC
            response_payload_str = await room.local_participant.perform_rpc(
                destination_identity=identity,
                method=self.rpc_method_name,
                payload=base64_encoded_payload,
            )

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
            "hide_overlay": "HIDE_OVERLAY"
        }
        
        action_type = action_type_map.get(tool_name, tool_name.upper())
        response = await self._send_rpc(room, identity, action_type, params)
        return response is not None and response.success

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
