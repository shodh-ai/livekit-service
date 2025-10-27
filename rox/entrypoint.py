# rox/entrypoint.py
import asyncio
import json
import logging
from typing import Any, Dict, Optional

from opentelemetry import trace as otel_trace
from opentelemetry import context as otel_context
from livekit import rtc, agents
from livekit.plugins import deepgram

from .agent import RoxAgent
from .rpc_services import AgentInteractionService
from .config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def _populate_agent_state_from_metadata(agent: RoxAgent, ctx: agents.JobContext):
    metadata: Dict[str, Any] = {}
    try:
        env_meta_str = settings.STUDENT_TOKEN_METADATA
        if env_meta_str:
            try:
                metadata = json.loads(env_meta_str)
            except Exception:
                logger.warning("Failed to parse STUDENT_TOKEN_METADATA; ignoring env override")
        if not metadata:
            try:
                room_meta = getattr(ctx.room, "metadata", None)
                if asyncio.iscoroutine(room_meta):
                    room_meta = await room_meta
                if room_meta:
                    metadata = json.loads(room_meta)
            except Exception:
                pass
        if not metadata:
            try:
                rp = getattr(ctx.room, "remote_participants", {}) or {}
                for pid, participant in list(rp.items()):
                    ident = str(getattr(participant, "identity", "") or pid)
                    if ident.startswith("browser-bot-"):
                        continue
                    pmeta = getattr(participant, "metadata", None)
                    if asyncio.iscoroutine(pmeta):
                        pmeta = await pmeta
                    if pmeta:
                        metadata = json.loads(pmeta)
                        break
            except Exception:
                pass
        if not metadata:
            try:
                local_participant = ctx.room.local_participant
                lp_meta = getattr(local_participant, "metadata", None)
                if asyncio.iscoroutine(lp_meta):
                    lp_meta = await lp_meta
                if lp_meta:
                    metadata = json.loads(lp_meta)
            except Exception:
                pass
        agent.user_id = metadata.get("user_id")
        agent.curriculum_id = metadata.get("curriculum_id")
        agent.current_lo_id = metadata.get("current_lo_id")
    except Exception as e:
        logger.warning(f"Could not populate metadata; proceeding with defaults: {e}")
    logger.info("Agent state populated from metadata.")


async def _setup_room_lifecycle_events(agent: RoxAgent, ctx: agents.JobContext):
    # Track identities we've already sent agent_ready to, to avoid duplicates
    handshake_targets_sent: set[str] = set()
    # Data channel handler (frontend -> agent)
    @ctx.room.on("data_received")
    def _on_data_received(*ev_args):
        """
        LiveKit Python emits data_received with signature:
        (payload: bytes, participant: RemoteParticipant, kind: DataPacket.Kind, topic: str)
        Older examples sometimes used a DataPacket-like object. This handler accepts both.
        """
        def _coerce_args(args):
            payload = None
            participant = None
            kind = None
            topic = None
            try:
                if len(args) == 1:
                    obj = args[0]
                    # DataPacket-like object
                    payload = getattr(obj, "data", None)
                    participant = getattr(obj, "participant", None)
                    kind = getattr(obj, "kind", None)
                    topic = getattr(obj, "topic", None)
                    if isinstance(payload, memoryview):
                        payload = bytes(payload)
                elif len(args) >= 2:
                    payload = args[0]
                    participant = args[1]
                    kind = args[2] if len(args) > 2 else None
                    topic = args[3] if len(args) > 3 else None
                    if isinstance(payload, memoryview):
                        payload = bytes(payload)
                return payload, participant, kind, topic
            except Exception:
                return None, None, None, None

        payload_bytes, participant, kind, topic = _coerce_args(ev_args)

        # Log raw packet first for better debugging
        try:
            sender_for_log = getattr(participant, "identity", "unknown") if participant else "unknown"
            raw_payload_for_log = payload_bytes if isinstance(payload_bytes, (bytes, bytearray)) else (payload_bytes.tobytes() if isinstance(payload_bytes, memoryview) else b"")
            payload_str_preview = raw_payload_for_log.decode("utf-8", errors="ignore")[:250]
            logger.info(f"[DC][RECV_RAW] from={sender_for_log} size={len(raw_payload_for_log)} preview='{payload_str_preview}' topic={topic}")
        except Exception:
            logger.warning("[DC][RECV_RAW] Could not log raw incoming packet details.")

        # Proceed with parsing and handling
        try:
            raw = payload_bytes
            if isinstance(raw, memoryview):
                raw = bytes(raw)
            if not isinstance(raw, (bytes, bytearray)):
                logger.warning("[data_received] Unexpected payload type; dropping packet")
                return
            payload_str = raw.decode("utf-8", errors="replace")
            try:
                sender = getattr(participant, "identity", None) if participant else None
            except Exception:
                sender = None
            try:
                logger.info(f"[DC][recv] from={sender or 'unknown'} size={len(payload_str)} preview={payload_str[:120]}")
            except Exception:
                pass
            payload = json.loads(payload_str)
            if isinstance(payload, dict):
                p_type = str(payload.get("type") or "").strip()
                if p_type == "agent_task":
                    try:
                        task_name = str(payload.get("taskName") or "").strip()
                        task_payload = payload.get("payload") or {}
                        if not task_name:
                            logger.info("[DC][agent_task] missing taskName; ignoring packet")
                            return
                        caller_id = (getattr(participant, "identity", None) if participant else None) or agent.caller_identity
                        if "transcript" not in task_payload and isinstance(task_payload.get("text"), str):
                            task_payload["transcript"] = task_payload.get("text")
                        mapped_name = task_name
                        if task_name == "student_spoke_or_acted":
                            if getattr(agent, "_expected_user_input_type", "INTERRUPTION") == "SUBMISSION":
                                mapped_name = "handle_submission"
                            else:
                                mapped_name = "handle_interruption"
                                task_payload = {**task_payload, "interaction_type": "interruption"}
                        elif task_name == "student_stopped_listening":
                            mapped_name = "student_stopped_listening"
                            task_payload = {**task_payload, "interaction_type": "manual_stop_listening"}
                        q_task = {"task_name": mapped_name, "caller_identity": caller_id, **task_payload}
                        logger.info(f"[DC][agent_task] enqueue -> {q_task.get('task_name')} from={caller_id}")
                        asyncio.create_task(agent._processing_queue.put(q_task))
                        # Send an explicit ACK back to caller for diagnostics
                        try:
                            ack = {
                                "ack": True,
                                "action": "agent_task",
                                "ok": True,
                                "taskName": mapped_name,
                                "to": getattr(ctx.room.local_participant, "identity", None),
                                "from": caller_id,
                            }
                            data = json.dumps(ack).encode("utf-8")
                            await ctx.room.local_participant.publish_data(
                                data,
                                destination_identities=[caller_id] if caller_id else None,
                                reliable=False,
                            )
                            logger.info(f"[DC][ACK_SENT] to={caller_id} for={mapped_name}")
                        except Exception:
                            logger.debug("[DC][ACK_SEND] failed", exc_info=True)
                        return
                    except Exception:
                        # Promote to WARNING for visibility in production
                        logger.warning("[DC][agent_task] Enqueue failed, likely due to payload format issue.", exc_info=True)
                elif p_type == "agent_context":
                    try:
                        action = str(payload.get("action") or "").upper()
                        params = payload.get("parameters") or {}
                        if action in ("UPDATE_AGENT_CONTEXT", "UPDATE_CONTEXT"):
                            ctx_json = params.get("jsonContextPayload")
                            context_payload = None
                            try:
                                if isinstance(ctx_json, str) and ctx_json.strip():
                                    context_payload = json.loads(ctx_json)
                            except Exception:
                                context_payload = None
                            q_task: Dict[str, Any] = {"task_name": "update_agent_context", "caller_identity": agent.caller_identity}
                            if isinstance(context_payload, dict):
                                q_task["restored_feed_summary"] = context_payload.get("restored_feed_summary")
                                q_task["context_payload"] = context_payload
                            logger.info("[DC][agent_context] enqueue update_agent_context")
                            asyncio.create_task(agent._processing_queue.put(q_task))
                            return
                    except Exception:
                        logger.debug("[DC][agent_context] process failed", exc_info=True)
                if payload.get("source") == "rrweb" and "event" in payload:
                    agent.rrweb_events_buffer.append(payload["event"])  # type: ignore[arg-type]
                elif payload.get("source") in ("rrweb", "vscode") and "event_payload" in payload:
                    agent.live_events_buffer.append(payload)
        except Exception:
            # Promote to WARNING for visibility in production
            logger.warning("[data_received] Handler error, likely failed to decode JSON.", exc_info=True)

    # Transcription text stream: capture 'lk.transcription' and log/user-forward
    try:
        def _on_text_stream(reader, participant_identity: str):
            # Log attachment of text stream handler for diagnostics
            logger.info(f"[STT] Text stream handler ATTACHED for participant: {participant_identity}")
            async def _consume():
                try:
                    text = await reader.read_all()
                    if isinstance(text, str) and text.strip():
                        logger.info(f"[STT] {participant_identity}: {text}")
                        try:
                            if agent and agent._frontend_client and agent._room and agent.caller_identity:
                                envelope = {"type": "transcript", "text": text, "speaker": participant_identity or "student"}
                                await agent._frontend_client._send_data(agent._room, agent.caller_identity, envelope)
                        except Exception:
                            logger.debug("[STT] failed forwarding transcript to frontend", exc_info=True)
                except Exception:
                    logger.debug("[STT] read text stream error", exc_info=True)
            asyncio.create_task(_consume())

        ctx.room.register_text_stream_handler("lk.transcription", _on_text_stream)
    except Exception:
        # Promote to WARNING for visibility in production
        logger.warning("Failed to register lk.transcription handler", exc_info=True)

    # Additionally, listen for transcription_received events (structured segments)
    @ctx.room.on("transcription_received")
    def _on_transcription_received(segments, participant, publication):
        try:
            text_parts = []
            try:
                for s in segments or []:
                    t = getattr(s, "text", None)
                    if isinstance(t, str) and t.strip():
                        text_parts.append(t.strip())
            except Exception:
                pass
            if text_parts:
                pid = getattr(participant, "identity", None) if participant else None
                logger.info(f"[STT:segments] {pid or 'unknown'}: {' '.join(text_parts)}")
        except Exception:
            logger.debug("transcription_received handler error", exc_info=True)

    # Participant disconnect -> delayed shutdown
    student_disconnect_grace = float(settings.STUDENT_DISCONNECT_GRACE_SEC)
    student_disconnect_task: asyncio.Task | None = None

    async def _delayed_student_shutdown(identity: str):
        try:
            await asyncio.sleep(student_disconnect_grace)
            if identity and identity == agent.caller_identity:
                logger.warning(
                    f"Student '{identity}' did not reconnect within {student_disconnect_grace:.1f}s. Shutting down agent."
                )
                ctx.shutdown()
        except asyncio.CancelledError:
            logger.info("Student reconnect detected before grace timeout; not shutting down.")
        except Exception:
            logger.debug("error in delayed student shutdown task", exc_info=True)

    def _on_participant_disconnected(participant: rtc.RemoteParticipant):
        nonlocal student_disconnect_task
        try:
            if participant.identity == agent.caller_identity:
                if student_disconnect_task and not student_disconnect_task.done():
                    student_disconnect_task.cancel()
                student_disconnect_task = asyncio.create_task(_delayed_student_shutdown(participant.identity))
        except Exception:
            logger.debug("participant_disconnected handler error", exc_info=True)

    ctx.room.on("participant_disconnected", _on_participant_disconnected)

    # Startup timeout: shut down if no student joins
    shutdown_timeout_seconds = int(settings.ROX_NO_SHOW_TIMEOUT_SECONDS)

    async def shutdown_if_no_student():
        try:
            await asyncio.sleep(shutdown_timeout_seconds)
        except asyncio.CancelledError:
            return
        if not agent.caller_identity:
            logger.warning("No student joined within the timeout period. Shutting down agent.")
            ctx.shutdown()

    shutdown_task = asyncio.create_task(shutdown_if_no_student())

    def _on_participant_connected(participant: rtc.RemoteParticipant):
        # Cancel the no-show shutdown
        try:
            if not shutdown_task.done():
                shutdown_task.cancel()
        except Exception:
            logger.debug("no-show cancel handler error", exc_info=True)

        # Set caller identity to first non-browser participant and send handshake
        try:
            ident = str(getattr(participant, "identity", "") or "")
            logger.info(f"participant_connected: {ident}")
            if ident and not ident.startswith("browser-bot-"):
                agent.caller_identity = ident

                async def _send_handshake_and_maybe_start():
                    # Best-effort handshake to frontend to kick off its flows
                    try:
                        if ident in handshake_targets_sent:
                            logger.info(f"agent_ready already sent to {ident}; skipping duplicate")
                        else:
                            payload = json.dumps({
                                "type": "agent_ready",
                                "agent_identity": ctx.room.local_participant.identity,
                            })
                            data = payload.encode("utf-8")
                            # Targeted send to the connecting participant only
                            await ctx.room.local_participant.publish_data(
                                data,
                                destination_identities=[ident],
                            )
                            handshake_targets_sent.add(ident)
                            logger.info(f"agent_ready sent to {ident}")
                    except Exception:
                        logger.debug("participant_connected: handshake publish failed", exc_info=True)

                    # Fallback: if frontend doesn't RPC soon, auto-enqueue start
                    try:
                        await asyncio.sleep(2)
                        if not getattr(agent, "_session_started", False) and not getattr(agent, "_start_task_enqueued", False):
                            agent._start_task_enqueued = True
                            try:
                                import time
                                agent._start_task_enqueued_at = time.time()
                            except Exception:
                                pass
                            await agent._processing_queue.put({
                                "task_name": "start_tutoring_session",
                                "caller_identity": ident,
                            })
                            logger.info("[participant_connected] Fallback enqueued start_tutoring_session")
                    except Exception:
                        logger.debug("participant_connected: fallback start enqueue failed", exc_info=True)

                asyncio.create_task(_send_handshake_and_maybe_start())
        except Exception:
            logger.debug("participant_connected post-setup error", exc_info=True)

    ctx.room.on("participant_connected", _on_participant_connected)

    # Initial handshake: only target non-browser participants
    async def _initial_handshake():
        try:
            await asyncio.sleep(1)
            if len(ctx.room.remote_participants) > 0:
                try:
                    participants = list(ctx.room.remote_participants.values())
                except Exception:
                    participants = []
                ids = [str(getattr(p, "identity", "") or "") for p in participants if str(getattr(p, "identity", "") or "")]
                non_browser = [i for i in ids if not i.startswith("browser-bot-")]
                if non_browser:
                    selected_identity = non_browser[0]
                    agent.caller_identity = selected_identity
                    logger.info(f"Initial handshake: Found student '{selected_identity}', sending agent_ready.")
                    if selected_identity in handshake_targets_sent:
                        logger.info(f"Initial handshake skipped; already sent to {selected_identity}")
                    else:
                        handshake_payload = json.dumps({"type": "agent_ready", "agent_identity": ctx.room.local_participant.identity})
                        await ctx.room.local_participant.publish_data(handshake_payload.encode("utf-8"), destination_identities=[selected_identity])
                        handshake_targets_sent.add(selected_identity)
                else:
                    # No student present yet; wait for participant_connected handler to send handshake
                    pass
        except Exception:
            logger.debug("initial handshake failed", exc_info=True)

    asyncio.create_task(_initial_handshake())

    logger.info("Room lifecycle event handlers registered.")


async def _register_agent_rpc_handlers(agent: RoxAgent, ctx: agents.JobContext):
    agent_rpc_service = AgentInteractionService()
    try:
        setattr(agent_rpc_service, "agent", agent)
    except Exception:
        pass
    service_name = "rox.interaction.AgentInteraction"
    local_participant = ctx.room.local_participant
    try:
        local_participant.register_rpc_method(f"{service_name}/InvokeAgentTask", agent_rpc_service.InvokeAgentTask)
        local_participant.register_rpc_method(f"{service_name}/student_wants_to_interrupt", agent_rpc_service.student_wants_to_interrupt)
        local_participant.register_rpc_method(f"{service_name}/student_mic_button_interrupt", agent_rpc_service.student_mic_button_interrupt)
        local_participant.register_rpc_method(f"{service_name}/student_spoke_or_acted", agent_rpc_service.student_spoke_or_acted)
        local_participant.register_rpc_method(f"{service_name}/student_stopped_listening", agent_rpc_service.student_stopped_listening)
        local_participant.register_rpc_method(f"{service_name}/TestPing", agent_rpc_service.TestPing)
    except Exception as e:
        logger.error(f"Failed to register RPC handlers: {e}", exc_info=True)
        raise
    logger.info("Agent RPC handlers registered.")


async def entrypoint(ctx: agents.JobContext):
    # Ensure OTel is initialized in the worker subprocess (dev watcher may spawn a new proc)
    try:
        from .otel_setup import init_tracing as _init_tracing
        _init_tracing()
    except Exception:
        pass
    tracer = otel_trace.get_tracer(__name__)
    _root_span = tracer.start_span("livekit.agent.job")
    _root_token = otel_context.attach(otel_trace.set_span_in_context(_root_span))

    logger.info("Starting Rox Conductor entrypoint")

    # Pre-connect: register text stream/transcription handlers so we don't miss early headers
    try:
        def _on_text_stream_pre(reader, participant_identity: str):
            logger.info(f"[STT][PRE] Text stream handler ATTACHED for participant: {participant_identity}")
            async def _consume():
                try:
                    text = await reader.read_all()
                    if isinstance(text, str) and text.strip():
                        logger.info(f"[STT][PRE] {participant_identity}: {text}")
                except Exception:
                    logger.debug("[STT][PRE] read text stream error", exc_info=True)
            asyncio.create_task(_consume())

        ctx.room.register_text_stream_handler("lk.transcription", _on_text_stream_pre)
        logger.info("[STT][PRE] Registered lk.transcription text stream handler before connect")

        @ctx.room.on("transcription_received")
        def _on_transcription_received_pre(segments, participant, publication):
            try:
                pid = getattr(participant, "identity", None) if participant else None
                logger.info(f"[STT:segments][PRE] handler attached; participant={pid}")
            except Exception:
                pass
    except Exception:
        logger.warning("Failed to register pre-connect lk.transcription handler", exc_info=True)

    try:
        await ctx.connect()
    except Exception as e:
        logger.error(f"Failed to connect to LiveKit room: {e}", exc_info=True)
        try:
            otel_context.detach(_root_token)
            _root_span.end()
        except Exception:
            pass
        return

    # Log agent/room identity after successful connect for diagnostics
    try:
        lp = getattr(ctx.room, "local_participant", None)
        logger.info(f"Agent connected with identity: {getattr(lp, 'identity', None)} room={getattr(ctx.room, 'name', None)}")
        try:
            ids = list(getattr(ctx.room, 'remote_participants', {}).keys())
            logger.info(f"Remote participants at connect: {ids}")
        except Exception:
            pass
    except Exception:
        pass

    agent = RoxAgent()
    ctx.rox_agent = agent
    agent._room = ctx.room
    agent.session_id = ctx.room.name

    await _populate_agent_state_from_metadata(agent, ctx)
    await _setup_room_lifecycle_events(agent, ctx)
    await _register_agent_rpc_handlers(agent, ctx)

    # AgentSession with Deepgram TTS/STT and the LLM interceptor
    session_kwargs = {
        "stt": deepgram.STT(
            model="nova-2",
            language="en",
            api_key=settings.DEEPGRAM_API_KEY,
            interim_results=False,
            punctuate=True,
            smart_format=True,
        ),
        "llm": agent.llm_interceptor,
        "tts": deepgram.TTS(
            model="aura-2-helena-en",
            api_key=settings.DEEPGRAM_API_KEY,
            encoding="linear16",
            sample_rate=24000,
        ),
    }
    agent_session = agents.AgentSession(**session_kwargs)
    agent.agent_session = agent_session
    # Ensure AgentSession is running before issuing TTS/say calls
    try:
        # Pass both the agent and the LiveKit room explicitly
        await agent_session.start(agent=agent, room=ctx.room)
    except Exception as e:
        logger.error(f"Failed to start AgentSession: {e}", exc_info=True)

    # Note: the main processing loop is driven externally by RPC/data events enqueuing tasks.
    # Start the processing loop as a background task for parity with previous behavior.
    try:
        asyncio.create_task(agent.processing_loop())
    except Exception:
        logger.debug("failed to start processing loop task", exc_info=True)

    # Keep span in scope until function end
    try:
        pass
    finally:
        try:
            otel_context.detach(_root_token)
            _root_span.end()
        except Exception:
            pass
