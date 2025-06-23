# ui_action_factory.py

import logging
import uuid
from typing import Callable, Dict, Any

from livekit.rtc import Room
from generated.protos import interaction_pb2

logger = logging.getLogger(__name__)

# The signature for all our builder functions.
# It takes the main request object and the parameters from LangGraph.
ActionBuilder = Callable[[interaction_pb2.AgentToClientUIActionRequest, Dict[str, Any]], None]

# --- Builder Functions ---
# Each function knows how to handle exactly ONE type of payload.

def _build_highlight_payload(request_pb: interaction_pb2.AgentToClientUIActionRequest, params: Dict[str, Any]):
    """Builds the payload for HIGHLIGHT_TEXT_RANGES."""
    ranges_data = params.get("ranges", [])
    for r_data in ranges_data:
        request_pb.highlight_ranges_payload.add(
            id=str(r_data.get("id", "")),
            start=int(r_data.get("from", 0)), # Use 'from' and 'to' consistent with your mod page
            end=int(r_data.get("to", 0)),
            type=str(r_data.get("type", "highlight"))
        )
    logger.info(f"Built highlight payload with {len(ranges_data)} ranges.")

def _build_alert_payload(request_pb: interaction_pb2.AgentToClientUIActionRequest, params: Dict[str, Any]):
    """Builds the payload for SHOW_ALERT."""
    # For simple actions, we just populate the generic parameters map.
    request_pb.parameters["message"] = str(params.get("message", "Alert!"))
    logger.info(f"Built alert payload with message: {params.get('message')}")

def _build_set_editor_content_payload(request_pb: interaction_pb2.AgentToClientUIActionRequest, params: Dict[str, Any]):
    """Builds the payload for SET_EDITOR_CONTENT."""
    payload = request_pb.set_editor_content_payload
    payload.editor_id = str(params.get("editor_id", ""))
    payload.content_html = str(params.get("content_html", ""))
    logger.info(f"Built set editor content payload for editor: {payload.editor_id}")


def _build_append_text_payload(request_pb: interaction_pb2.AgentToClientUIActionRequest, params: Dict[str, Any]):
    """Builds the payload for APPEND_TEXT_TO_EDITOR_REALTIME."""
    text_chunk = params.get("text_chunk", "")
    # Populate the specific payload field, not the generic map
    request_pb.append_text_to_editor_realtime_payload.text_chunk = str(text_chunk)
    logger.info(f"Built append_text_to_editor_realtime payload with chunk size: {len(text_chunk)}")


# --- The Action Registry ---
ACTION_BUILDER_REGISTRY: Dict[str, ActionBuilder] = {
    "HIGHLIGHT_TEXT_RANGES": _build_highlight_payload,
    "SHOW_ALERT": _build_alert_payload,
    "SET_EDITOR_CONTENT": _build_set_editor_content_payload,
    "APPEND_TEXT_TO_EDITOR_REALTIME": _build_append_text_payload,
}

def build_ui_action_request(action_type_str: str, parameters: Dict[str, Any]) -> interaction_pb2.AgentToClientUIActionRequest:
    """
    Factory function to build a complete AgentToClientUIActionRequest
    using the registry.
    """
    try:
        action_type_enum = interaction_pb2.ClientUIActionType.Value(action_type_str)
    except ValueError:
        logger.error(f"Unknown UI action type string: '{action_type_str}'")
        raise

    request_pb = interaction_pb2.AgentToClientUIActionRequest(action_type=action_type_enum)
    
    builder_func = ACTION_BUILDER_REGISTRY.get(action_type_str)
    
    if builder_func:
        builder_func(request_pb, parameters)
    else:
        # Default fallback for actions that only use the generic map
        logger.warning(f"No specific builder for '{action_type_str}'. Using generic parameters.")
        for key, value in parameters.items():
            request_pb.parameters[key] = str(value)

    return request_pb


async def trigger_client_ui_action(
    room: Room,
    client_identity: str,
    action_type: str,
    parameters: Dict[str, Any],
):
    """
    Builds and sends a UI action request to a specific client.
    This is the central function used by the backend to trigger UI changes.
    """
    logger.info(
        f"Triggering UI action '{action_type}' for client '{client_identity}' "
        f"with params: {parameters}"
    )
    try:
        # Use the factory to construct the correct protobuf message
        request_pb = build_ui_action_request(action_type, parameters)
        
        # Add a unique request_id for tracking
        request_pb.request_id = f"ui-action-{uuid.uuid4().hex}"

        # Send the RPC request to the specific client
        # The frontend must have a handler for "AgentToClientUIAction"
        await room.rpc.send_request(
            target_identities=[client_identity],
            name="AgentToClientUIAction",
            message=request_pb,
        )
        logger.info(f"Successfully sent RPC 'AgentToClientUIAction' (id: {request_pb.request_id}) to '{client_identity}'.")

    except ValueError as e:
        logger.error(f"Failed to build UI action due to unknown type '{action_type}': {e}")
    except Exception as e:
        logger.error(f"Failed to send UI action RPC to '{client_identity}': {e}", exc_info=True)
