"""
SimpleClaw v2.0 - Task Executor (Resilient Agent Loop)
========================================================
State machine com transições validadas, error boundary com
classificação de severidade, recovery automático, checkpoint
por etapa, e verificação pós-execução.

Fluxo:
  IDLE → ANALYZING → PLANNING → EXECUTING → VERIFYING → COMPLETED
                                    ↓              ↓
                                  FAILED     ROLLING_BACK
                                    ↓              ↓
                                RECOVERING    RECOVERING
                                    ↓              ↓
                                 PLANNING      PLANNING (replano)
                                    or             or
                                 ESCALATED     ESCALATED (humano)
"""

from __future__ import annotations

import asyncio
import traceback
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import structlog

from src.config.settings import get_settings
from src.tools.git_checkpoint import GitCheckpoint

logger = structlog.get_logger()


# ─── STATE MACHINE ──────────────────────────────────────────

class TaskState(str, Enum):
    IDLE = "idle"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    RECOVERING = "recovering"
    ESCALATED = "escalated"


# Transições válidas — imutável
VALID_TRANSITIONS: dict[TaskState, list[TaskState]] = {
    TaskState.IDLE:         [TaskState.ANALYZING],
    TaskState.ANALYZING:    [TaskState.PLANNING, TaskState.FAILED],
    TaskState.PLANNING:     [TaskState.EXECUTING, TaskState.FAILED],
    TaskState.EXECUTING:    [TaskState.VERIFYING, TaskState.FAILED],
    TaskState.VERIFYING:    [TaskState.COMPLETED, TaskState.ROLLING_BACK],
    TaskState.ROLLING_BACK: [TaskState.RECOVERING, TaskState.FAILED],
    TaskState.RECOVERING:   [TaskState.PLANNING, TaskState.ESCALATED],
    TaskState.FAILED:       [TaskState.RECOVERING, TaskState.ESCALATED],
    TaskState.ESCALATED:    [TaskState.IDLE],
    TaskState.COMPLETED:    [TaskState.IDLE],
}


class InvalidTransition(Exception):
    pass


# ─── ERROR CLASSIFICATION ───────────────────────────────────

class ErrorSeverity(str, Enum):
    TRANSIENT = "transient"      # Timeout, rate limit → retry
    RECOVERABLE = "recoverable"  # Port conflict, missing dep → plan B
    SEVERE = "severe"            # Data corruption → rollback
    CRITICAL = "critical"        # Unrecoverable → escalate to human


def classify_error(error: Exception) -> ErrorSeverity:
    """Classify error severity based on type and message."""
    msg = str(error).lower()

    # Transient — retry usually fixes
    transient_signals = [
        "timeout", "timed out", "rate limit", "429", "503",
        "connection reset", "broken pipe", "temporary",
    ]
    if any(s in msg for s in transient_signals):
        return ErrorSeverity.TRANSIENT

    # Recoverable — needs alternative approach
    recoverable_signals = [
        "address already in use", "port", "already exists",
        "permission denied", "no such file", "not found",
        "missing", "dependency", "module", "import",
        "could not connect", "connection refused",
    ]
    if any(s in msg for s in recoverable_signals):
        return ErrorSeverity.RECOVERABLE

    # Severe — data at risk
    severe_signals = [
        "corrupt", "integrity", "foreign key", "constraint",
        "deadlock", "serialization", "out of memory", "disk full",
    ]
    if any(s in msg for s in severe_signals):
        return ErrorSeverity.SEVERE

    # Default to recoverable (optimistic)
    return ErrorSeverity.RECOVERABLE


# ─── EXECUTION CONTEXT ──────────────────────────────────────

class StepResult:
    """Result of a single execution step."""

    def __init__(
        self,
        step_name: str,
        success: bool,
        output: str = "",
        error: Optional[Exception] = None,
        fallback_used: Optional[str] = None,
        checkpoint_hash: Optional[str] = None,
    ):
        self.step_name = step_name
        self.success = success
        self.output = output
        self.error = error
        self.fallback_used = fallback_used
        self.checkpoint_hash = checkpoint_hash
        self.timestamp = datetime.now(timezone.utc)


class TaskContext:
    """Mutable context for a running task. Carries state across steps."""

    def __init__(self, task_id: str, user_id: str, chat_id: int = 0):
        self.task_id = task_id
        self.user_id = user_id
        self.chat_id = chat_id
        self.state = TaskState.IDLE
        self.steps_completed: list[StepResult] = []
        self.recovery_attempts = 0
        self.max_recoveries = 3
        self.last_error: Optional[Exception] = None
        self.last_severity: Optional[ErrorSeverity] = None
        self.plan: list[dict] = []
        self.final_output: str = ""
        self.work_dir: Optional[Path] = None

    def transition(self, to_state: TaskState) -> None:
        """Validate and execute state transition."""
        valid = VALID_TRANSITIONS.get(self.state, [])
        if to_state not in valid:
            raise InvalidTransition(
                f"Transição ilegal: {self.state.value} → {to_state.value}. "
                f"Válidas: {[s.value for s in valid]}"
            )
        logger.info(
            "task.transition",
            task_id=self.task_id,
            from_state=self.state.value,
            to_state=to_state.value,
        )
        self.state = to_state

    def add_step_result(self, result: StepResult) -> None:
        self.steps_completed.append(result)

    def get_progress_summary(self) -> str:
        """Generate human-readable progress."""
        completed = [s for s in self.steps_completed if s.success]
        failed = [s for s in self.steps_completed if not s.success]
        lines = []
        for s in completed:
            suffix = f" (alternativa: {s.fallback_used})" if s.fallback_used else ""
            lines.append(f"✓ {s.step_name}{suffix}")
        for s in failed:
            lines.append(f"✗ {s.step_name}")
        return "\n".join(lines) if lines else "Nenhuma etapa concluída ainda."


# ─── TASK EXECUTOR ──────────────────────────────────────────

class TaskExecutor:
    """
    Orchestrates task execution with the Resilient Agent Loop.
    
    Integrates:
    - State machine (validated transitions)
    - Error boundary (classifies and handles every exception)
    - Recovery engine (plan B generation)
    - Git checkpoints (per-step, rollbackable)
    - Post-execution verification
    - User notification at each step
    """

    def __init__(self, specialist_manager, router_agent):
        self._specialist = specialist_manager
        self._router = router_agent
        self._settings = get_settings()
        self._notify_callback: Optional[Callable] = None

    def set_notify_callback(self, callback: Callable) -> None:
        self._notify_callback = callback

    async def _notify(self, chat_id: int, message: str) -> None:
        if self._notify_callback and chat_id:
            try:
                await self._notify_callback(chat_id, message)
            except Exception:
                pass

    async def execute(
        self,
        request: str,
        user_id: str,
        session_id: str,
        chat_id: int = 0,
    ) -> str:
        """
        Execute a user request through the full resilient loop.
        
        Returns:
            Final result text for the user.
        """
        task_id = str(uuid.uuid4())[:8]
        ctx = TaskContext(task_id=task_id, user_id=user_id, chat_id=chat_id)

        # Setup workspace
        ctx.work_dir = Path(self._settings.context_base_path) / "processing" / task_id
        ctx.work_dir.mkdir(parents=True, exist_ok=True)

        git = GitCheckpoint(ctx.work_dir)
        git.init_repo()

        try:
            # ── PHASE 1: ANALYZING ──
            ctx.transition(TaskState.ANALYZING)
            await self._notify(chat_id, "🔍 Analisando requisitos...")

            spec = await self._router.generate_task_spec(request, user_id)
            git.checkpoint("Análise concluída - spec gerada")

            ctx.add_step_result(StepResult("Análise de requisitos", True, output="Spec gerada"))

            # ── PHASE 2: PLANNING ──
            ctx.transition(TaskState.PLANNING)
            await self._notify(chat_id, "📋 Planejando execução...")

            # The spec IS the plan - the Team will coordinate internally
            git.checkpoint("Plano de execução definido")
            ctx.add_step_result(StepResult("Planejamento", True))

            # ── PHASE 3: EXECUTING ──
            ctx.transition(TaskState.EXECUTING)
            await self._notify(chat_id, "⚙️ Executando com equipe especialista...")

            result = await self._execute_with_recovery(ctx, spec, user_id, session_id, git)

            # ── PHASE 4: VERIFYING ──
            ctx.transition(TaskState.VERIFYING)
            await self._notify(chat_id, "🔎 Verificando resultado...")

            verification = await self._verify_output(ctx, result)

            if verification["passed"]:
                git.checkpoint("Verificação aprovada - tarefa concluída", tag=f"done-{task_id}")
                ctx.transition(TaskState.COMPLETED)
                ctx.final_output = result

                progress = ctx.get_progress_summary()
                await self._notify(chat_id, f"✅ Tarefa concluída!\n\n{progress}")
                return result
            else:
                # Verification failed — rollback and retry
                await self._notify(
                    chat_id,
                    f"⚠️ Verificação detectou problemas: {verification['reason']}\n"
                    "Ajustando abordagem..."
                )
                ctx.transition(TaskState.ROLLING_BACK)
                git.rollback(steps=1)

                ctx.transition(TaskState.RECOVERING)
                return await self._recover_and_replan(
                    ctx, request, user_id, session_id, git,
                    reason=verification["reason"]
                )

        except InvalidTransition as e:
            logger.error("task.invalid_transition", error=str(e), task_id=task_id)
            return f"⚠️ Erro interno de estado: {str(e)}"

        except Exception as e:
            logger.error(
                "task.unhandled_error",
                task_id=task_id,
                error=str(e),
                traceback=traceback.format_exc()[:500],
            )
            return self._escalate(ctx, e)

    async def _execute_with_recovery(
        self,
        ctx: TaskContext,
        spec: dict,
        user_id: str,
        session_id: str,
        git: GitCheckpoint,
    ) -> str:
        """Execute task with automatic recovery on failure."""
        last_error = None

        for attempt in range(1, ctx.max_recoveries + 1):
            try:
                team = await self._specialist.get_team()
                task_description = spec.get("raw_spec", spec.get("original_request", ""))

                if attempt > 1:
                    # Enrich description with error context for recovery
                    task_description = (
                        f"{task_description}\n\n"
                        f"ATENÇÃO: Tentativa anterior falhou com erro: {str(last_error)[:200]}\n"
                        f"Tente uma abordagem alternativa para resolver o problema."
                    )
                    await self._notify(
                        ctx.chat_id,
                        f"🔄 Tentativa {attempt}/{ctx.max_recoveries}. "
                        "Usando abordagem alternativa..."
                    )

                response = team.run(
                    task_description,
                    user_id=user_id,
                    session_id=session_id,
                )
                result = response.content if hasattr(response, "content") else str(response)

                # Success — checkpoint
                git.checkpoint(f"Execução bem-sucedida (tentativa {attempt})")
                ctx.add_step_result(StepResult(
                    f"Execução (tentativa {attempt})",
                    True,
                    output=result[:200],
                    fallback_used=f"plano alternativo" if attempt > 1 else None,
                ))
                return result

            except Exception as e:
                last_error = e
                severity = classify_error(e)
                ctx.last_error = e
                ctx.last_severity = severity

                logger.error(
                    "task.execution_failed",
                    attempt=attempt,
                    severity=severity.value,
                    error=str(e)[:200],
                )

                ctx.add_step_result(StepResult(
                    f"Execução (tentativa {attempt})",
                    False,
                    error=e,
                ))

                if severity == ErrorSeverity.CRITICAL:
                    break  # Don't retry critical errors

                if severity == ErrorSeverity.TRANSIENT:
                    wait = 2 * attempt
                    await self._notify(
                        ctx.chat_id,
                        f"⏳ Problema temporário detectado. Aguardando {wait}s..."
                    )
                    await asyncio.sleep(wait)

                elif severity == ErrorSeverity.RECOVERABLE:
                    # Force team rebuild for different approach
                    self._specialist._team = None
                    await asyncio.sleep(1)

                elif severity == ErrorSeverity.SEVERE:
                    git.rollback(steps=1)
                    self._specialist._team = None
                    await asyncio.sleep(2)

        # All attempts exhausted
        return self._escalate(ctx, last_error)

    async def _verify_output(self, ctx: TaskContext, result: str) -> dict:
        """
        Verify task output quality.
        
        Basic checks:
        - Result is not empty
        - Result is not just an error message
        - Result has reasonable length
        
        Returns dict with 'passed' bool and optional 'reason'.
        """
        if not result or not result.strip():
            return {"passed": False, "reason": "Resultado vazio"}

        error_indicators = [
            "error:", "traceback", "exception", "falhou",
            "não foi possível", "couldn't", "failed to",
        ]
        lower_result = result.lower()
        error_count = sum(1 for s in error_indicators if s in lower_result)

        # If more than half the result seems to be errors
        if error_count >= 3:
            return {"passed": False, "reason": "Resultado contém múltiplos erros"}

        if len(result.strip()) < 20:
            return {"passed": False, "reason": "Resultado muito curto para ser útil"}

        return {"passed": True}

    async def _recover_and_replan(
        self,
        ctx: TaskContext,
        original_request: str,
        user_id: str,
        session_id: str,
        git: GitCheckpoint,
        reason: str = "",
    ) -> str:
        """Attempt recovery by replanning with error context."""
        ctx.recovery_attempts += 1

        if ctx.recovery_attempts > ctx.max_recoveries:
            ctx.transition(TaskState.ESCALATED)
            return self._escalate(ctx, ctx.last_error)

        try:
            ctx.transition(TaskState.PLANNING)

            enriched_request = (
                f"{original_request}\n\n"
                f"CONTEXTO DE RECUPERAÇÃO: A abordagem anterior falhou. "
                f"Motivo: {reason}. "
                f"Tentativas anteriores: {ctx.recovery_attempts}/{ctx.max_recoveries}. "
                f"Use uma abordagem diferente."
            )

            spec = await self._router.generate_task_spec(enriched_request, user_id)
            git.checkpoint(f"Replano após recuperação #{ctx.recovery_attempts}")

            ctx.transition(TaskState.EXECUTING)
            result = await self._execute_with_recovery(ctx, spec, user_id, session_id, git)

            # Re-verify
            ctx.transition(TaskState.VERIFYING)
            verification = await self._verify_output(ctx, result)

            if verification["passed"]:
                git.checkpoint("Recuperação bem-sucedida", tag=f"recovered-{ctx.task_id}")
                ctx.transition(TaskState.COMPLETED)
                return result
            else:
                ctx.transition(TaskState.ROLLING_BACK)
                git.rollback(steps=1)
                ctx.transition(TaskState.RECOVERING)
                return self._escalate(ctx, Exception(verification["reason"]))

        except Exception as e:
            logger.error("task.recovery_failed", error=str(e))
            return self._escalate(ctx, e)

    def _escalate(self, ctx: TaskContext, error: Optional[Exception]) -> str:
        """
        Escalate to human with full context.
        User-friendly message, no raw stack traces.
        """
        progress = ctx.get_progress_summary()
        error_msg = str(error)[:200] if error else "Erro desconhecido"

        logger.warning(
            "task.escalated",
            task_id=ctx.task_id,
            state=ctx.state.value,
            recovery_attempts=ctx.recovery_attempts,
            error=error_msg,
        )

        lines = [
            "⚠️ Preciso da sua ajuda para continuar.\n",
            f"*Problema:* {error_msg}\n",
        ]

        if progress:
            lines.append(f"*Progresso até agora:*\n{progress}\n")

        lines.append(
            "*Sugestões:*\n"
            "• Reformule o pedido com mais detalhes\n"
            "• Divida em partes menores\n"
            "• Verifique se os serviços necessários estão rodando (/health)"
        )

        return "\n".join(lines)
