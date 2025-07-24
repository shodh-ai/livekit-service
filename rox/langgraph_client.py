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
        
        # Construct the full state payload
        payload = {
            "user_id": user_id,
            "session_id": session_id,
            **task
        }
        
        request_body = {"json_payload": json.dumps(payload)}

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
        event_data_lines = []
        final_toolbelt = None
        
        async for line_bytes in response.content:
            line = line_bytes.decode().strip()
            
            if line.startswith("data:"):
                event_data_lines.append(line[len("data:"):].strip())
            elif line == "":  # Empty line signals end of event
                if not event_data_lines:
                    continue
                    
                data_str = "\n".join(event_data_lines)
                event_data_lines.clear()
                
                try:
                    data_obj = json.loads(data_str)
                    
                    # Look for the final toolbelt in the event
                    if isinstance(data_obj, dict):
                        if "final_toolbelt" in data_obj:
                            final_toolbelt = data_obj["final_toolbelt"]
                            logger.debug("Found final_toolbelt in SSE event")
                        elif "toolbelt" in data_obj:
                            final_toolbelt = data_obj["toolbelt"]
                            logger.debug("Found toolbelt in SSE event")
                        elif data_obj.get("type") == "final" and "actions" in data_obj:
                            final_toolbelt = data_obj["actions"]
                            logger.debug("Found actions in final SSE event")
                            
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse SSE event as JSON: {e}")
                    continue
        
        return final_toolbelt if final_toolbelt else []
