"""Synchronous local CLI adapter for use by a future worker, not an HTTP loop."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from uuid import uuid4

from .base import ExecutionRequest, ExecutionResult, Provider, ProviderInfo


class CodexProvider(Provider):
    name = 'codex'
    capabilities = ('text', 'code_analysis')

    def __init__(self, binary: str = 'codex', model: str | None = None,
                 output_limit_bytes: int = 1_048_576):
        if output_limit_bytes < 1:
            raise ValueError('output_limit_bytes must be positive')
        self.binary = binary
        self.model = model
        self.output_limit_bytes = output_limit_bytes

    def inspect(self) -> ProviderInfo:
        available = shutil.which(self.binary) is not None
        return ProviderInfo(
            name=self.name, model=self.model, capabilities=list(self.capabilities),
            available=available, status='available' if available else 'unavailable',
            detail=('CLI installed; authentication and model access are not checked'
                    if available else 'CLI not found in this runtime'),
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        started = time.monotonic()
        result = ExecutionResult(
            execution_id=str(uuid4()), provider=self.name,
            model=request.model or self.model, status='failed',
            started_at=datetime.now(timezone.utc), duration_seconds=0,
            metadata={'task_id': request.task_id, 'agent_id': request.agent_id,
                      'parent_execution_id': request.parent_execution_id,
                      'sandbox': 'read-only', 'approval_policy': 'never'},
        )
        binary = shutil.which(self.binary)
        workspace = Path(request.workspace).resolve()
        if not binary:
            result.status = 'unavailable'
            result.metadata['error'] = 'cli_not_found'
        elif not workspace.is_dir():
            result.metadata['error'] = 'invalid_workspace'
        else:
            command = [binary, '--ask-for-approval', 'never', 'exec',
                       '--sandbox', 'read-only', '--color', 'never',
                       '--ephemeral', '--json', '--cd', str(workspace)]
            if result.model:
                command.extend(['--model', result.model])
            command.append('-')  # Prompt is stdin, never a shell command or argv.
            # Do not pass application DB/Redis credentials into the child process.
            allowed = ('PATH', 'HOME', 'CODEX_HOME', 'OPENAI_API_KEY', 'CODEX_API_KEY',
                       'LANG', 'LC_ALL', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR')
            environment = {key: os.environ[key] for key in allowed if key in os.environ}
            # Spool output to disk to avoid pipe deadlocks and unbounded RAM usage.
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                try:
                    process = subprocess.Popen(
                        command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                        cwd=workspace, env=environment, start_new_session=True,
                    )
                    try:
                        process.communicate(request.prompt.encode(), timeout=request.timeout_seconds)
                        result.status = 'succeeded' if process.returncode == 0 else 'failed'
                    except subprocess.TimeoutExpired:
                        self._kill(process)
                        process.communicate()
                        result.status = 'timed_out'
                    except BaseException:
                        self._kill(process)
                        process.communicate()
                        raise
                    result.exit_code = process.returncode
                except OSError:
                    result.metadata['error'] = 'process_start_failed'
                for name, stream in (('stdout', stdout), ('stderr', stderr)):
                    size = stream.tell()
                    stream.seek(0)
                    setattr(result, name, stream.read(self.output_limit_bytes).decode('utf-8', errors='replace'))
                    result.metadata[f'{name}_bytes'] = size
                    result.metadata[f'{name}_truncated'] = size > self.output_limit_bytes
        result.duration_seconds = time.monotonic() - started
        return result

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
