from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.config import Settings
from app.core.errors import ErrorCode, NexusError
from app.integrations.validation_runner import (
    AntigravityValidationRunner,
    compute_patch_fingerprint,
)
from app.models.task import Task, TaskStatus
from app.repositories.records import WorkflowRepositories
from app.schemas.execution import ExecutionRecord, ExecutionStatus
from app.schemas.validation import ValidationReport, ValidationStatus


class ValidationService:
    def __init__(
        self, settings: Settings, tasks, records: WorkflowRepositories, runner=None
    ) -> None:
        self.settings, self.tasks, self.records = settings, tasks, records
        self.runner = runner or AntigravityValidationRunner(settings)

    async def cancel_execution(self, execution_id: str) -> None:
        cancel = getattr(self.runner, "cancel_execution", None)
        if cancel is not None:
            await asyncio.to_thread(cancel, execution_id)

    async def validate(
        self, task: Task, execution: ExecutionRecord, workspace: Path
    ) -> ValidationReport:
        if task.status != TaskStatus.VALIDATING or execution.status != ExecutionStatus.RUNNING:
            raise NexusError(
                ErrorCode.REPORT_CONFLICT, "Execution is not ready for validation.", status_code=409
            )
        if not execution.patch_fingerprint or not execution.repository_key:
            raise NexusError(
                ErrorCode.REPORT_CONFLICT, "Execution has no verified patch.", status_code=409
            )
        if task.repository_key != execution.repository_key:
            raise NexusError(
                ErrorCode.REPORT_CONFLICT,
                "Task and execution repositories differ.",
                status_code=409,
            )
        begin = getattr(self.runner, "begin_execution", None)
        if begin is not None:
            begin(execution.id)
        report = await asyncio.to_thread(
            self.runner.run,
            task,
            execution,
            workspace,
            expected_fingerprint=execution.patch_fingerprint,
        )
        await self.records.validation_reports.create(report)
        if report.overall_status == ValidationStatus.PASSED:
            if compute_patch_fingerprint(workspace) != execution.patch_fingerprint:
                raise NexusError(
                    ErrorCode.REPORT_CONFLICT, "Patch changed after validation.", status_code=409
                )
        return report
