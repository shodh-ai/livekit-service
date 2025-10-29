import asyncio
import os
import sys
from dotenv import load_dotenv
import time
from pathlib import Path
from livekit import rtc

# Add parent dir to path so we can import rox
sys.path.append(str(Path(__file__).parent))
from rox.gemini_tts import GeminiNativeAudioTTS

async def test_gemini_tts():
    # Load env vars
    load_dotenv()
    
    # Get API key from env or prompt
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY not found in .env")
        return

    # Initialize TTS
    print("Initializing GeminiNativeAudioTTS...")
    tts = GeminiNativeAudioTTS(
        api_key=api_key,
        model=os.getenv("GEMINI_NATIVE_MODEL", "gemini-2.5-flash-native-audio-preview-09-2025"),
        sample_rate=int(os.getenv("GEMINI_SAMPLE_RATE", "24000")),
        system_instruction=os.getenv("GEMINI_SYSTEM_INSTRUCTION", "You are a helpful assistant. Speak clearly and professionally.")
    )
    
    # Test text
    test_text = "This is a test of the Gemini TTS system. If you can hear this, the TTS is working!"
    
    print(f"Synthesizing: {test_text}")
    
    # Synthesize in one shot and iterate frames
    try:
        t0_cold = time.perf_counter()
        stream = tts.synthesize(test_text)
        frame_count = 0
        total_ms = 0.0
        frames_oneshot: list[rtc.AudioFrame] = []
        first_frame_ms_cold = None
        async for ev in stream:
            frame = ev.frame
            frames_oneshot.append(frame)
            frame_count += 1
            total_ms += frame.duration * 1000.0
            if first_frame_ms_cold is None:
                first_frame_ms_cold = (time.perf_counter() - t0_cold) * 1000.0
            print(f"Frame #{frame_count}: {frame.samples_per_channel} samples @ {frame.sample_rate}Hz, duration={frame.duration*1000:.2f} ms, final={ev.is_final}")
        total_time_ms_cold = (time.perf_counter() - t0_cold) * 1000.0
        print(f"Synthesis completed: {frame_count} frames, ~{total_ms:.1f} ms of audio")
        if first_frame_ms_cold is not None:
            print(f"Cold synth: first_frame={first_frame_ms_cold:.0f} ms, total={total_time_ms_cold:.0f} ms")

        # Write one-shot WAV
        if frames_oneshot:
            combined_oneshot = rtc.combine_audio_frames(frames_oneshot)
            wav_bytes = combined_oneshot.to_wav_bytes()
            out_wav = Path(__file__).parent / "gemini_test.wav"
            out_wav.write_bytes(wav_bytes)
            print(f"Saved one-shot WAV to {out_wav} ({len(wav_bytes)} bytes)")

        # Warm synth (reuse persistent session)
        warm_text = "Warm session test. This should start quickly."
        t0_warm = time.perf_counter()
        warm_stream = tts.synthesize(warm_text)
        warm_first_ms = None
        warm_frames = 0
        async for ev in warm_stream:
            warm_frames += 1
            if warm_first_ms is None:
                warm_first_ms = (time.perf_counter() - t0_warm) * 1000.0
        warm_total_ms = (time.perf_counter() - t0_warm) * 1000.0
        print(f"Warm synth: first_frame={warm_first_ms:.0f} ms, total={warm_total_ms:.0f} ms, frames={warm_frames}")

        # Run a second test using streaming API
        s = tts.stream()
        t0_stream = time.perf_counter()
        s.push_text(test_text)
        s.flush()
        s.end_input()
        s_frame_count = 0
        frames_stream: list[rtc.AudioFrame] = []
        stream_first_ms = None
        async for ev in s:
            frames_stream.append(ev.frame)
            s_frame_count += 1
            if stream_first_ms is None:
                stream_first_ms = (time.perf_counter() - t0_stream) * 1000.0
        print(f"Streaming test produced {s_frame_count} frames; first_frame={stream_first_ms:.0f} ms")

        # Write streaming WAV
        if frames_stream:
            combined_stream = rtc.combine_audio_frames(frames_stream)
            wav_bytes = combined_stream.to_wav_bytes()
            out_wav_stream = Path(__file__).parent / "gemini_test_stream.wav"
            out_wav_stream.write_bytes(wav_bytes)
            print(f"Saved streaming WAV to {out_wav_stream} ({len(wav_bytes)} bytes)")
    except Exception as e:
        print(f"Error during TTS synthesize: {e}", file=sys.stderr)
        raise
    finally:
        try:
            await tts.aclose()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(test_gemini_tts())
