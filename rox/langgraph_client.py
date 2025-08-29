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
    def __init__(self):
        # The URL should point to your new Student Tutor agent's base
        self.base_url = os.getenv("LANGGRAPH_TUTOR_URL", "https://kamikaze-765053940579.asia-south1.run.app")
        self.timeout = aiohttp.ClientTimeout(total=120.0)
        logger.info(f"LangGraphClient initialized with base_url: {self.base_url}") 

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
                    # Pass visual_context if present
                    if task.get("visual_context"):
                        request_body["visual_context"] = task.get("visual_context")
                else:
                    endpoint = "/handle_response"
                    # Add student_input for regular response
                    request_body["student_input"] = task.get("transcript", "")
                    # Pass visual_context if present
                    if task.get("visual_context"):
                        request_body["visual_context"] = task.get("visual_context")
                
                # --- ROBUSTNESS CHANGE: Construct URL safely ---
                full_url = f"{self.base_url}{endpoint}"

                # Ensure session_id is propagated to the Student Tutor service
                # so that conversational context is maintained across turns.
                if session_id:
                    request_body["session_id"] = session_id

                async with session.post(full_url, json=request_body) as response:
                    response.raise_for_status()
                    
                    response_data = await response.json()
                    
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
