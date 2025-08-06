# File: livekit-service/rox/langgraph_client.py
# rox/langgraph_client.py
"""
LangGraph client for communicating with the Brain service.
Handles task invocation and streaming response parsing.
"""

import logging
import json
import os
import aiohttp
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

class LangGraphClient:
    def __init__(self):
        # The URL should point to your new Student Tutor agent's base
        self.base_url = os.getenv("LANGGRAPH_TUTOR_URL", "http://localhost:8002")
        self.timeout = aiohttp.ClientTimeout(total=120.0)

    # --- SIGNATURE CHANGE ---
    async def invoke_langgraph_task(self, task: Dict, user_id: str, expert_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Invoke a task with LangGraph and return the final state.
        
        Args:
            task: The task dictionary containing task_name and other parameters
            user_id: The student identifier
            expert_id: The expert identifier for the current course
            session_id: The session identifier
            
        Returns:
            The final state dictionary from the graph, or None if failed.
        """
        logger.info(f"Invoking Brum-langgraph with task: {task.get('task_name')}")
        
        # --- PAYLOAD CHANGE: expert_id is now dynamic ---
        request_body = {
            "student_id": user_id,
            "expert_id": expert_id,  # No longer hardcoded
            "current_lo_id": task.get("current_lo_id", None),  # Pass along the current topic
            "student_input": task.get("transcript", "")
        }

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                # Route to appropriate endpoint based on task type
                if task.get("task_name") == "start_tutoring_session":
                    endpoint = "/start_session"
                    # For session start, we don't need student_input
                    request_body = {
                        "student_id": user_id,
                        "expert_id": expert_id,
                        "current_lo_id": task.get("current_lo_id", None)
                    }
                elif task.get("interaction_type") == "interruption":
                    endpoint = "/handle_interruption"
                else:
                    endpoint = "/handle_response"
                
                # --- ROBUSTNESS CHANGE: Construct URL safely ---
                full_url = f"{self.base_url}{endpoint}"

                async with session.post(full_url, json=request_body) as response:
                    response.raise_for_status()
                    
                    # This is where you would check for a streaming response vs. a JSON one.
                    # For now, we handle the JSON response correctly.
                    
                    response_data = await response.json()
                    
                    # --- BUG FIX: Return the whole response data ---
                    # The main agent loop will be responsible for parsing the delivery_plan and current_lo_id
                    if response_data:
                        logger.info(f"Received response from Brum-langgraph service: {response_data}")
                        return response_data
                    else:
                        logger.warning("No data received from Brum-langgraph service")
                        return None
                        
        except aiohttp.ClientError as e:
            logger.error(f"HTTP error communicating with LangGraph: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error invoking LangGraph task: {e}", exc_info=True)
            return None

    async def _parse_sse_stream(self, response: aiohttp.ClientResponse) -> Optional[List[Dict[str, Any]]]:
        """
        Parse Server-Sent Events stream from LangGraph to extract the final toolbelt.
        
        Args:
            response: The aiohttp response object
            
        Returns:
            The final toolbelt as a list of action dictionaries
        """
        final_toolbelt = None
        current_event = None
        
        # Read the response line by line
        async for line_bytes in response.content.iter_any():
            lines = line_bytes.decode('utf-8').split('\n')
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                    
                if line.startswith("event: "):
                    current_event = line[7:]  # Remove 'event: ' prefix
                    logger.debug(f"SSE event: {current_event}")
                elif line.startswith("data: "):
                    data = line[6:]  # Remove 'data: ' prefix
                    if data and data != '[DONE]' and current_event == 'final_toolbelt':
                        try:
                            final_toolbelt = json.loads(data)
                            logger.info(f"Found final_toolbelt in SSE stream: {len(final_toolbelt)} actions")
                            return final_toolbelt
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse SSE toolbelt data as JSON: {e}")
                            continue
        
        return final_toolbelt if final_toolbelt else []
