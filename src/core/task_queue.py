"""
SimpleClaw v2.1 - Persistent Task Queue
==========================================
Redis Streams como fila persistente de tarefas.
Sobrevive restart. Worker pode morrer e outro assume.
Estado reconstruído via replay de eventos.

Dependências:
    pip install redis

Configuração .env:
    SIMPLECLAW_REDIS_URL=redis://localhost:6379/0
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

logger = structlog.get_logger()


class TaskEvent:
    """Evento de uma tarefa — unidade atômica de estado."""

    def __init__(self, event_type: str, data: dict = None,
                 worker_id: str = "", task_id: str = ""):
        self.event_type = event_type  # enqueued, claimed, started, progressed, checkpoint, failed, completed, recovered
        self.data = data or {}
        self.worker_id = worker_id
        self.task_id = task_id
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_redis(self) -> dict:
        return {
            "type": self.event_type,
            "task_id": self.task_id,
            "worker": self.worker_id,
            "data": json.dumps(self.data, ensure_ascii=False, default=str),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_redis(cls, fields: dict) -> TaskEvent:
        return cls(
            event_type=fields.get("type", "unknown"),
            task_id=fields.get("task_id", ""),
            worker_id=fields.get("worker", ""),
            data=json.loads(fields.get("data", "{}")),
        )


class TaskState:
    """Estado reconstruído a partir do log de eventos."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.status = "unknown"
        self.user_id = ""
        self.capability = ""
        self.payload: dict = {}
        self.worker_id = ""
        self.events: list[TaskEvent] = []
        self.result: Optional[str] = None
        self.error: Optional[str] = None
        self.enqueued_at: Optional[str] = None
        self.started_at: Optional[str] = None
        self.completed_at: Optional[str] = None
        self.checkpoints: list[dict] = []

    @classmethod
    def replay(cls, task_id: str, events: list[TaskEvent]) -> TaskState:
        """Reconstrói estado completo a partir do log de eventos."""
        state = cls(task_id)

        for event in events:
            state.events.append(event)

            if event.event_type == "enqueued":
                state.status = "pending"
                state.user_id = event.data.get("user_id", "")
                state.capability = event.data.get("capability", "")
                state.payload = event.data.get("payload", {})
                state.enqueued_at = event.timestamp

            elif event.event_type == "claimed":
                state.status = "claimed"
                state.worker_id = event.worker_id

            elif event.event_type == "started":
                state.status = "processing"
                state.started_at = event.timestamp

            elif event.event_type == "progressed":
                state.status = "processing"

            elif event.event_type == "checkpoint":
                state.checkpoints.append({
                    "step": event.data.get("step", ""),
                    "hash": event.data.get("hash", ""),
                    "timestamp": event.timestamp,
                })

            elif event.event_type == "completed":
                state.status = "completed"
                state.result = event.data.get("result", "")
                state.completed_at = event.timestamp

            elif event.event_type == "failed":
                state.status = "failed"
                state.error = event.data.get("error", "")

            elif event.event_type == "recovered":
                state.status = "processing"
                state.error = None

        return state


class PersistentTaskQueue:
    """
    Fila distribuída e persistente via Redis Streams.
    Sobrevive restart. Estado reconstruível.
    """

    PENDING_STREAM = "tasks:pending"
    GROUP_NAME = "simpleclaw-workers"

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        import redis as redis_lib
        self._redis = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        self._ensure_consumer_group()

    def _ensure_consumer_group(self) -> None:
        """Create consumer group if it doesn't exist."""
        try:
            self._redis.xgroup_create(
                self.PENDING_STREAM,
                self.GROUP_NAME,
                id="0",
                mkstream=True,
            )
            logger.info("task_queue.group_created", group=self.GROUP_NAME)
        except Exception as e:
            if "BUSYGROUP" in str(e):
                pass  # Group already exists
            else:
                logger.error("task_queue.group_create_failed", error=str(e))

    def enqueue(self, user_id: str, capability: str, payload: dict,
                original_request: str = "") -> str:
        """
        Persiste tarefa ANTES de qualquer processamento.
        Retorna task_id.
        """
        task_id = str(uuid.uuid4())[:12]

        # Add to pending stream
        self._redis.xadd(
            self.PENDING_STREAM,
            {
                "task_id": task_id,
                "user_id": user_id,
                "capability": capability,
                "payload": json.dumps(payload, ensure_ascii=False, default=str),
                "original_request": original_request[:500],
                "enqueued_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        # Log event
        self._log_event(task_id, TaskEvent(
            event_type="enqueued",
            task_id=task_id,
            data={
                "user_id": user_id,
                "capability": capability,
                "payload": payload,
                "original_request": original_request[:200],
            },
        ))

        logger.info("task_queue.enqueued", task_id=task_id, capability=capability, user_id=user_id)
        return task_id

    def claim_next(self, worker_id: Optional[str] = None) -> Optional[dict]:
        """
        Atomically claim next pending task.
        Outro worker não vê a mesma tarefa.
        """
        wid = worker_id or self._worker_id

        try:
            response = self._redis.xreadgroup(
                groupname=self.GROUP_NAME,
                consumername=wid,
                streams={self.PENDING_STREAM: ">"},
                count=1,
                block=5000,
            )

            if not response:
                return None

            # Parse response: [(stream_name, [(message_id, fields)])]
            stream_name, messages = response[0]
            if not messages:
                return None

            message_id, fields = messages[0]

            task_data = {
                "message_id": message_id,
                "task_id": fields["task_id"],
                "user_id": fields["user_id"],
                "capability": fields["capability"],
                "payload": json.loads(fields.get("payload", "{}")),
                "original_request": fields.get("original_request", ""),
            }

            # Log claim event
            self._log_event(task_data["task_id"], TaskEvent(
                event_type="claimed",
                task_id=task_data["task_id"],
                worker_id=wid,
            ))

            logger.info("task_queue.claimed", task_id=task_data["task_id"], worker=wid)
            return task_data

        except Exception as e:
            logger.error("task_queue.claim_failed", error=str(e))
            return None

    def ack(self, message_id: str) -> None:
        """Acknowledge processed message."""
        try:
            self._redis.xack(self.PENDING_STREAM, self.GROUP_NAME, message_id)
        except Exception as e:
            logger.error("task_queue.ack_failed", error=str(e))

    def checkpoint(self, task_id: str, step: str, data: dict = None) -> None:
        """Persist progress checkpoint."""
        self._log_event(task_id, TaskEvent(
            event_type="checkpoint",
            task_id=task_id,
            worker_id=self._worker_id,
            data={"step": step, **(data or {})},
        ))

    def mark_started(self, task_id: str) -> None:
        self._log_event(task_id, TaskEvent(
            event_type="started",
            task_id=task_id,
            worker_id=self._worker_id,
        ))

    def mark_completed(self, task_id: str, result: str = "") -> None:
        self._log_event(task_id, TaskEvent(
            event_type="completed",
            task_id=task_id,
            worker_id=self._worker_id,
            data={"result": result[:500]},
        ))
        logger.info("task_queue.completed", task_id=task_id)

    def mark_failed(self, task_id: str, error: str = "") -> None:
        self._log_event(task_id, TaskEvent(
            event_type="failed",
            task_id=task_id,
            worker_id=self._worker_id,
            data={"error": error[:500]},
        ))
        logger.error("task_queue.failed", task_id=task_id, error=error[:200])

    def mark_recovered(self, task_id: str) -> None:
        self._log_event(task_id, TaskEvent(
            event_type="recovered",
            task_id=task_id,
            worker_id=self._worker_id,
        ))

    def recover_state(self, task_id: str) -> TaskState:
        """Reconstrói estado completo a partir do log de eventos."""
        events = self._get_events(task_id)
        return TaskState.replay(task_id, events)

    def get_unfinished_tasks(self) -> list[TaskState]:
        """
        Encontra tarefas que ficaram inacabadas (após restart).
        Retorna estados reconstruídos.
        """
        # Read pending entries that were claimed but not acked
        try:
            pending = self._redis.xpending_range(
                self.PENDING_STREAM,
                self.GROUP_NAME,
                min="-",
                max="+",
                count=50,
            )

            unfinished = []
            for entry in pending:
                # entry has: message_id, consumer, idle_time, delivery_count
                msg_id = entry["message_id"]

                # Read the original message
                messages = self._redis.xrange(self.PENDING_STREAM, min=msg_id, max=msg_id)
                if messages:
                    _, fields = messages[0]
                    task_id = fields.get("task_id", "")
                    if task_id:
                        state = self.recover_state(task_id)
                        if state.status not in ("completed", "failed"):
                            unfinished.append(state)

            return unfinished

        except Exception as e:
            logger.error("task_queue.recover_failed", error=str(e))
            return []

    def _log_event(self, task_id: str, event: TaskEvent) -> None:
        """Persist event to task-specific stream."""
        try:
            self._redis.xadd(
                f"task:{task_id}:events",
                event.to_redis(),
            )
        except Exception as e:
            logger.error("task_queue.event_log_failed", task_id=task_id, error=str(e))

    def _get_events(self, task_id: str) -> list[TaskEvent]:
        """Read all events for a task."""
        try:
            raw_events = self._redis.xrange(f"task:{task_id}:events")
            return [TaskEvent.from_redis(fields) for _, fields in raw_events]
        except Exception as e:
            logger.error("task_queue.read_events_failed", task_id=task_id, error=str(e))
            return []

    def health_check(self) -> bool:
        """Check Redis connectivity."""
        try:
            return self._redis.ping()
        except Exception:
            return False
