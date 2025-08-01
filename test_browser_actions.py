#!/usr/bin/env python3
"""
Test script to verify browser action protobuf integration works correctly.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from rox.generated import interaction_pb2
from rox.utils.ui_action_factory import build_ui_action_request

def test_browser_navigate():
    """Test that BROWSER_NAVIGATE action can be created successfully."""
    try:
        # Test that the enum value exists
        action_type = "BROWSER_NAVIGATE"
        action_type_enum = interaction_pb2.ClientUIActionType.Value(action_type)
        print(f"✅ {action_type} enum value: {action_type_enum}")
        
        # Test that we can build a UI action request
        parameters = {"url": "https://jupyter.org/try-jupyter/lab/"}
        ui_action_request = build_ui_action_request(action_type, parameters)
        print(f"✅ Successfully created UI action request for {action_type}")
        print(f"   Action type enum: {ui_action_request.action_type}")
        print(f"   Parameters: {dict(ui_action_request.parameters)}")
        
        return True
    except Exception as e:
        print(f"❌ Error testing {action_type}: {e}")
        return False

def test_all_browser_actions():
    """Test all browser-related actions."""
    browser_actions = [
        ("BROWSER_NAVIGATE", {"url": "https://jupyter.org/try-jupyter/lab/"}),
        ("BROWSER_CLICK", {"selector": "#run-button"}),
        ("BROWSER_TYPE", {"text": "print('Hello World')", "selector": ".CodeMirror"}),
        ("UPLOAD_FILE_TO_JUPYTER", {"file_path": "/tmp/data.csv", "target_path": "data.csv"})
    ]
    
    all_passed = True
    for action_type, parameters in browser_actions:
        try:
            action_type_enum = interaction_pb2.ClientUIActionType.Value(action_type)
            ui_action_request = build_ui_action_request(action_type, parameters)
            print(f"✅ {action_type} (enum {action_type_enum}) - OK")
        except Exception as e:
            print(f"❌ {action_type} - FAILED: {e}")
            all_passed = False
    
    return all_passed

if __name__ == "__main__":
    print("Testing browser action protobuf integration...")
    print("=" * 50)
    
    # Test individual browser navigate
    test_browser_navigate()
    print()
    
    # Test all browser actions
    print("Testing all browser actions:")
    all_passed = test_all_browser_actions()
    
    print("\n" + "=" * 50)
    if all_passed:
        print("🎉 All browser action tests PASSED!")
        print("The protobuf integration is working correctly.")
    else:
        print("💥 Some tests FAILED!")
        print("There are still issues with the protobuf integration.")
