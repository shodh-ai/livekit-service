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
        self.url = os.getenv("LANGGRAPH_BRAIN_URL", "http://localhost:8080/invoke_task_streaming")
        self.timeout = aiohttp.ClientTimeout(total=120.0)

    async def invoke_langgraph_task(self, task: Dict, user_id: str, session_id: str) -> Optional[List[Dict[str, Any]]]:
        """
        Invoke a task with LangGraph and return the final toolbelt.
        
        Args:
            task: The task dictionary containing task_name and other parameters
            user_id: The user identifier
            session_id: The session identifier
            
        Returns:
            List of actions (toolbelt) to execute, or None if failed
        """
        logger.info(f"Invoking LangGraph with task: {task.get('task_name')}")
        
        # Construct the request body to match Brain API format
        json_payload = {
            "current_context": {
                "user_id": user_id,
                "session_id": session_id
            },
            "transcript": task.get("transcript", "Hello")  # Default greeting if no transcript
        }
        
        request_body = {
            "task_name": task.get("task_name", "rox_conversation_turn"),
            "json_payload": json.dumps(json_payload)
        }

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(self.url, json=request_body) as response:
                    response.raise_for_status()
                    
                    # Parse the SSE stream to find the final toolbelt
                    final_toolbelt = await self._parse_sse_stream(response)
                    
                    if final_toolbelt:
                        logger.info(f"Received toolbelt with {len(final_toolbelt)} actions")
                        return final_toolbelt
                    else:
                        logger.warning("No toolbelt received from LangGraph")
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
        
        async for line_bytes in response.content:
            line = line_bytes.decode().strip()
            
            if line.startswith("event: "):
                current_event = line[7:]  # Remove 'event: ' prefix
            elif line.startswith("data: "):
                data = line[6:]  # Remove 'data: ' prefix
                if data and data != '[DONE]' and current_event == 'final_toolbelt':
                    try:
                        final_toolbelt = json.loads(data)
                        logger.debug("Found final_toolbelt in SSE stream")
                        break
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse SSE toolbelt data as JSON: {e}")
                        continue
        
        return final_toolbelt if final_toolbelt else []
