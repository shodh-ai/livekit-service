#!/usr/bin/env python3
"""
Conductor-Brain Integration Test Script

This script tests the HTTP communication between the LiveKit Conductor (livekit-service)
and the LangGraph Brain (langgraph-service) without requiring LiveKit server or frontend.

Prerequisites:
1. Start the LangGraph Brain server: uvicorn app:app --host 0.0.0.0 --port 8080
2. Ensure .env file is configured with proper environment variables

Usage:
    python test_conductor_brain_link.py
"""

import asyncio
import logging
import json
import os
import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Import the Conductor components
from rox.main import RoxAgent
from rox.langgraph_client import LangGraphClient
from rox.frontend_client import FrontendClient

# Configure logging to see detailed output
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class MockRoom:
    """Mock LiveKit room for testing"""
    def __init__(self):
        self.local_participant = MockParticipant()

class MockParticipant:
    """Mock LiveKit participant for testing"""
    async def perform_rpc(self, method, payload, participant_identity):
        logger.info(f"[MOCK RPC] Would send '{method}' to '{participant_identity}' with payload length: {len(payload)}")
        return "mock_rpc_response_success"

class MockAgentSession:
    """Mock LiveKit agent session for testing"""
    async def say(self, text, allow_interruptions=True):
        logger.info(f"[MOCK TTS] Would speak: '{text}' (interruptions: {allow_interruptions})")
    
    def interrupt(self):
        logger.info("[MOCK TTS] Would interrupt current speech")

async def test_brain_standalone():
    """
    Step 1: Test the Brain API directly with HTTP request
    This validates that the LangGraph service is working correctly.
    """
    logger.info("=== STEP 1: Testing Brain API Standalone ===")
    
    try:
        import aiohttp
        
        # Prepare the request payload (same as curl command)
        payload = {
            "task_name": "rox_conversation_turn",
            "json_payload": json.dumps({
                "current_context": {
                    "user_id": "test-user-curl",
                    "session_id": "test-session-curl-123"
                },
                "transcript": "Hello"
            })
        }
        
        logger.info("Sending HTTP request to Brain API...")
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://localhost:8080/invoke_task_streaming",
                json=payload,
                headers={"Content-Type": "application/json"}
            ) as response:
                
                if response.status == 200:
                    logger.info("✅ Brain API responded successfully!")
                    
                    # Read the SSE stream
                    final_toolbelt = None
                    current_event = None
                    async for line in response.content:
                        line_str = line.decode('utf-8').strip()
                        if line_str.startswith('event: '):
                            current_event = line_str[7:]  # Remove 'event: ' prefix
                        elif line_str.startswith('data: '):
                            data = line_str[6:]  # Remove 'data: ' prefix
                            if data and data != '[DONE]' and current_event == 'final_toolbelt':
                                try:
                                    final_toolbelt = json.loads(data)
                                    logger.info("📋 Received final_toolbelt from Brain!")
                                    break
                                except json.JSONDecodeError:
                                    continue
                    
                    if final_toolbelt:
                        logger.info("✅ Step 1 PASSED: Brain API is working correctly")
                        logger.info(f"Sample toolbelt: {json.dumps(final_toolbelt[:1] if final_toolbelt else [], indent=2)}")
                        return True
                    else:
                        logger.error("❌ Step 1 FAILED: No final_toolbelt received")
                        return False
                else:
                    logger.error(f"❌ Step 1 FAILED: Brain API returned status {response.status}")
                    return False
                    
    except Exception as e:
        logger.error(f"❌ Step 1 FAILED: Error connecting to Brain API: {e}")
        logger.info("💡 Make sure the LangGraph server is running: uvicorn app:app --host 0.0.0.0 --port 8080")
        return False

async def test_conductor_brain_integration():
    """
    Step 2: Test the Conductor's ability to communicate with the Brain
    This validates the LangGraphClient and Conductor integration.
    """
    logger.info("\n=== STEP 2: Testing Conductor-Brain Integration ===")
    
    try:
        # 1. Create a Conductor instance
        conductor = RoxAgent()
        
        # 2. Set up mock dependencies
        conductor._room = MockRoom()
        conductor.agent_session = MockAgentSession()
        conductor.caller_identity = "test-frontend-client"
        conductor.user_id = "test-user-conductor"
        conductor.session_id = "test-session-conductor-456"
        
        logger.info("🤖 Conductor initialized with mock dependencies")
        
        # 3. Test the LangGraph client directly
        logger.info("📡 Testing LangGraph client communication...")
        
        task = {
            "task_name": "rox_conversation_turn",
            "current_context": {
                "user_id": conductor.user_id,
                "session_id": conductor.session_id
            },
            "transcript": "Hello from Conductor test"
        }
        
        # Call the LangGraph client
        toolbelt = await conductor._langgraph_client.invoke_langgraph_task(
            task=task,
            user_id=conductor.user_id,
            session_id=conductor.session_id
        )
        
        if toolbelt and len(toolbelt) > 0:
            logger.info("✅ Conductor successfully received toolbelt from Brain!")
            logger.info(f"📋 Toolbelt contains {len(toolbelt)} actions:")
            
            for i, action in enumerate(toolbelt[:3]):  # Show first 3 actions
                logger.info(f"  {i+1}. {action.get('tool_name', 'unknown')} - {action.get('parameters', {})}")
            
            # 4. Test the Unified Action Executor
            logger.info("\n🎯 Testing Unified Action Executor...")
            await conductor._execute_toolbelt(toolbelt)
            
            logger.info("✅ Step 2 PASSED: Conductor-Brain integration working correctly!")
            return True
            
        else:
            logger.error("❌ Step 2 FAILED: Conductor received empty or invalid toolbelt")
            return False
            
    except Exception as e:
        logger.error(f"❌ Step 2 FAILED: Error in Conductor-Brain integration: {e}")
        logger.exception("Full error details:")
        return False

async def main():
    """
    Main test orchestrator - runs both integration test steps
    """
    logger.info("🚀 Starting Conductor-Brain Integration Test Suite")
    logger.info("=" * 60)
    
    # Load environment variables
    from dotenv import load_dotenv
    load_dotenv()
    
    # Step 1: Test Brain API standalone
    brain_test_passed = await test_brain_standalone()
    
    if not brain_test_passed:
        logger.error("❌ Brain API test failed. Cannot proceed to Conductor integration test.")
        logger.info("💡 Please ensure the LangGraph server is running and accessible.")
        return False
    
    # Step 2: Test Conductor-Brain integration
    integration_test_passed = await test_conductor_brain_integration()
    
    # Final results
    logger.info("\n" + "=" * 60)
    logger.info("🏁 INTEGRATION TEST RESULTS")
    logger.info("=" * 60)
    
    if brain_test_passed and integration_test_passed:
        logger.info("🎉 ALL TESTS PASSED!")
        logger.info("✅ Brain API is working correctly")
        logger.info("✅ Conductor-Brain HTTP communication is working")
        logger.info("✅ Unified Action Executor is processing toolbelts")
        logger.info("\n🚀 Your system is ready for LiveKit integration!")
        return True
    else:
        logger.error("❌ SOME TESTS FAILED")
        logger.info("🔧 Please fix the issues above before proceeding")
        return False

if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
