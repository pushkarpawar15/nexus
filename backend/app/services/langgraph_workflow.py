from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class AsyncFirestoreCheckpointer(BaseCheckpointSaver):
    """Small async LangGraph checkpointer backed by the app's Firestore client."""

    def __init__(self, client: Any, collection: str = "langgraph_checkpoints") -> None:
        super().__init__()
        self._threads = client.collection(collection)

    def _thread(self, thread_id: str):
        return self._threads.document(thread_id)

    @staticmethod
    def _config_values(config: dict) -> tuple[str, str, str | None]:
        values = config.get("configurable", {})
        thread_id = values.get("thread_id")
        if not thread_id:
            raise ValueError("LangGraph checkpoint config requires thread_id.")
        return thread_id, values.get("checkpoint_ns", ""), values.get("checkpoint_id")

    async def aput(
        self,
        config: dict,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> dict:
        thread_id, checkpoint_ns, _ = self._config_values(config)
        checkpoint_id = checkpoint["id"]
        checkpoint_type, checkpoint_data = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_data = self.serde.dumps_typed(dict(metadata))
        parent_id = config.get("configurable", {}).get("checkpoint_id")
        await self._thread(thread_id).collection("checkpoints").document(checkpoint_id).set(
            {
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
                "parent_checkpoint_id": parent_id,
                "checkpoint_type": checkpoint_type,
                "checkpoint_data": checkpoint_data,
                "metadata_type": metadata_type,
                "metadata_data": metadata_data,
                "created_at": datetime.now(timezone.utc),
            }
        )
        configurable = dict(config.get("configurable", {}))
        configurable["checkpoint_id"] = checkpoint_id
        return {**config, "configurable": configurable}

    async def aput_writes(
        self,
        config: dict,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, checkpoint_ns, checkpoint_id = self._config_values(config)
        if checkpoint_id is None:
            raise ValueError("Pending writes require a checkpoint_id.")
        collection = self._thread(thread_id).collection("writes")
        for index, (channel, value) in enumerate(writes):
            value_type, value_data = self.serde.dumps_typed(value)
            key = hashlib.sha256(f"{task_id}:{index}:{channel}".encode()).hexdigest()
            await collection.document(key).set(
                {
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                    "task_id": task_id,
                    "task_path": task_path,
                    "index": index,
                    "channel": channel,
                    "value_type": value_type,
                    "value_data": value_data,
                }
            )

    async def aget_tuple(self, config: dict) -> CheckpointTuple | None:
        thread_id, checkpoint_ns, checkpoint_id = self._config_values(config)
        collection = self._thread(thread_id).collection("checkpoints")
        if checkpoint_id:
            snapshot = await collection.document(checkpoint_id).get()
            record = snapshot.to_dict() if snapshot.exists else None
        else:
            snapshots = [snapshot async for snapshot in collection.stream()]
            records = [snapshot.to_dict() for snapshot in snapshots]
            records = [
                record
                for record in records
                if record.get("checkpoint_ns", "") == checkpoint_ns
            ]
            record = max(records, key=lambda item: item.get("created_at"), default=None)
        if record is None or record.get("checkpoint_ns", "") != checkpoint_ns:
            return None

        configurable = {
            **config.get("configurable", {}),
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": record["checkpoint_id"],
        }
        writes_query = self._thread(thread_id).collection("writes").where(
            "checkpoint_id", "==", record["checkpoint_id"]
        )
        write_snapshots = [snapshot async for snapshot in writes_query.stream()]
        pending_writes = [
            (
                item.to_dict()["task_id"],
                item.to_dict()["channel"],
                self.serde.loads_typed(
                    (
                        item.to_dict()["value_type"],
                        item.to_dict()["value_data"],
                    )
                ),
            )
            for item in write_snapshots
            if item.to_dict().get("checkpoint_ns", "") == checkpoint_ns
        ]
        parent_id = record.get("parent_checkpoint_id")
        parent_config = (
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_id,
                }
            }
            if parent_id
            else None
        )
        return CheckpointTuple(
            config={"configurable": configurable},
            checkpoint=self.serde.loads_typed(
                (record["checkpoint_type"], record["checkpoint_data"])
            ),
            metadata=self.serde.loads_typed(
                (record["metadata_type"], record["metadata_data"])
            ),
            parent_config=parent_config,
            pending_writes=pending_writes,
        )

    async def alist(
        self,
        config: dict | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return
        thread_id, checkpoint_ns, _ = self._config_values(config)
        snapshots = [
            snapshot
            async for snapshot in self._thread(thread_id).collection("checkpoints").stream()
        ]
        records = [snapshot.to_dict() for snapshot in snapshots]
        records = [
            record
            for record in records
            if record.get("checkpoint_ns", "") == checkpoint_ns
        ]
        records.sort(key=lambda item: item.get("created_at"), reverse=True)
        if before:
            _, _, before_id = self._config_values(before)
            before_record = next(
                (item for item in records if item.get("checkpoint_id") == before_id), None
            )
            if before_record:
                records = [
                    item
                    for item in records
                    if item.get("created_at") < before_record.get("created_at")
                ]
        if filter:
            records = [
                item
                for item in records
                if all(
                    self.serde.loads_typed((item["metadata_type"], item["metadata_data"])).get(
                        key
                    )
                    == value
                    for key, value in filter.items()
                )
            ]
        if limit is not None:
            records = records[:limit]
        for record in records:
            checkpoint_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": record["checkpoint_id"],
                }
            }
            checkpoint_tuple = await self.aget_tuple(checkpoint_config)
            if checkpoint_tuple is not None:
                yield checkpoint_tuple

    async def adelete_thread(self, thread_id: str) -> None:
        thread = self._thread(thread_id)
        for collection_name in ("checkpoints", "writes"):
            snapshots = [
                snapshot async for snapshot in thread.collection(collection_name).stream()
            ]
            for snapshot in snapshots:
                await snapshot.reference.delete()


class OperatorInputState(TypedDict):
    question: str
    answer: str | None


def build_operator_input_graph(checkpointer: BaseCheckpointSaver):
    async def request_answer(state: OperatorInputState) -> dict[str, str]:
        answer = interrupt({"question": state["question"]})
        return {"answer": answer}

    graph = StateGraph(OperatorInputState)
    graph.add_node("request_answer", request_answer)
    graph.add_edge(START, "request_answer")
    graph.add_edge("request_answer", END)
    return graph.compile(checkpointer=checkpointer)


__all__ = ["AsyncFirestoreCheckpointer", "build_operator_input_graph"]
