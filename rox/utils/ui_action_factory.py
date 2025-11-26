# File: livekit-service/rox/utils/ui_action_factory.py
# utils/ui_action_factory.py (Corrected and Completed)

import logging
import uuid
import json
from typing import Dict, Any

try:
    from ..generated.protos import interaction_pb2
except Exception:
    from generated.protos import interaction_pb2

logger = logging.getLogger(__name__)

def build_ui_action_request(action_type: str, parameters: Dict[str, Any]) -> interaction_pb2.AgentToClientUIActionRequest:
    """
    Builds a complete, typed AgentToClientUIActionRequest protobuf message
    from a simple action type string and a generic parameters dictionary.
    This is the central translator for the livekit-service.
    """
    try:
        action_type_enum = interaction_pb2.ClientUIActionType.Value(action_type)
    except ValueError:
        logger.error(f"Unknown UI action type string: '{action_type}'")
        raise

    request_pb = interaction_pb2.AgentToClientUIActionRequest(
        request_id=f"ui-action-{uuid.uuid4().hex[:8]}",
        action_type=action_type_enum
    )

    # --- This is the new, crucial translation logic ---
    # We check the action_type and populate the *specific* payload field.

    if action_type == "APPEND_TEXT_TO_EDITOR_REALTIME":
        payload = request_pb.append_text_to_editor_realtime_payload
        payload.text_chunk = str(parameters.get("text_chunk", ""))
    
    elif action_type == "UPDATE_TEXT_CONTENT":
        payload = request_pb.update_text_content_payload
        payload.text = str(parameters.get("text", ""))
        
    elif action_type == "HIGHLIGHT_TEXT_RANGES":
        ranges_data = parameters.get("ranges", [])
        for r_data in ranges_data:
            request_pb.highlight_ranges_payload.add(
                id=str(r_data.get("id", "")),
                start=int(r_data.get("start", 0)),
                end=int(r_data.get("end", 0)),
                type=str(r_data.get("type", "highlight"))
            )

    elif action_type == "DISPLAY_REMARKS_LIST":
        remarks_data = parameters.get("remarks", [])
        for r_data in remarks_data:
            request_pb.display_remarks_list_payload.remarks.add(
                id=str(r_data.get("id", "")),
                title=str(r_data.get("title", "")),
                content=str(r_data.get("content", ""))
            )
            
    elif action_type == "REPLACE_TEXT_RANGE":
        payload = request_pb.replace_text_range_payload
        payload.start = int(parameters.get("start_pos", 0))
        payload.end = int(parameters.get("end_pos", 0))
        payload.replacement = str(parameters.get("new_text", ""))

    elif action_type == "DISPLAY_VISUAL_AID":
        payload = request_pb.display_visual_aid_payload
        payload.commands_json = json.dumps(parameters.get("prompt", ""))
        if "canvas_id" in parameters:
            payload.canvas_id = str(parameters["canvas_id"])
        if "clear_previous" in parameters:
            payload.clear_previous = bool(parameters["clear_previous"])
            
    elif action_type == "HIGHLIGHT_ELEMENTS":
        # For highlight_elements -> HIGHLIGHT_TEXT_RANGES mapping
        element_ids = parameters.get("elementIds", [])
        for i, element_id in enumerate(element_ids):
            request_pb.highlight_ranges_payload.add(
                id=str(element_id),
                start=0,  # Default values for canvas highlighting
                end=0,
                type="highlight"
            )
    
    elif action_type == "SUGGESTED_RESPONSES":
        # Populate the aligned payload: suggestions (id,text,reason) and title
        payload = request_pb.suggested_responses_payload

        suggestions_param = parameters.get("suggestions")
        responses_param = parameters.get("responses")

        normalized_suggestions = []
        if isinstance(suggestions_param, list) and suggestions_param:
            # Rich suggestions provided
            for s in suggestions_param:
                if isinstance(s, dict):
                    ent = payload.suggestions.add()
                    ent.id = str(s.get("id", ""))
                    ent.text = str(s.get("text", ""))
                    if s.get("reason") is not None:
                        ent.reason = str(s.get("reason"))
                    normalized_suggestions.append({
                        "id": ent.id,
                        "text": ent.text,
                        "reason": getattr(ent, "reason", "")
                    })
                else:
                    ent = payload.suggestions.add()
                    ent.id = ""
                    ent.text = str(s)
                    normalized_suggestions.append({"id": "", "text": ent.text, "reason": ""})
        elif isinstance(responses_param, list) and responses_param:
            # Legacy responses list (array of strings)
            for resp in responses_param:
                ent = payload.suggestions.add()
                ent.id = ""
                ent.text = str(resp)
                normalized_suggestions.append({"id": "", "text": ent.text, "reason": ""})

        # Title handling (map prompt -> title for compatibility)
        title = parameters.get("title")
        prompt = parameters.get("prompt")
        if title is not None:
            payload.title = str(title)
        elif prompt is not None:
            payload.title = str(prompt)

        # Also populate generic parameters so frontends can parse without strict proto alignment
        try:
            if normalized_suggestions:
                request_pb.parameters["suggestions"] = json.dumps(normalized_suggestions)
                request_pb.parameters["responses"] = json.dumps([s["text"] for s in normalized_suggestions])
            if title is not None:
                request_pb.parameters["title"] = str(title)
            if prompt is not None and "prompt" not in request_pb.parameters:
                request_pb.parameters["prompt"] = str(prompt)
            # Preserve group_id for any correlation use in clients
            if parameters.get("group_id") is not None:
                request_pb.parameters["group_id"] = str(parameters.get("group_id"))
        except Exception:
            # Non-fatal: parameters map is best-effort
            logger.debug("Failed to populate generic parameters for SUGGESTED_RESPONSES", exc_info=True)
    
    else:
        # Fallback for simple actions that use the generic parameters map,
        # like NAVIGATE_TO_PAGE, SHOW_LOADING_INDICATOR, SET_UI_STATE, etc.
        logger.info(f"Using generic parameters map for action: {action_type}")
        for key, value in parameters.items():
            if isinstance(value, (dict, list)):
                request_pb.parameters[key] = json.dumps(value)
            else:
                request_pb.parameters[key] = str(value)

    return request_pb
