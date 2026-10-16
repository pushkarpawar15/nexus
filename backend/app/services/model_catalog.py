from __future__ import annotations

from app.core.config import Settings
from app.core.errors import ErrorCode, NexusError

MODEL_OPTIONS = (
    {
        "id": "gemini-3.8-flash",
        "label": "Gemini 3.8 Flash",
        "description": "Recommended for detailed analysis and multi-file coding tasks.",
        "provider": "Gemini",
        "supports_attachments": True,
    },
    {
        "id": "gemini-3.1-pro-preview",
        "label": "Gemini 3.1 Pro (preview)",
        "description": "Higher reasoning capacity for complex tasks; preview model.",
        "provider": "Gemini",
        "supports_attachments": True,
    },
    {
        "id": "gemini-3.6-flash",
        "label": "Gemini 3.6 Flash",
        "description": "Balanced speed and reasoning for everyday work.",
        "provider": "Gemini",
        "supports_attachments": True,
    },
    {
        "id": "gemini-3.5-flash",
        "label": "Gemini 3.5 Flash",
        "description": "General-purpose Gemini model for analysis and coding.",
        "provider": "Gemini",
        "supports_attachments": True,
    },
    {
        "id": "gemini-3.5-flash-lite",
        "label": "Gemini 3.5 Flash-Lite",
        "description": "Faster, lower-cost option for simpler tasks.",
        "provider": "Gemini",
        "supports_attachments": True,
    },
    {
        "id": "gemini-3.1-flash-lite",
        "label": "Gemini 3.1 Flash-Lite",
        "description": "Cost-efficient, low-latency model for simpler tasks.",
        "provider": "Gemini",
        "supports_attachments": True,
    },
    {
        "id": "qwen/qwen3-coder-480b-a35b-instruct-maas",
        "label": "Qwen3 Coder (open model)",
        "description": "Open coding model served through Vertex AI MaaS.",
        "provider": "Open model",
        "supports_attachments": False,
    },
    {
        "id": "deepseek-ai/deepseek-v3.2-maas",
        "label": "DeepSeek V3.2 (open model)",
        "description": "Open reasoning and coding model served through Vertex AI MaaS.",
        "provider": "Open model",
        "supports_attachments": False,
    },
)
ALLOWED_MODEL_IDS = frozenset(option["id"] for option in MODEL_OPTIONS)
OPEN_MODEL_IDS = frozenset(
    option["id"] for option in MODEL_OPTIONS if option["provider"] == "Open model"
)
CODING_FALLBACK_MODEL_IDS = (
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
)


def is_open_model(model_id: str | None) -> bool:
    return model_id in OPEN_MODEL_IDS


def model_supports_attachments(model_id: str | None) -> bool:
    return next(
        (option["supports_attachments"] for option in MODEL_OPTIONS if option["id"] == model_id),
        False,
    )


def validate_model_id(model_id: str | None, default: str) -> str:
    selected = model_id or default
    if selected not in ALLOWED_MODEL_IDS:
        raise NexusError(
            ErrorCode.HTTP_ERROR,
            "Choose a model from the available model list.",
            status_code=422,
        )
    return selected


def model_choices(settings: Settings) -> dict[str, object]:
    analysis_default = settings.gemini_analysis_model or settings.gemini_model
    return {
        "analysis_default": analysis_default,
        "coding_default": settings.gemini_coding_model,
        "analysis_provider": "Gemini API" if settings.gemini_api_key else "Vertex AI",
        "coding_provider": "Vertex AI",
        "coding_location": settings.coding_model_location,
        "models": [dict(option) for option in MODEL_OPTIONS],
    }
