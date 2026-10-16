import pytest

from app.core.errors import NexusError
from app.services.model_catalog import (
    MODEL_OPTIONS,
    is_open_model,
    model_supports_attachments,
    validate_model_id,
)


def test_catalog_exposes_only_verified_gemini_and_open_models() -> None:
    model_ids = {option["id"] for option in MODEL_OPTIONS}

    assert "gemini-3.1-flash-lite" in model_ids
    assert "gemini-3.1-pro-preview" in model_ids
    assert "gemini-3.5-flash" in model_ids
    assert "qwen/qwen3-coder-480b-a35b-instruct-maas" in model_ids
    assert "deepseek-ai/deepseek-v3.2-maas" in model_ids
    assert "meta/llama-4-maverick-17b-128e-instruct-maas" not in model_ids


def test_open_models_require_vertex_and_do_not_claim_attachment_support() -> None:
    qwen = "qwen/qwen3-coder-480b-a35b-instruct-maas"
    deepseek = "deepseek-ai/deepseek-v3.2-maas"

    assert is_open_model(qwen)
    assert is_open_model(deepseek)
    assert not model_supports_attachments(qwen)
    assert not model_supports_attachments(deepseek)
    assert model_supports_attachments("gemini-3.8-flash")


def test_model_allowlist_rejects_unknown_vertex_model_ids() -> None:
    assert validate_model_id("gemini-3.1-pro-preview", "gemini-3.8-flash") == (
        "gemini-3.1-pro-preview"
    )
    with pytest.raises(NexusError):
        validate_model_id("unapproved-model", "gemini-3.8-flash")
