from pydantic import BaseModel


class AgentRequest(BaseModel):
    room_name: str
    room_url: str
