# Import all adapters so they self-register on package import.
from app.services.ingestion.adapters.openai_adapter import OpenAIAdapter
from app.services.ingestion.adapters.anthropic_adapter import AnthropicAdapter
from app.services.ingestion.adapters.google_adapter import GoogleAdapter
from app.services.ingestion.adapters.generic_adapter import GenericAdapter

__all__ = ["OpenAIAdapter", "AnthropicAdapter", "GoogleAdapter", "GenericAdapter"]
