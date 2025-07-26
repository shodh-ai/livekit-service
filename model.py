# File: livekit-service/model.py

from pydantic import BaseModel


class AgentRequest(BaseModel):
    room_name: str
    room_url: str
    client_identity: str
