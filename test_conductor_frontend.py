#!/usr/bin/env python3
"""
Test script to directly test Conductor → Frontend communication
without involving the LangGraph Brain.

This will help isolate whether the issue is in the RPC handlers,
frontend hooks, or the Brain output format.
"""

import asyncio
import json
import logging
from typing import List, Dict, Any

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def create_test_toolbelt() -> List[Dict[str, Any]]:
    """Create a test toolbelt with structured visualization elements."""
    return [
        {
            "tool_name": "set_ui_state",
            "parameters": {
                "status_text": "Alex is drawing a test visualization...",
                "is_agent_speaking": False
            }
        },
        {
            "tool_name": "generate_visualization",
            "parameters": {
                "elements": [
                    {
                        "type": "text",
                        "x": 300,
                        "y": 50,
                        "width": 400,
                        "height": 40,
                        "text": "TEST VISUALIZATION",
                        "fontSize": 24,
                        "strokeColor": "#1e1e1e",
                        "backgroundColor": "transparent"
                    },
                    {
                        "type": "rectangle",
                        "x": 100,
                        "y": 120,
                        "width": 150,
                        "height": 80,
                        "text": "Box 1",
                        "fontSize": 16,
                        "strokeColor": "#007cba",
                        "backgroundColor": "#e3f2fd"
                    },
                    {
                        "type": "arrow",
                        "x": 250,
                        "y": 160,
                        "width": 100,
                        "height": 0,
                        "strokeColor": "#333"
                    },
                    {
                        "type": "rectangle",
                        "x": 350,
                        "y": 120,
                        "width": 150,
                        "height": 80,
                        "text": "Box 2",
                        "fontSize": 16,
                        "strokeColor": "#d32f2f",
                        "backgroundColor": "#ffebee"
                    },
                    {
                        "type": "ellipse",
                        "x": 200,
                        "y": 220,
                        "width": 200,
                        "height": 100,
                        "text": "Ellipse",
                        "fontSize": 14,
                        "strokeColor": "#2e7d32",
                        "backgroundColor": "#e8f5e8"
                    }
                ]
            }
        },
        {
            "tool_name": "speak",
            "parameters": {
                "text": "I've created a test visualization with structured elements. Can you see the boxes, arrow, and ellipse on the canvas?"
            }
        },
        {
            "tool_name": "highlight_elements",
            "parameters": {
                "element_ids": ["element_1", "element_2"],
                "highlight_type": "attention",
                "duration_ms": 3000
            }
        },
        {
            "tool_name": "listen",
            "parameters": {
                "expected_intent": "CONFIRM_UNDERSTANDING"
            }
        }
    ]

async def test_conductor_execution():
    """Test the Conductor's toolbelt execution directly."""
    try:
        # Import the RoxAgent class
        from rox.main import RoxAgent
        
        # Create a RoxAgent instance
        agent = RoxAgent()
        
        # Create test toolbelt
        test_toolbelt = create_test_toolbelt()
        
        logger.info("=" * 50)
        logger.info("TESTING CONDUCTOR TOOLBELT EXECUTION")
        logger.info("=" * 50)
        logger.info(f"Test toolbelt has {len(test_toolbelt)} actions:")
        
        for i, action in enumerate(test_toolbelt):
            logger.info(f"  {i+1}. {action['tool_name']}")
            if action['tool_name'] == 'generate_visualization':
                elements = action['parameters'].get('elements', [])
                logger.info(f"     → {len(elements)} structured elements")
        
        logger.info("\nExecuting toolbelt...")
        
        # Execute the toolbelt (this will log what would happen)
        await agent._execute_toolbelt(test_toolbelt)
        
        logger.info("\nToolbelt execution complete!")
        logger.info("Check the logs above to see if:")
        logger.info("1. generate_visualization was called with structured elements")
        logger.info("2. Frontend client methods were invoked properly")
        logger.info("3. Any errors occurred during execution")
        
    except Exception as e:
        logger.error(f"Test failed with error: {e}", exc_info=True)

def create_curl_test_command():
    """Create a curl command to test the frontend RPC directly."""
    test_rpc_payload = {
        "tool_name": "generate_visualization",
        "parameters": {
            "elements": [
                {
                    "type": "text",
                    "x": 400,
                    "y": 100,
                    "width": 300,
                    "height": 50,
                    "text": "CURL TEST",
                    "fontSize": 28,
                    "strokeColor": "#ff0000"
                },
                {
                    "type": "rectangle",
                    "x": 200,
                    "y": 200,
                    "width": 200,
                    "height": 100,
                    "text": "Test Box",
                    "fontSize": 18,
                    "strokeColor": "#0000ff",
                    "backgroundColor": "#f0f0ff"
                }
            ]
        }
    }
    
    curl_command = f"""
# Test RPC call to frontend (replace ROOM_URL and IDENTITY as needed)
curl -X POST "http://localhost:3000/api/test-rpc" \\
  -H "Content-Type: application/json" \\
  -d '{json.dumps(test_rpc_payload, indent=2)}'
"""
    
    logger.info("=" * 50)
    logger.info("CURL TEST COMMAND")
    logger.info("=" * 50)
    logger.info("You can use this curl command to test the frontend RPC directly:")
    logger.info(curl_command)

if __name__ == "__main__":
    print("🧪 Conductor → Frontend Test Suite")
    print("=" * 50)
    
    print("\n1. Testing Conductor toolbelt execution...")
    asyncio.run(test_conductor_execution())
    
    print("\n2. Generating curl test command...")
    create_curl_test_command()
    
    print("\n✅ Test complete!")
    print("\nNext steps:")
    print("1. Check the logs above for any errors in toolbelt execution")
    print("2. Use the curl command to test frontend RPC directly")
    print("3. Check browser console for frontend hook execution")
