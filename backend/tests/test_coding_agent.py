import asyncio
import hashlib

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.coding_agent import (
    CodeProposal,
    CodingAgent,
    GeminiCodingAgent,
    _coding_model_fallbacks,
    _parse_code_proposal,
)


def test_coding_agent_uses_direct_structured_gemini_adapter_by_default() -> None:
    settings = Settings(_env_file=None, environment="testing", storage="memory")

    agent = CodingAgent(settings)

    assert isinstance(agent.model, GeminiCodingAgent)


def test_coding_model_fallbacks_only_move_to_lower_cost_options() -> None:
    assert _coding_model_fallbacks("gemini-3.8-flash") == [
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
    ]
    assert _coding_model_fallbacks("gemini-3.6-flash") == [
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
    ]
    assert _coding_model_fallbacks("gemini-3.5-flash-lite") == [
        "gemini-3.5-flash-lite",
    ]


def test_coding_agent_falls_back_after_provider_quota_error() -> None:
    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        gemini_coding_model="gemini-3.8-flash",
    )
    requested_models = []

    class QuotaLimitedModel(GeminiCodingAgent):
        async def propose(
            self,
            _task,
            _context,
            _paths,
            _previous_error=None,
            attachments=None,
            model_id=None,
        ):
            requested_models.append(model_id)
            if model_id == "gemini-3.8-flash":
                raise RuntimeError("429 RESOURCE_EXHAUSTED")
            return CodeProposal(
                rationale="Create a small source file.",
                operations=[{"operation": "create", "path": "probe.txt", "content": "ok"}],
            )

    progress_events = []

    async def on_progress(event_type, message, metadata=None):
        progress_events.append((event_type, message, metadata))

    async def run():
        return await CodingAgent(settings, QuotaLimitedModel(settings))._propose(
            task=None,
            context={},
            paths=[],
            previous_error=None,
            attachments=None,
            model_id=None,
            on_progress=on_progress,
            attempt=0,
        )

    proposal, selected_model = asyncio.run(run())

    assert requested_models == ["gemini-3.8-flash", "gemini-3.6-flash"]
    assert selected_model == "gemini-3.6-flash"
    assert proposal.operations[0].path == "probe.txt"
    assert progress_events[0][0] == "coding.model.fallback"


def test_parse_repairs_preconditions_from_exact_source_context() -> None:
    source = "current file contents\n"
    proposal = {
        "rationale": "Update and remove files",
        "operations": [
            {"operation": "replace", "path": "src/app.ts", "content": "updated"},
            {
                "operation": "delete",
                "path": "src/obsolete.ts",
                "content": "model supplied irrelevant content",
            },
        ],
    }

    parsed = _parse_code_proposal(
        proposal,
        {"src/app.ts": source, "src/obsolete.ts": source},
    )

    assert (
        parsed.operations[0].original_sha256 == hashlib.sha256(source.encode("utf-8")).hexdigest()
    )
    assert parsed.operations[1].content is None
    assert (
        parsed.operations[1].original_sha256 == hashlib.sha256(source.encode("utf-8")).hexdigest()
    )


def test_parse_does_not_invent_preconditions_without_source_context() -> None:
    proposal = {
        "rationale": "Delete a file",
        "operations": [{"operation": "delete", "path": "src/obsolete.ts"}],
    }

    with pytest.raises(ValidationError, match="delete requires only an original hash"):
        _parse_code_proposal(proposal, {})


def test_parse_does_not_invent_missing_replacement_content() -> None:
    proposal = {
        "rationale": "Replace a file",
        "operations": [{"operation": "replace", "path": "src/app.ts"}],
    }

    with pytest.raises(ValidationError, match="replace requires content and original hash"):
        _parse_code_proposal(proposal, {"src/app.ts": "current file contents\n"})
