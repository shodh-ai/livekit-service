#!/usr/bin/env python3

"""
Test script to verify Gemini 2.5 Flash TTS integration in LiveKit
"""

import asyncio
import sys
import os
import tempfile
import wave
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Add the rox directory to the path
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rox'))

from rox.gemini_tts_client import GeminiTTSClient

async def test_gemini_tts_basic():
    """Test basic Gemini TTS functionality"""
    
    print("🎙️ Testing Gemini 2.5 Flash TTS - Basic Functionality...")
    
    try:
        # Initialize Gemini TTS client
        tts_client = GeminiTTSClient()
        
        # Test basic speech synthesis
        text = "Hello! This is a test of the Gemini TTS integration."
        
        print(f"📝 Synthesizing: '{text}'")
        
        # Generate audio
        audio_data = await tts_client.synthesize_speech(text)
        
        if audio_data:
            print(f"✅ SUCCESS: Generated {len(audio_data)} bytes of audio data")
            
            # Save to temporary file for verification
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_file:
                temp_file.write(audio_data)
                temp_path = temp_file.name
            
            print(f"💾 Audio saved to: {temp_path}")
            
            # Verify it's valid WAV data
            try:
                with wave.open(temp_path, 'rb') as wav_file:
                    frames = wav_file.getnframes()
                    sample_rate = wav_file.getframerate()
                    duration = frames / sample_rate
                    print(f"🎵 Audio info: {duration:.2f}s, {sample_rate}Hz, {frames} frames")
                
                # Clean up
                os.unlink(temp_path)
                return True
                
            except Exception as e:
                print(f"❌ Invalid audio format: {e}")
                os.unlink(temp_path)
                return False
                
        else:
            print("❌ FAILED: No audio data generated")
            return False
            
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

async def test_gemini_tts_emotional():
    """Test Gemini TTS with emotional parameters"""
    
    print("\n🎭 Testing Gemini 2.5 Flash TTS - Emotional Intelligence...")
    
    try:
        tts_client = GeminiTTSClient()
        
        # Test different emotional styles
        test_cases = [
            {
                "text": "Great job! You're doing amazing work!",
                "emotion": "excited",
                "voice_style": "teacher",
                "description": "Excited teacher praise"
            },
            {
                "text": "Let me explain this concept step by step.",
                "emotion": "calm",
                "voice_style": "friendly",
                "description": "Calm explanation"
            },
            {
                "text": "I understand this might be confusing. Let's work through it together.",
                "emotion": "encouraging",
                "voice_style": "teacher",
                "description": "Encouraging support"
            }
        ]
        
        success_count = 0
        
        for i, test_case in enumerate(test_cases, 1):
            print(f"\n🎯 Test {i}: {test_case['description']}")
            print(f"   Text: '{test_case['text']}'")
            print(f"   Emotion: {test_case['emotion']}, Style: {test_case['voice_style']}")
            
            try:
                audio_data = await tts_client.synthesize_speech(
                    text=test_case['text'],
                    emotion=test_case['emotion'],
                    voice_style=test_case['voice_style']
                )
                
                if audio_data:
                    print(f"   ✅ Generated {len(audio_data)} bytes of emotional audio")
                    success_count += 1
                else:
                    print("   ❌ Failed to generate audio")
                    
            except Exception as e:
                print(f"   ❌ Error: {e}")
        
        print(f"\n📊 Emotional TTS Results: {success_count}/{len(test_cases)} tests passed")
        return success_count == len(test_cases)
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

async def test_gemini_tts_livekit_integration():
    """Test Gemini TTS integration with LiveKit audio track creation"""
    
    print("\n🎵 Testing Gemini TTS - LiveKit Integration...")
    
    try:
        tts_client = GeminiTTSClient()
        
        # Test LiveKit audio track creation
        text = "This is a test of the LiveKit audio track integration with Gemini TTS."
        
        print(f"📝 Creating LiveKit audio track for: '{text}'")
        
        # Generate LiveKit audio track
        audio_track = await tts_client.create_audio_track(
            text=text,
            emotion="calm",
            voice_style="teacher"
        )
        
        if audio_track:
            print("✅ SUCCESS: Created LiveKit audio track")
            print(f"   Track info: {type(audio_track).__name__}")
            
            # Test track properties
            if hasattr(audio_track, 'kind'):
                print(f"   Track kind: {audio_track.kind}")
            
            return True
        else:
            print("❌ FAILED: Could not create LiveKit audio track")
            return False
            
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

async def test_environment_setup():
    """Test that required environment variables are set"""
    
    print("🔧 Testing Environment Setup...")
    
    required_vars = ['GOOGLE_API_KEY']
    missing_vars = []
    
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
        else:
            print(f"✅ {var}: Set")
    
    if missing_vars:
        print(f"❌ Missing environment variables: {', '.join(missing_vars)}")
        print("💡 Please set these variables before running Gemini TTS tests")
        return False
    
    return True

async def main():
    """Run all Gemini TTS tests"""
    
    print("🚀 Gemini 2.5 Flash TTS Integration Test Suite")
    print("=" * 60)
    
    # Test environment setup first
    if not await test_environment_setup():
        print("\n❌ Environment setup failed. Please configure required variables.")
        return
    
    # Run all tests
    tests = [
        ("Basic TTS", test_gemini_tts_basic),
        ("Emotional TTS", test_gemini_tts_emotional),
        ("LiveKit Integration", test_gemini_tts_livekit_integration)
    ]
    
    results = []
    
    for test_name, test_func in tests:
        print(f"\n{'=' * 20} {test_name} {'=' * 20}")
        try:
            success = await test_func()
            results.append((test_name, success))
        except Exception as e:
            print(f"❌ Test '{test_name}' crashed: {e}")
            results.append((test_name, False))
    
    # Summary
    print("\n" + "=" * 60)
    print("🎯 TEST SUMMARY")
    print("=" * 60)
    
    passed = 0
    for test_name, success in results:
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status} - {test_name}")
        if success:
            passed += 1
    
    print(f"\n📊 Overall: {passed}/{len(results)} tests passed")
    
    if passed == len(results):
        print("🎉 ALL GEMINI TTS TESTS PASSED! Your integration is working perfectly!")
    else:
        print("⚠️  Some tests failed. Check the output above for details.")

if __name__ == "__main__":
    asyncio.run(main())
