from __future__ import annotations

import asyncio
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings
from app.core.logging import redact_credentials
from app.integrations.security_gemini import GeminiSecurityReviewer
from app.integrations.validation_runner import compute_patch_fingerprint
from app.models.task import Task
from app.repositories.records import WorkflowRepositories
from app.schemas.security import (
    FindingSeverity,
    FindingSource,
    ModelSecurityOutput,
    SecurityFinding,
    SecurityReview,
    SecurityStatus,
)
from app.schemas.validation import ValidationReport, ValidationStatus

_CREDENTIAL_NAME = re.compile(
    r"(?i)(^|/)(\.env(?:\.|$)|id_rsa|id_ed25519|credentials?\.json|secrets?\.)"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
_TOKEN = re.compile(
    r"(?i)(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AIza[0-9A-Za-z_-]{30,}|AKIA[0-9A-Z]{16})"
)
_DANGEROUS = re.compile(r"\b(?:verify\s*=\s*False|shell\s*=\s*True|eval\s*\(|exec\s*\()")


def _safe_review_text(value: str, limit: int) -> str:
    return _TOKEN.sub("[REDACTED]", redact_credentials(value))[:limit]


class SecurityService:
    def __init__(self, settings: Settings, records: WorkflowRepositories, reviewer=None) -> None:
        self.settings, self.records = settings, records
        self.reviewer = reviewer or GeminiSecurityReviewer(settings)

    async def review(
        self, task: Task, execution, workspace: Path, validation: ValidationReport
    ) -> SecurityReview:
        started = datetime.now(timezone.utc)
        if (
            validation.overall_status != ValidationStatus.PASSED
            or validation.patch_fingerprint != execution.patch_fingerprint
        ):
            raise ValueError("security review requires a matching passing validation report")
        if compute_patch_fingerprint(workspace) != execution.patch_fingerprint:
            raise ValueError("workspace changed after validation")
        proc = await asyncio.to_thread(
            subprocess.run,
            ["git", "-c", "core.pager=cat", "diff", "--no-ext-diff", "--unified=0", "--", "."],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
        )
        if proc.returncode:
            raise ValueError("could not inspect the workspace diff")
        diff = proc.stdout[:100_000]
        findings: list[SecurityFinding] = []
        current_path = None
        line_no = None
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                current_path = line[6:]
                if _CREDENTIAL_NAME.search(current_path):
                    findings.append(
                        self._finding(
                            FindingSeverity.CRITICAL,
                            current_path,
                            None,
                            "Credential-like path was added.",
                            "A credential file must not be committed.",
                            "Remove the file and rotate any exposed credential.",
                        )
                    )
            elif line.startswith("@@"):
                match = re.search(r"\+(\d+)", line)
                line_no = int(match.group(1)) if match else None
            elif line.startswith("+") and not line.startswith("+++"):
                content = line[1:]
                if _PRIVATE_KEY.search(content) or _TOKEN.search(content):
                    findings.append(
                        self._finding(
                            FindingSeverity.CRITICAL,
                            current_path,
                            line_no,
                            "A credential-like value was detected in added content.",
                            "The patch appears to contain a credential.",
                            "Remove it and rotate the credential.",
                        )
                    )
                elif _DANGEROUS.search(content):
                    findings.append(
                        self._finding(
                            FindingSeverity.HIGH,
                            current_path,
                            line_no,
                            "A risky execution or TLS pattern was added.",
                            "This pattern can bypass normal safety checks.",
                            "Use a safer API and justify any unavoidable exception.",
                        )
                    )
                if line_no is not None:
                    line_no += 1
        limitations = ["Static checks are pattern based and can miss context-dependent issues."]
        if any(f.severity == FindingSeverity.CRITICAL for f in findings):
            report = self._report(
                task.id,
                execution.id,
                execution.patch_fingerprint,
                started,
                findings,
                limitations,
                SecurityStatus.FINDINGS,
                "Critical static security finding detected.",
            )
        else:
            try:
                output = await self.reviewer.review(
                    task, diff, [r.status.value for r in validation.commands]
                )
                if not isinstance(output, ModelSecurityOutput):
                    output = ModelSecurityOutput.model_validate(output)
                findings.extend(
                    SecurityFinding(
                        source=FindingSource.MODEL,
                        severity=f.severity,
                        file_path=_safe_review_text(f.file_path, 500) if f.file_path else None,
                        line=f.line,
                        evidence=_safe_review_text(f.evidence, 5000),
                        explanation=_safe_review_text(f.explanation, 4000),
                        remediation=_safe_review_text(f.remediation, 4000),
                        confirmed=False,
                    )
                    for f in output.findings
                )
                limitations.extend(
                    _safe_review_text(item, 1000) for item in output.review_limitations
                )
                if len(findings) > 50:
                    raise ValueError("too many combined findings")
                status = (
                    SecurityStatus.FINDINGS
                    if any(
                        f.severity in {FindingSeverity.HIGH, FindingSeverity.CRITICAL}
                        for f in findings
                    )
                    else SecurityStatus.PASSED
                )
                report = self._report(
                    task.id,
                    execution.id,
                    execution.patch_fingerprint,
                    started,
                    findings,
                    limitations[:12],
                    status,
                    "Security review completed.",
                )
            except Exception:
                report = self._report(
                    task.id,
                    execution.id,
                    execution.patch_fingerprint,
                    started,
                    findings,
                    limitations,
                    SecurityStatus.BLOCKED,
                    "Contextual security review was unavailable or invalid.",
                )
        if compute_patch_fingerprint(workspace) != execution.patch_fingerprint:
            report = report.model_copy(
                update={
                    "overall_status": SecurityStatus.BLOCKED,
                    "summary": "Patch changed during security review.",
                }
            )
        await self.records.security_reviews.create(report)
        return report

    def _finding(self, severity, path, line, evidence, explanation, remediation):
        return SecurityFinding(
            source=FindingSource.STATIC_CHECK,
            severity=severity,
            file_path=path,
            line=line,
            evidence=evidence,
            explanation=explanation,
            remediation=remediation,
            confirmed=True,
        )

    def _report(
        self, task_id, execution_id, fingerprint, started, findings, limitations, status, summary
    ):
        return SecurityReview(
            id=uuid4().hex,
            task_id=task_id,
            execution_id=execution_id,
            patch_fingerprint=fingerprint,
            overall_status=status,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            findings=findings,
            review_limitations=limitations[:12],
            summary=summary,
        )
