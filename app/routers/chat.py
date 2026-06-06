import os
from fastapi import APIRouter
from pydantic import BaseModel
from typing import List
import anthropic

router = APIRouter(prefix="/chat", tags=["chat"])

_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

_SYSTEM = """You are an AI Governance assistant embedded in an AI observability platform.
You help users understand their AI usage costs, security risks, alerts, policy compliance,
and how to use the platform. Be concise and helpful. When asked about costs, alerts,
PII risks, or governance rules, explain clearly. You do not have live access to the
user's data — refer them to the relevant dashboard page when they need specific numbers."""


class Message(BaseModel):
    role: str  # "user" or "assistant"
    text: str


class ChatRequest(BaseModel):
    message: str
    history: List[Message] = []


class ChatResponse(BaseModel):
    reply: str


@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest):
    messages = []
    for m in req.history[-10:]:  # keep last 10 turns for context
        messages.append({
            "role": m.role,
            "content": m.text,
        })
    messages.append({"role": "user", "content": req.message})

    response = _client.messages.create(
        model="claude-opus-4-8",
        max_tokens=512,
        system=_SYSTEM,
        messages=messages,
    )

    reply = next(
        (block.text for block in response.content if block.type == "text"),
        "Sorry, I couldn't generate a response.",
    )
    return ChatResponse(reply=reply)
