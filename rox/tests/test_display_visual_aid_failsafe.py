# Tests for display_visual_aid failsafe in RoxAgent._execute_toolbelt
# Location: livekit-service/rox/tests/test_display_visual_aid_failsafe.py

import asyncio
import json
import sys
import types
from pathlib import Path

# Ensure 'rox' package is importable by adding livekit-service to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # .../livekit-service
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# --- Minimal stubs for external dependencies so we can import rox.main ---

# fastapi
fastapi_mod = types.ModuleType("fastapi")
class _FastAPI:
    def __init__(self, *a, **k):
        self._routes = {}
    def get(self, path: str, *a, **k):
        def _decorator(fn):
            self._routes[("GET", path)] = fn
            return fn
        return _decorator
    def post(self, path: str, *a, **k):
        def _decorator(fn):
            self._routes[("POST", path)] = fn
            return fn
        return _decorator
class _HTTPException(Exception):
    pass
fastapi_mod.FastAPI = _FastAPI
fastapi_mod.HTTPException = _HTTPException
sys.modules.setdefault("fastapi", fastapi_mod)

# dotenv
dotenv_mod = types.ModuleType("dotenv")
def _load_dotenv(*a, **k):
    return None
dotenv_mod.load_dotenv = _load_dotenv
sys.modules.setdefault("dotenv", dotenv_mod)

# pydantic (avoid installing during tests)
pydantic_mod = types.ModuleType("pydantic")
class _BaseModel:
    pass
pydantic_mod.BaseModel = _BaseModel
sys.modules.setdefault("pydantic", pydantic_mod)

# aiohttp
aiohttp_mod = types.ModuleType("aiohttp")
class _ClientTimeout:
    def __init__(self, *a, **k):
        pass
class _ClientSession:
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc, tb):
        return False
aiohttp_mod.ClientTimeout = _ClientTimeout
aiohttp_mod.ClientSession = _ClientSession
sys.modules.setdefault("aiohttp", aiohttp_mod)

# livekit and submodules
livekit_mod = types.ModuleType("livekit")
rtc_mod = types.ModuleType("livekit.rtc")
class _Room:
    pass
rtc_mod.Room = _Room
agents_mod = types.ModuleType("livekit.agents")
class _Agent:
    def __init__(self, **kwargs):
        pass
class _JobContext:
    pass
class _AgentSession:
    async def say(self, *a, **k):
        class _Handle:
            def __await__(self):
                async def _noop():
                    return None
                return _noop().__await__()
            def interrupt(self):
                pass
        return _Handle()
class _WorkerOptions:
    def __init__(self, *a, **k):
        pass
class _Worker:
    def __init__(self, *a, **k):
        pass
    async def run(self):
        return None
agents_mod.Agent = _Agent
agents_mod.JobContext = _JobContext
agents_mod.AgentSession = _AgentSession
agents_mod.WorkerOptions = _WorkerOptions
agents_mod.Worker = _Worker
llm_mod = types.ModuleType("livekit.agents.llm")
class _LLM:
    pass
class _ChatChunk:
    def __init__(self, id=None, delta=None):
        self.id = id
        self.delta = delta
class _ChoiceDelta:
    def __init__(self, role=None, content=None):
        self.role = role
        self.content = content
class _ChatContext:
    pass
llm_mod.LLM = _LLM
llm_mod.ChatChunk = _ChatChunk
llm_mod.ChoiceDelta = _ChoiceDelta
llm_mod.ChatContext = _ChatContext
plugins_mod = types.ModuleType("livekit.plugins")
deepgram_mod = types.ModuleType("livekit.plugins.deepgram")
silero_mod = types.ModuleType("livekit.plugins.silero")
turn_detector_mod = types.ModuleType("livekit.plugins.turn_detector")
multilingual_mod = types.ModuleType("livekit.plugins.turn_detector.multilingual")
class _MultilingualModel:
    pass
multilingual_mod.MultilingualModel = _MultilingualModel

sys.modules.setdefault("livekit", livekit_mod)
sys.modules.setdefault("livekit.rtc", rtc_mod)
sys.modules.setdefault("livekit.agents", agents_mod)
sys.modules.setdefault("livekit.agents.llm", llm_mod)
sys.modules.setdefault("livekit.plugins", plugins_mod)
sys.modules.setdefault("livekit.plugins.deepgram", deepgram_mod)
sys.modules.setdefault("livekit.plugins.silero", silero_mod)
sys.modules.setdefault("livekit.plugins.turn_detector", turn_detector_mod)
sys.modules.setdefault("livekit.plugins.turn_detector.multilingual", multilingual_mod)

# livekit.rtc.rpc submodule used by rpc_services.py
rtc_rpc_mod = types.ModuleType("livekit.rtc.rpc")
class _RpcInvocationData:
    def __init__(self, payload: str = "", caller_identity: str = ""):
        self.payload = payload
        self.caller_identity = caller_identity
rtc_rpc_mod.RpcInvocationData = _RpcInvocationData
sys.modules.setdefault("livekit.rtc.rpc", rtc_rpc_mod)

# google protobuf in generated stubs (minimal)
google_mod = types.ModuleType("google")
protobuf_mod = types.ModuleType("google.protobuf")
wrappers_mod = types.ModuleType("google.protobuf.wrappers_pb2")
empty_mod = types.ModuleType("google.protobuf.empty_pb2")
sys.modules.setdefault("google", google_mod)
sys.modules.setdefault("google.protobuf", protobuf_mod)
sys.modules.setdefault("google.protobuf.wrappers_pb2", wrappers_mod)
sys.modules.setdefault("google.protobuf.empty_pb2", empty_mod)

# generated.protos.interaction_pb2 minimal stub
generated_mod = types.ModuleType("generated")
protos_mod = types.ModuleType("generated.protos")
interaction_pb2_mod = types.ModuleType("generated.protos.interaction_pb2")
class _ClientUIActionResponse:
    def __init__(self):
        self.success = True
    def SerializeToString(self):
        return b""
    def ParseFromString(self, b):
        return None
interaction_pb2_mod.ClientUIActionResponse = _ClientUIActionResponse

# Minimal enum shim
class _ClientUIActionType:
    @staticmethod
    def Value(x):
        return x
interaction_pb2_mod.ClientUIActionType = _ClientUIActionType

# Minimal payload holder helpers
class _Obj:
    def __init__(self):
        pass

class _ListWithAdd(list):
    def add(self, **kwargs):
        obj = types.SimpleNamespace(**kwargs)
        self.append(obj)
        return obj

class _SuggestedResponsesPayload:
    def __init__(self):
        self.responses = []
        self.prompt = ""
        self.group_id = ""

class _AgentToClientUIActionRequest:
    def __init__(self, request_id: str = "", action_type: str = ""):
        self.request_id = request_id
        self.action_type = action_type
        self.append_text_to_editor_realtime_payload = _Obj()
        self.update_text_content_payload = _Obj()
        self.highlight_ranges_payload = _ListWithAdd()
        self.display_visual_aid_payload = _Obj()
        self.suggested_responses_payload = _SuggestedResponsesPayload()
        self.parameters = {}

    # Simulate protobuf API
    def SerializeToString(self):
        return b""

interaction_pb2_mod.AgentToClientUIActionRequest = _AgentToClientUIActionRequest

# AgentResponse used by rpc_services
class _AgentResponse:
    def __init__(self, status_message: str = "", success: bool = True):
        self.status_message = status_message
        self.success = success
    def SerializeToString(self):
        return b""
interaction_pb2_mod.AgentResponse = _AgentResponse
sys.modules.setdefault("generated", generated_mod)
sys.modules.setdefault("generated.protos", protos_mod)
sys.modules.setdefault("generated.protos.interaction_pb2", interaction_pb2_mod)

from rox.agent import RoxAgent


class FakeFrontendClient:
    """Test double to capture calls without requiring LiveKit room/identity."""
    def __init__(self):
        self.calls = []

    async def execute_visual_action(self, room, identity, tool_name, params):
        # Record and pretend success
        self.calls.append({
            "method": "execute_visual_action",
            "tool_name": tool_name,
            "params": params,
        })
        return True

    async def show_feedback(self, room, identity, feedback_type: str, message: str, duration_ms: int = 3000):
        self.calls.append({
            "method": "show_feedback",
            "feedback_type": feedback_type,
            "message": message,
            "duration_ms": duration_ms,
        })
        return True


def run(coro):
    return asyncio.run(coro)


def make_agent_with_fake_frontend():
    agent = RoxAgent()
    agent._frontend_client = FakeFrontendClient()
    agent._room = object()  # non-None sentinel
    agent.caller_identity = "test-client"
    return agent


def test_valid_text_prompt_forwards_to_frontend():
    agent = make_agent_with_fake_frontend()
    toolbelt = [{
        "tool_name": "display_visual_aid",
        "parameters": {
            "topic_context": "Photosynthesis",
            "text_to_visualize": "Steps from light absorption to glucose",
        }
    }]

    run(agent._execute_toolbelt(toolbelt))

    calls = agent._frontend_client.calls
    # Expect exactly one execute_visual_action call
    assert any(c["method"] == "execute_visual_action" for c in calls), calls
    last = next(c for c in calls if c["method"] == "execute_visual_action")
    assert last["tool_name"] == "GENERATE_VISUALIZATION"
    assert last["params"]["prompt"] == "Steps from light absorption to glucose"
    assert last["params"]["topic_context"] == "Photosynthesis"


def test_object_prompt_is_stringified():
    agent = make_agent_with_fake_frontend()
    complex_prompt = {"layers": ["HW", "Kernel", "User"], "edges": [[0,1],[1,2]]}
    toolbelt = [{
        "tool_name": "display_visual_aid",
        "parameters": {
            "topic_context": "OS Layers",
            "text_to_visualize": complex_prompt,
        }
    }]

    run(agent._execute_toolbelt(toolbelt))

    calls = agent._frontend_client.calls
    last = next(c for c in calls if c["method"] == "execute_visual_action")
    # Should be JSON string
    assert isinstance(last["params"]["prompt"], str)
    parsed = json.loads(last["params"]["prompt"])
    assert parsed == complex_prompt


def test_empty_prompt_shows_feedback_and_does_not_forward():
    agent = make_agent_with_fake_frontend()
    toolbelt = [{
        "tool_name": "display_visual_aid",
        "parameters": {
            "topic_context": "Any",
            "text_to_visualize": "    ",  # whitespace only
        }
    }]

    run(agent._execute_toolbelt(toolbelt))

    calls = agent._frontend_client.calls
    # Expect a show_feedback call and no execute_visual_action
    assert any(c["method"] == "show_feedback" for c in calls), calls
    assert not any(c["method"] == "execute_visual_action" for c in calls), calls
    fb = next(c for c in calls if c["method"] == "show_feedback")
    assert fb["feedback_type"] == "error"


def test_oversized_prompt_is_truncated():
    agent = make_agent_with_fake_frontend()
    big = "x" * 9000
    toolbelt = [{
        "tool_name": "display_visual_aid",
        "parameters": {
            "topic_context": "Big",
            "text_to_visualize": big,
        }
    }]

    run(agent._execute_toolbelt(toolbelt))

    calls = agent._frontend_client.calls
    last = next(c for c in calls if c["method"] == "execute_visual_action")
    sent = last["params"]["prompt"]
    assert len(sent) == 8000
