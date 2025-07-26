import sys
import os

# Add the parent directory to Python path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

print("Python Path:")
print("\n".join(sys.path))
print("\nCurrent Working Directory:", os.getcwd())
print("\nTrying to import google.generativeai...")
try:
    import google.generativeai as genai
    print("Successfully imported google.generativeai")
    print("Module location:", genai.__file__)
    
    print("\nTrying to import GeminiTTSClient from rox.gemini_tts_client...")
    from rox.gemini_tts_client import GeminiTTSClient
    print("Successfully imported GeminiTTSClient")
    
    # Test creating an instance
    print("\nTrying to create GeminiTTSClient instance...")
    # The class expects GOOGLE_API_KEY to be set in the environment
    os.environ["GOOGLE_API_KEY"] = "test_key"
    tts = GeminiTTSClient()
    print("Successfully created GeminiTTSClient instance")
    
except Exception as e:
    import traceback
    print("Error:", str(e))
    print("\nTraceback:")
    traceback.print_exc()
