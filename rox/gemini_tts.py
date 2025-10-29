from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import os
from typing import Optional

from livekit.agents import tts, utils
from livekit.agents.types import APIConnectOptions, DEFAULT_API_CONNECT_OPTIONS
from livekit import rtc

try:
    # Lazy import; only required if provider is enabled
    from google import genai
    from google.genai import types as genai_types
except Exception:  # pragma: no cover
    genai = None
    genai_types = None

NUM_CHANNELS = 1


@dataclass
class _Opts:
    api_key: Optional[str]
    model: str
    sample_rate: int
    system_instruction: Optional[str]


class GeminiNativeAudioTTS(tts.TTS):
    def __init__(
        self,
        *,
        api_key: Optional[str],
        model: str = "gemini-2.5-flash-native-audio-preview-09-2025",
        sample_rate: int = 24000,
        system_instruction: Optional[str] = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=NUM_CHANNELS,
        )
        if genai is None:
            raise RuntimeError(
                "google-genai is required for GeminiNativeAudioTTS; please install google-genai"
            )
        self._opts = _Opts(
            api_key=api_key,
            model=model,
            sample_rate=sample_rate,
            system_instruction=system_instruction,
        )
        # Persistent session state
        self._client = genai.Client(api_key=self._opts.api_key)  # type: ignore[name-defined]
        self._session = None
        self._session_cm = None
        self._session_lock = asyncio.Lock()
        self._connect_config = self._build_connect_config()
        # Optional: reset session for each one-shot speak to enforce verbatim behavior
        self._reset_each_speak = str(os.getenv("GEMINI_TTS_RESET_EACH_SPEAK", "0")).lower() in ("1", "true", "yes")

    def synthesize(self, text: str, *, conn_options: APIConnectOptions | None = None) -> "_GeminiChunkedStream":
        return _GeminiChunkedStream(
            tts=self,
            input_text=text,
            opts=self._opts,
            conn_options=conn_options or DEFAULT_API_CONNECT_OPTIONS,
        )

    def stream(self, *, conn_options: APIConnectOptions | None = None) -> "_GeminiSynthesizeStream":
        return _GeminiSynthesizeStream(
            tts=self,
            opts=self._opts,
            conn_options=conn_options or DEFAULT_API_CONNECT_OPTIONS,
        )

    async def aclose(self) -> None:
        await super().aclose()
        await self._reset_session()

    def _build_connect_config(self) -> dict:
        config: dict = {
            "response_modalities": ["AUDIO"],
            "thinking_config": {
                "include_thoughts": False,
            },
        }
        if self._opts.system_instruction:
            config["system_instruction"] = {
                "role": "user",
                "parts": [{"text": self._opts.system_instruction}],
            }
        return config

    async def _get_or_create_session(self):
        if self._session is None:
            # Create and retain the context manager to be able to close later
            self._session_cm = self._client.aio.live.connect(
                model=self._opts.model, config=self._connect_config
            )
            self._session = await self._session_cm.__aenter__()
        return self._session

    async def _reset_session(self) -> None:
        cm = self._session_cm
        self._session = None
        self._session_cm = None
        if cm is not None:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)

    @contextlib.asynccontextmanager
    async def _acquire_session(self):
        async with self._session_lock:
            try:
                session = await self._get_or_create_session()
            except Exception:
                # ensure clean state on failure
                await self._reset_session()
                raise
            try:
                yield session
            except Exception:
                # mark session invalid so next call reconnects
                await self._reset_session()
                raise


class _GeminiChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts: GeminiNativeAudioTTS,
        input_text: str,
        opts: _Opts,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._opts = opts

    async def _run(self) -> None:
        request_id = utils.shortuuid()
        audio_bstream = utils.audio.AudioByteStream(
            sample_rate=self._tts.sample_rate,
            num_channels=NUM_CHANNELS,
        )
        if self._tts._reset_each_speak:
            await self._tts._reset_session()
        async with self._tts._acquire_session() as session:
            # Send one full turn with the input text
            await session.send(input=self._input_text, end_of_turn=True)

            emitter = tts.SynthesizedAudioEmitter(
                event_ch=self._event_ch,
                request_id=request_id,
                segment_id=utils.shortuuid(),
            )

            async for resp in session.receive():
                try:
                    data = getattr(resp, "data", None)
                    if data:
                        for frame in audio_bstream.write(data):
                            emitter.push(frame)
                    server_content = getattr(resp, "server_content", None)
                    turn_complete = bool(getattr(server_content, "turn_complete", False)) if server_content else False
                    if turn_complete:
                        for frame in audio_bstream.flush():
                            emitter.push(frame)
                        emitter.flush()
                        break
                except Exception:
                    # In case of schema differences, attempt best-effort drain
                    pass


class _GeminiSynthesizeStream(tts.SynthesizeStream):
    def __init__(
        self,
        *,
        tts: GeminiNativeAudioTTS,
        opts: _Opts,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._opts = opts

    async def _run(self) -> None:
        request_id = utils.shortuuid()
        segment_id = utils.shortuuid()

        audio_bstream = utils.audio.AudioByteStream(
            sample_rate=self._tts.sample_rate,
            num_channels=NUM_CHANNELS,
        )

        # Queue of pending segment emitters created per flush
        emitters: asyncio.Queue[tts.SynthesizedAudioEmitter] = asyncio.Queue()

        if self._tts._reset_each_speak:
            await self._tts._reset_session()
        async with self._tts._acquire_session() as session:
            # Task: forward input tokens and flushes as text turns
            async def _forward_input():
                nonlocal segment_id
                buffer: list[str] = []
                async for data in self._input_ch:
                    if isinstance(data, str):
                        if not self._started_time:
                            self._mark_started()
                        buffer.append(data)
                    elif isinstance(data, self._FlushSentinel):
                        if buffer:
                            text = "".join(buffer)
                            buffer = []
                            # Create a new emitter for this segment
                            seg_emitter = tts.SynthesizedAudioEmitter(
                                event_ch=self._event_ch,
                                request_id=request_id,
                                segment_id=segment_id,
                            )
                            await emitters.put(seg_emitter)
                            await session.send(input=text, end_of_turn=True)
                            segment_id = utils.shortuuid()
                # End of input; nothing further

            # Task: receive audio and write frames to current emitter
            async def _recv_audio():
                current_emitter: tts.SynthesizedAudioEmitter | None = None
                pre_frames: list[rtc.AudioFrame] = []
                async for resp in session.receive():
                    data = getattr(resp, "data", None)
                    if data:
                        frames = list(audio_bstream.write(data))
                        if current_emitter is None:
                            if not emitters.empty():
                                current_emitter = await emitters.get()
                                # push any buffered frames first
                                for pf in pre_frames:
                                    current_emitter.push(pf)
                                pre_frames.clear()
                            else:
                                pre_frames.extend(frames)
                                continue
                        for frame in frames:
                            current_emitter.push(frame)
                        continue
                    server_content = getattr(resp, "server_content", None)
                    turn_complete = bool(getattr(server_content, "turn_complete", False)) if server_content else False
                    if turn_complete:
                        # Drain remaining frames and flush
                        frames = list(audio_bstream.flush())
                        if current_emitter is None and not emitters.empty():
                            current_emitter = await emitters.get()
                        if current_emitter is not None:
                            for pf in pre_frames:
                                current_emitter.push(pf)
                            pre_frames.clear()
                            for frame in frames:
                                current_emitter.push(frame)
                            current_emitter.flush()
                            current_emitter = None
                        else:
                            pre_frames.extend(frames)

            recv_task = asyncio.create_task(_recv_audio())
            fwd_task = asyncio.create_task(_forward_input())
            try:
                await asyncio.gather(fwd_task, recv_task)
            finally:
                with contextlib.suppress(Exception):
                    await utils.aio.cancel_and_wait(recv_task)
