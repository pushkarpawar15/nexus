from __future__ import annotations

import asyncio

import pytest
from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.events import Event, EventActions
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.services.operator_input_workflow import (
    OPERATOR_APP_NAME,
    OPERATOR_USER_ID,
    FirestoreSessionService,
    OperatorInputWorkflow,
    build_operator_input_workflow,
)


def _pause_calls(session) -> list:
    calls = []
    for event in session.events:
        parts = event.content.parts if event.content and event.content.parts else []
        for part in parts:
            if part.function_call and part.function_call.name == "adk_request_input":
                calls.append(part.function_call)
    return calls


def test_pause_carries_the_question_and_waits_without_a_model() -> None:
    async def scenario():
        workflow = OperatorInputWorkflow(InMemorySessionService())
        await workflow.request_answer("exec-1", "Apply the operator's hint?")
        session = await workflow._session_service.get_session(
            app_name=OPERATOR_APP_NAME, user_id=OPERATOR_USER_ID, session_id="exec-1"
        )
        return session

    session = asyncio.run(scenario())
    assert session is not None
    calls = _pause_calls(session)
    assert len(calls) == 1
    assert calls[0].args["message"] == "Apply the operator's hint?"
    assert calls[0].id


def test_pause_resume_returns_the_operator_answer() -> None:
    async def scenario() -> str:
        workflow = OperatorInputWorkflow(InMemorySessionService())
        await workflow.request_answer("exec-1", "Retry the failing check?")
        return await workflow.provide_answer("exec-1", "Retry with -v")

    assert asyncio.run(scenario()) == "Retry with -v"


def test_multiple_questions_on_one_execution_get_distinct_answers() -> None:
    async def scenario() -> list[str]:
        workflow = OperatorInputWorkflow(InMemorySessionService())
        await workflow.request_answer("exec-1", "Question one?")
        first = await workflow.provide_answer("exec-1", "Answer one")
        await workflow.request_answer("exec-1", "Question two?")
        second = await workflow.provide_answer("exec-1", "Answer two")
        return [first, second]

    assert asyncio.run(scenario()) == ["Answer one", "Answer two"]


def test_provide_answer_twice_has_no_pending_question() -> None:
    async def scenario() -> str | None:
        workflow = OperatorInputWorkflow(InMemorySessionService())
        await workflow.request_answer("exec-1", "Question?")
        await workflow.provide_answer("exec-1", "Answer")
        try:
            await workflow.provide_answer("exec-1", "Again")
        except ValueError as exc:
            return str(exc)
        return None

    message = asyncio.run(scenario())
    assert message == "The operator-input workflow has no question awaiting an answer."


def test_provide_answer_for_unknown_execution_raises() -> None:
    async def scenario() -> str | None:
        workflow = OperatorInputWorkflow(InMemorySessionService())
        try:
            await workflow.provide_answer("missing", "Answer")
        except ValueError as exc:
            return str(exc)
        return None

    assert asyncio.run(scenario()) == "No operator-input session exists for this execution."


def test_discard_abandons_a_pending_question() -> None:
    async def scenario() -> str | None:
        workflow = OperatorInputWorkflow(InMemorySessionService())
        await workflow.request_answer("exec-1", "Question?")
        await workflow.discard("exec-1")
        try:
            await workflow.provide_answer("exec-1", "Answer")
        except ValueError as exc:
            return str(exc)
        return None

    assert asyncio.run(scenario()) == "No operator-input session exists for this execution."


def test_workflow_graph_is_pure_human_input() -> None:
    workflow = build_operator_input_workflow()
    assert workflow.name == OPERATOR_APP_NAME


# --- FirestoreSessionService -------------------------------------------------


class _Snapshot:
    def __init__(self, data: dict | None) -> None:
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict | None:
        return dict(self._data) if self._data is not None else None


class _Document:
    def __init__(self, store: dict, key: str) -> None:
        self._store, self._key = store, key

    async def get(self) -> _Snapshot:
        return _Snapshot(self._store.get(self._key))

    async def set(self, data: dict) -> None:
        self._store[self._key] = dict(data)

    async def delete(self) -> None:
        self._store.pop(self._key, None)


class _Query:
    def __init__(self, store: dict) -> None:
        self._store = store
        self._filters: list[tuple[str, str, object]] = []

    def where(self, field: str, operator: str, value: object) -> _Query:
        self._filters.append((field, operator, value))
        return self

    async def stream(self):
        for key in sorted(self._store):
            data = self._store[key]
            if all(data.get(field) == value for field, _, value in self._filters):
                yield _Snapshot(data)


class _Collection:
    def __init__(self, store: dict) -> None:
        self._store = store

    def document(self, key: str) -> _Document:
        return _Document(self._store, key)

    def where(self, field: str, operator: str, value: object) -> _Query:
        return _Query(self._store).where(field, operator, value)


class FakeFirestoreClient:
    def __init__(self) -> None:
        self._docs: dict[str, dict] = {}

    def collection(self, name: str) -> _Collection:
        return _Collection(self._docs)


def test_firestore_session_service_create_append_and_persist() -> None:
    async def scenario():
        service = FirestoreSessionService(FakeFirestoreClient())
        session = await service.create_session(
            app_name="app", user_id="user", session_id="s1", state={"a": 1}
        )
        with pytest.raises(AlreadyExistsError):
            await service.create_session(app_name="app", user_id="user", session_id="s1")
        event = Event(
            author="test",
            content=types.Content(role="user", parts=[types.Part(text="hi")]),
            actions=EventActions(state_delta={"kept": "yes", "temp:flag": True}),
        )
        await service.append_event(session, event)
        loaded = await service.get_session(app_name="app", user_id="user", session_id="s1")
        missing = await service.get_session(app_name="app", user_id="user", session_id="nope")
        listed = await service.list_sessions(app_name="app", user_id="user")
        await service.delete_session(app_name="app", user_id="user", session_id="s1")
        deleted = await service.get_session(app_name="app", user_id="user", session_id="s1")
        return session, loaded, missing, listed, deleted

    session, loaded, missing, listed, deleted = asyncio.run(scenario())
    assert missing is None
    assert deleted is None
    assert loaded is not None
    assert loaded.state == {"a": 1, "kept": "yes"}
    assert len(loaded.events) == 1
    assert loaded.events[0].content.parts[0].text == "hi"
    assert session.state.get("temp:flag") is True  # kept in memory for this run
    assert [item.id for item in listed.sessions] == ["s1"]
    assert listed.sessions[0].events == []


def test_firestore_session_service_filters_recent_events() -> None:
    async def scenario():
        service = FirestoreSessionService(FakeFirestoreClient())
        session = await service.create_session(app_name="app", user_id="user", session_id="s1")
        for index in range(3):
            await service.append_event(
                session,
                Event(
                    author="test",
                    content=types.Content(role="user", parts=[types.Part(text=f"m{index}")]),
                ),
            )
        from google.adk.sessions.base_session_service import GetSessionConfig

        recent = await service.get_session(
            app_name="app",
            user_id="user",
            session_id="s1",
            config=GetSessionConfig(num_recent_events=2),
        )
        none = await service.get_session(
            app_name="app",
            user_id="user",
            session_id="s1",
            config=GetSessionConfig(num_recent_events=0),
        )
        return recent, none

    recent, none = asyncio.run(scenario())
    assert [e.content.parts[0].text for e in recent.events] == ["m1", "m2"]
    assert none.events == []


def test_operator_workflow_persists_through_firestore_sessions() -> None:
    async def scenario() -> tuple[str, int]:
        service = FirestoreSessionService(FakeFirestoreClient())
        workflow = OperatorInputWorkflow(service)
        await workflow.request_answer("exec-9", "Ship the patch?")
        paused = await service.get_session(
            app_name=OPERATOR_APP_NAME,
            user_id=OPERATOR_USER_ID,
            session_id="exec-9",
        )
        calls = _pause_calls(paused)
        answer = await workflow.provide_answer("exec-9", "Yes, ship it")
        completed = await service.get_session(
            app_name=OPERATOR_APP_NAME,
            user_id=OPERATOR_USER_ID,
            session_id="exec-9",
        )
        await workflow.discard("exec-9")
        gone = await service.get_session(
            app_name=OPERATOR_APP_NAME,
            user_id=OPERATOR_USER_ID,
            session_id="exec-9",
        )
        return answer, len(calls), len(completed.events), gone

    answer, call_count, event_count, gone = asyncio.run(scenario())
    assert answer == "Yes, ship it"
    assert call_count == 1
    assert event_count >= 4  # user message, pause, reply, workflow output
    assert gone is None
