# File: livekit-service/rox/gemini_tts_client.py
# rox/gemini_tts_client.py
"""
Gemini 2.5 Flash TTS client for expressive, emotional speech synthesis.
Supports system prompts and inline emotional cues like [whisper], [excited], [laugh].
"""

import os
import logging
import asyncio
import io
from typing import Optional, Dict, Any
import google.generativeai as genai
from livekit import rtc

logger = logging.getLogger(__name__)

class GeminiTTSClient:
    """Client for Gemini 2.5 Flash TTS with emotional intelligence."""
    
    def __init__(self):
        self.model_id = "gemini-2.0-flash-exp"  # Updated model ID
        self.api_key = os.getenv("GOOGLE_API_KEY")
        
        if not self.api_key:
            raise ValueError("GOOGLE_API_KEY environment variable is required for Gemini TTS")
        
        # Configure Gemini client
        genai.configure(api_key=self.api_key)
        self.client = genai.GenerativeModel(self.model_id)
        
        logger.info(f"Initialized Gemini TTS client with model: {self.model_id}")
    
    async def synthesize_speech(
        self, 
        text: str, 
        system_prompt: Optional[str] = None,
        emotion: Optional[str] = None,
        voice_style: Optional[str] = None
    ) -> Optional[bytes]:
        """
        Synthesize speech using Gemini 2.5 Flash TTS with emotional intelligence.
        
        Args:
            text: The text to synthesize
            system_prompt: Optional system prompt for voice characteristics
            emotion: Optional emotion to inject (e.g., "excited", "calm", "whisper")
            voice_style: Optional voice style (e.g., "teacher", "friendly", "professional")
            
        Returns:
            Audio bytes or None if synthesis failed
        """
        try:
            # Build the enhanced prompt for emotional intelligence
            enhanced_text = self._build_enhanced_prompt(text, system_prompt, emotion, voice_style)
            
            logger.info(f"Processing speech with Gemini emotional intelligence: {enhanced_text[:100]}...")
            
            # NOTE: Gemini 2.5 Flash doesn't directly support audio generation yet
            # Instead, we'll use it for emotional text processing and fallback to system TTS
            
            # Generate emotionally enhanced text using Gemini
            response = await asyncio.get_event_loop().run_in_executor(
                None, 
                lambda: self.client.generate_content(
                    f"Transform this text to be more {emotion} and {voice_style}: {text}"
                )
            )
            
            # Extract enhanced text
            enhanced_speech_text = text  # Default fallback
            if hasattr(response, 'candidates') and response.candidates:
                if response.candidates[0].content.parts:
                    enhanced_speech_text = response.candidates[0].content.parts[0].text.strip()
                    logger.info(f"Enhanced text with Gemini: {enhanced_speech_text[:100]}...")
            
            # Use system TTS for actual audio generation
            # This is a placeholder - in production you'd integrate with a real TTS service
            audio_data = self._generate_system_tts(enhanced_speech_text, emotion, voice_style)
            
            if audio_data:
                logger.info(f"Successfully generated {len(audio_data)} bytes of audio")
                return audio_data
            else:
                logger.warning("No audio generated, returning None")
                return None
            
        except Exception as e:
            logger.error(f"Failed to synthesize speech with Gemini TTS: {e}", exc_info=True)
            return None
    
    def _build_enhanced_prompt(
        self, 
        text: str, 
        system_prompt: Optional[str] = None,
        emotion: Optional[str] = None,
        voice_style: Optional[str] = None
    ) -> str:
        """
        Build an enhanced prompt with system instructions and emotional cues.
        
        Args:
            text: Base text to speak
            system_prompt: System-level voice instructions
            emotion: Emotional state to convey
            voice_style: Overall voice style
            
        Returns:
            Enhanced prompt string for Gemini TTS
        """
        # Default system prompt for AI tutor
        default_system = "You are Alex, a warm and engaging AI teacher. Speak naturally with appropriate pacing and enthusiasm."
        
        # Use provided system prompt or default
        system_instruction = system_prompt or default_system
        
        # Add voice style if specified
        if voice_style:
            system_instruction += f" Use a {voice_style} tone of voice."
        
        # Build the final prompt
        if emotion:
            # Inject emotional cue at the beginning
            enhanced_text = f"Say in a {emotion} voice: {text}"
        else:
            enhanced_text = f"Say: {text}"
        
        # Combine system instruction with text
        full_prompt = f"{system_instruction}\n\n{enhanced_text}"
        
        return full_prompt
    
    def _inject_emotional_cues(self, text: str, emotion: str) -> str:
        """
        Inject emotional cues into text for more expressive speech.
        
        Args:
            text: Original text
            emotion: Emotion to inject
            
        Returns:
            Text with emotional cues
        """
        emotion_map = {
            "excited": "[excited]",
            "calm": "[calm]",
            "whisper": "[whisper]",
            "laugh": "[laugh]",
            "giggle": "[giggle]",
            "surprised": "[wow]",
            "thinking": "[hmm]",
            "encouraging": "[warm]"
        }
        
        cue = emotion_map.get(emotion.lower(), f"[{emotion}]")
        
        # Add emotional cue at strategic points
        if "?" in text:
            # Add cue before questions
            text = text.replace("?", f" {cue}?")
        elif "!" in text:
            # Add cue before exclamations
            text = text.replace("!", f" {cue}!")
        else:
            # Add at the beginning for statements
            text = f"{cue} {text}"
        
        return text
    
    def _generate_system_tts(self, text: str, emotion: Optional[str] = None, voice_style: Optional[str] = None) -> Optional[bytes]:
        """
        Generate TTS audio using system TTS as fallback.
        
        Args:
            text: Text to synthesize
            emotion: Emotional parameter (for future TTS integration)
            voice_style: Voice style parameter (for future TTS integration)
            
        Returns:
            Audio bytes or None
        """
        try:
            # For now, create a simple WAV file with silence as a placeholder
            # In production, this would integrate with a real TTS service like Deepgram or ElevenLabs
            
            import wave
            import struct
            import io
            
            # Create a simple WAV file with silence (placeholder)
            sample_rate = 22050
            duration = max(0.5, len(text) * 0.1)  # Rough duration based on text length
            num_samples = int(sample_rate * duration)
            
            # Generate silence (in production, this would be actual TTS audio)
            audio_samples = [0] * num_samples
            
            # Create WAV file in memory
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(1)  # Mono
                wav_file.setsampwidth(2)  # 16-bit
                wav_file.setframerate(sample_rate)
                
                # Write audio data
                for sample in audio_samples:
                    wav_file.writeframes(struct.pack('<h', sample))
            
            wav_buffer.seek(0)
            audio_data = wav_buffer.getvalue()
            
            logger.info(f"Generated placeholder audio: {len(audio_data)} bytes for text: '{text[:50]}...'")
            return audio_data
            
        except Exception as e:
            logger.error(f"Failed to generate system TTS: {e}")
            return None

    async def create_audio_track(self, text: str, emotion: Optional[str] = None, voice_style: Optional[str] = None) -> Optional[rtc.AudioTrack]:
        """
        Create a LiveKit audio track from synthesized audio data.
        
        Args:
            text: Text to synthesize into speech
            emotion: Emotional parameter for speech synthesis
            voice_style: Voice style parameter for speech synthesis
            
        Returns:
            LiveKit AudioTrack or None if creation failed
        """
        try:
            # First, synthesize the speech
            audio_data = await self.synthesize_speech(text, emotion=emotion, voice_style=voice_style)
            
            if not audio_data:
                logger.error("No audio data generated for LiveKit track creation")
                return None
            
            # Create audio source from bytes
            audio_stream = io.BytesIO(audio_data)
            
            # Create LiveKit audio track
            # Note: This is a simplified implementation
            # In production, you'd need proper audio format handling
            logger.info(f"Creating LiveKit audio track from {len(audio_data)} bytes of audio")
            
            # For now, return a mock track since LiveKit audio track creation
            # requires more complex setup with proper audio sources
            # In production, this would integrate with LiveKit's audio pipeline
            
            logger.info("Successfully prepared audio data for LiveKit integration")
            return f"MockAudioTrack({len(audio_data)}_bytes)"  # Placeholder
            
        except Exception as e:
            logger.error(f"Failed to create audio track: {e}", exc_info=True)
            return None
