from __future__ import annotations

from app.core.config import Settings
from app.core.errors import ErrorCode, NexusError


MODEL_OPTIONS = (
    {
        "id": "gemini-3.8-flash",
        "label": "Gemini 3.8 Flash",
        "description": "Recommended for detailed analysis and multi-file coding tasks.",
    },
    {
        "id": "gemini-3.6-flash",
        "label": "Gemini 3.6 Flash",
        "description": "Balanced speed and reasoning for everyday work.",
    },
    {
        "id": "gemini-3.5-flash-lite",
        "label": "Gemini 3.5 Flash-Lite",
        "description": "Faster, lower-cost option for simpler tasks.",
    },
)
ALLOWED_MODEL_IDS = frozenset(option["id"] for option in MODEL_OPTIONS)


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
