#!/bin/bash

# Brain API Standalone Test Script
# This script tests the LangGraph Brain API directly with curl

echo "🧠 Testing Brain API with curl..."
echo "=================================================="

# Check if the Brain server is running
echo "📡 Checking if Brain server is running on localhost:8080..."

if ! curl -s -f http://localhost:8080/health > /dev/null 2>&1; then
    echo "❌ Brain server is not responding on localhost:8080"
    echo "💡 Please start the LangGraph server first:"
    echo "   cd ../langgraph-service"
    echo "   uvicorn app:app --host 0.0.0.0 --port 8080"
    exit 1
fi

echo "✅ Brain server is responding!"
echo ""

# Send the test request
echo "🚀 Sending test request to Brain API..."
echo "Request payload:"
echo '{
    "task_name": "rox_conversation_turn",
    "json_payload": "{ \"current_context\": { \"user_id\": \"test-user-curl\", \"session_id\": \"test-session-curl-123\" }, \"transcript\": \"Hello\" }"
}'
echo ""

# Execute the curl command
curl -X POST "http://localhost:8080/invoke_task_streaming" \
-H "Content-Type: application/json" \
-d '{
    "task_name": "rox_conversation_turn",
    "json_payload": "{ \"current_context\": { \"user_id\": \"test-user-curl\", \"session_id\": \"test-session-curl-123\" }, \"transcript\": \"Hello\" }"
}' \
-v

echo ""
echo "=================================================="
echo "✅ If you see Server-Sent Events above ending with 'final_toolbelt', the Brain API is working correctly!"
echo "🔧 If you see errors, check the LangGraph server logs for details."
