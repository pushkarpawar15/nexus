from __future__ import annotations

import logging
import uuid
from typing import Any
from urllib.parse import quote

from google.adk import Runner, Workflow
from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.errors.session_not_found_error import SessionNotFoundError
from google.adk.events import Event, RequestInput
from google.adk.platform import time as platform_time
from google.adk.sessions import BaseSessionService, Session, State
from google.adk.sessions.base_session_service import (
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.utils._function_call_names import REQUEST_INPUT_FUNCTION_CALL_NAME
from google.adk.workflow.utils._workflow_hitl_utils import create_request_input_response
from google.genai import types

logger = logging.getLogger(__name__)

OPERATOR_APP_NAME = "operator_input"
OPERATOR_USER_ID = "nexus_operator"
_ANSWER_KEY = "answer"


def request_operator_answer(question: str):
    """Workflow node that pauses until the operator answers (no model involved)."""
    yield RequestInput(message=question)


def receive_operator_answer(node_input):
    """Workflow node that turns the operator's answer into the workflow output."""
    return node_input


def build_operator_input_workflow() -> Workflow:
    """Human-input workflow mirroring the retired LangGraph interrupt graph."""
    return Workflow(
        name=OPERATOR_APP_NAME,
        edges=[("START", request_operator_answer, receive_operator_answer)],
    )


class FirestoreSessionService(BaseSessionService):
    """ADK session service persisted in the app's Firestore database.

    Replaces the LangGraph Firestore checkpointer. Each session lives in a
    single document keyed by app name, user id and session id, with the
    session state and its events embedded. Executions only pause for one
    operator question at a time, so a single writer owns each document.
    """

    def __init__(self, client: Any, collection: str = "adk_sessions") -> None:
        self._client = client
        self._collection = collection

    def _document(self, app_name: str, user_id: str, session_id: str):
        document_id = "::".join(quote(part, safe="") for part in (app_name, user_id, session_id))
        return self._client.collection(self._collection).document(document_id)

    @staticmethod
    def _to_document(session: Session) -> dict[str, Any]:
        return {
            "app_name": session.app_name,
            "user_id": session.user_id,
            "session_id": session.id,
            "state": {
                key: value
                for key, value in session.state.items()
                if not key.startswith(State.TEMP_PREFIX)
            },
            "last_update_time": session.last_update_time,
            "events": [event.model_dump(mode="json") for event in session.events],
        }

    @staticmethod
    def _from_document(data: dict[str, Any]) -> Session:
        return Session(
            app_name=data["app_name"],
            user_id=data["user_id"],
            id=data["session_id"],
            state=data.get("state", {}),
            events=[Event.model_validate(item) for item in data.get("events", [])],
            last_update_time=data.get("last_update_time", 0.0),
        )

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Session:
        session_id = session_id.strip() if session_id else str(uuid.uuid4())
        document = self._document(app_name, user_id, session_id)
        existing = await document.get()
        if existing.exists:
            raise AlreadyExistsError(f"Session with id {session_id} already exists.")
        session = Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            state=dict(state or {}),
            last_update_time=platform_time.get_time(),
        )
        await document.set(self._to_document(session))
        return session

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: GetSessionConfig | None = None,
    ) -> Session | None:
        session_id = session_id.strip() if session_id else session_id
        snapshot = await self._document(app_name, user_id, session_id).get()
        if not snapshot.exists:
            return None
        session = self._from_document(snapshot.to_dict())
        if config is None:
            return session
        events = session.events
        if config.num_recent_events is not None:
            if config.num_recent_events == 0:
                events = []
            else:
                events = events[-config.num_recent_events :]
        if config.after_timestamp is not None:
            index = len(events) - 1
            while index >= 0:
                if events[index].timestamp < config.after_timestamp:
                    break
                index -= 1
            if index >= 0:
                events = events[index + 1 :]
        if events is not session.events:
            session = session.model_copy(update={"events": events})
        return session

    async def list_sessions(
        self, *, app_name: str, user_id: str | None = None
    ) -> ListSessionsResponse:
        query = self._client.collection(self._collection).where("app_name", "==", app_name)
        if user_id is not None:
            query = query.where("user_id", "==", user_id)
        sessions: list[Session] = []
        async for snapshot in query.stream():
            data = snapshot.to_dict() or {}
            if not data:
                continue
            sessions.append(
                Session(
                    app_name=data["app_name"],
                    user_id=data["user_id"],
                    id=data["session_id"],
                    state=data.get("state", {}),
                    events=[],
                    last_update_time=data.get("last_update_time", 0.0),
                )
            )
        sessions.sort(key=lambda item: (item.last_update_time, item.user_id, item.id))
        return ListSessionsResponse(sessions=sessions)

    async def delete_session(self, *, app_name: str, user_id: str, session_id: str) -> None:
        session_id = session_id.strip() if session_id else session_id
        await self._document(app_name, user_id, session_id).delete()

    async def append_event(self, session: Session, event: Event) -> Event:
        if event.partial:
            return event
        committed = await super().append_event(session=session, event=event)
        session.last_update_time = committed.timestamp
        document = self._document(session.app_name, session.user_id, session.id)
        snapshot = await document.get()
        if not snapshot.exists:
            raise SessionNotFoundError(f"Session {session.id} not found.")
        await document.set(self._to_document(session))
        return committed


def _pending_interrupt_id(session: Session) -> str | None:
    """Returns the id of the last request-input call that is still unanswered."""
    answered: set[str] = set()
    pending: str | None = None
    for event in session.events:
        parts = event.content.parts if event.content and event.content.parts else []
        for part in parts:
            if (
                part.function_call
                and part.function_call.name == REQUEST_INPUT_FUNCTION_CALL_NAME
                and part.function_call.id
            ):
                pending = part.function_call.id
            elif part.function_response and part.function_response.id:
                answered.add(part.function_response.id)
    if pending is None or pending in answered:
        return None
    return pending


class OperatorInputWorkflow:
    """Pauses an execution for an operator question and resumes it with the answer.

    Successor of the LangGraph interrupt graph: an ADK ``Workflow`` whose
    human-input node yields ``RequestInput`` without calling any model, with
    the pause state persisted through an ADK session service.
    """

    def __init__(self, session_service: BaseSessionService) -> None:
        self._session_service = session_service
        self._runner = Runner(
            app_name=OPERATOR_APP_NAME,
            agent=build_operator_input_workflow(),
            session_service=session_service,
        )

    async def request_answer(self, execution_id: str, question: str) -> None:
        """Runs the workflow until it pauses waiting for the operator's answer."""
        try:
            await self._session_service.create_session(
                app_name=OPERATOR_APP_NAME,
                user_id=OPERATOR_USER_ID,
                session_id=execution_id,
            )
        except AlreadyExistsError:
            pass
        paused = False
        async for event in self._runner.run_async(
            user_id=OPERATOR_USER_ID,
            session_id=execution_id,
            new_message=types.Content(role="user", parts=[]),
            state_delta={"question": question},
        ):
            if event.long_running_tool_ids:
                paused = True
        if not paused:
            raise RuntimeError(
                "The operator-input workflow finished without waiting for an answer."
            )

    async def provide_answer(self, execution_id: str, answer: str) -> str:
        """Resumes the paused workflow and returns the validated operator answer."""
        session = await self._session_service.get_session(
            app_name=OPERATOR_APP_NAME,
            user_id=OPERATOR_USER_ID,
            session_id=execution_id,
        )
        if session is None:
            raise ValueError("No operator-input session exists for this execution.")
        interrupt_id = _pending_interrupt_id(session)
        if interrupt_id is None:
            raise ValueError("The operator-input workflow has no question awaiting an answer.")
        reply = types.Content(
            role="user",
            parts=[create_request_input_response(interrupt_id, {_ANSWER_KEY: answer})],
        )
        resumed = None
        async for event in self._runner.run_async(
            user_id=OPERATOR_USER_ID,
            session_id=execution_id,
            new_message=reply,
        ):
            if event.output is not None:
                resumed = event.output
        value = resumed.get(_ANSWER_KEY) if isinstance(resumed, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("The operator-input workflow did not return the operator's answer.")
        return value.strip()

    async def discard(self, execution_id: str) -> None:
        """Removes the execution's session, abandoning any paused question."""
        await self._session_service.delete_session(
            app_name=OPERATOR_APP_NAME,
            user_id=OPERATOR_USER_ID,
            session_id=execution_id,
        )


__all__ = [
    "FirestoreSessionService",
    "OperatorInputWorkflow",
    "build_operator_input_workflow",
]
