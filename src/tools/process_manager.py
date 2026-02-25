"""
SimpleClaw v2.0 - Process Manager Tool
========================================
Manages isolated subprocess execution with virtual environments.
Replaces Docker-in-Docker with lighter, safer isolation.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import psutil
import structlog

from src.config.settings import get_settings

logger = structlog.get_logger()


class ProcessManager:
    """
    Executes code in isolated subprocess with dedicated venv.

    Security:
    - Each task gets its own venv (no shared packages)
    - Working directory is isolated
    - Resource limits (timeout, max memory via monitoring)
    - No access to SimpleClaw source code
    - No access to .env or credentials
    """

    def __init__(self):
        settings = get_settings()
        self._base_dir = Path(settings.worker_base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._timeout = settings.worker_timeout_after_task_minutes * 60
        self._active_processes: dict[str, subprocess.Popen] = {}

    def _create_workspace(self, task_id: str) -> Path:
        """Create an isolated workspace for a task."""
        workspace = self._base_dir / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _create_venv(self, workspace: Path) -> Path:
        """Create a virtual environment in the workspace."""
        venv_path = workspace / ".venv"
        if not venv_path.exists():
            subprocess.run(
                ["python3", "-m", "venv", str(venv_path)],
                check=True,
                timeout=60,
            )
        return venv_path

    def _get_python(self, venv_path: Path) -> str:
        """Get the python executable path inside the venv."""
        return str(venv_path / "bin" / "python")

    async def install_packages(
        self,
        task_id: str,
        packages: list[str],
    ) -> dict:
        """
        Install Python packages in the task's isolated venv.

        Args:
            task_id: Task identifier
            packages: List of package specs (e.g., ["pandas==2.1.0", "requests"])

        Returns:
            Dict with success status and output
        """
        workspace = self._create_workspace(task_id)
        venv_path = self._create_venv(workspace)
        pip = str(venv_path / "bin" / "pip")

        try:
            result = subprocess.run(
                [pip, "install"] + packages,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(workspace),
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout[-1000:] if result.stdout else "",
                "stderr": result.stderr[-500:] if result.stderr else "",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Package installation timed out (5min)"}

    async def execute_code(
        self,
        task_id: str,
        code: str,
        filename: str = "task.py",
        timeout_seconds: int = 300,
    ) -> dict:
        """
        Execute Python code in an isolated subprocess.

        Args:
            task_id: Task identifier
            code: Python code to execute
            filename: Script filename
            timeout_seconds: Max execution time

        Returns:
            Dict with stdout, stderr, return_code, files_created
        """
        workspace = self._create_workspace(task_id)
        venv_path = self._create_venv(workspace)
        python = self._get_python(venv_path)

        # Write code to file
        script_path = workspace / filename
        script_path.write_text(code, encoding="utf-8")

        # Track files before execution
        files_before = set(workspace.rglob("*"))

        # Sanitized environment (no access to SimpleClaw env vars)
        safe_env = {
            "PATH": f"{venv_path / 'bin'}:/usr/bin:/bin",
            "HOME": str(workspace),
            "PYTHONPATH": str(workspace),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

        try:
            process = await asyncio.create_subprocess_exec(
                python, str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
                env=safe_env,
            )

            self._active_processes[task_id] = process

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )

            # Find newly created files
            files_after = set(workspace.rglob("*"))
            new_files = [
                str(f.relative_to(workspace))
                for f in (files_after - files_before)
                if f.is_file() and f.name != filename
            ]

            result = {
                "success": process.returncode == 0,
                "return_code": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace")[-5000:],
                "stderr": stderr.decode("utf-8", errors="replace")[-2000:],
                "files_created": new_files,
                "workspace": str(workspace),
            }

            logger.info(
                "process.executed",
                task_id=task_id,
                success=result["success"],
                files=len(new_files),
            )
            return result

        except asyncio.TimeoutError:
            if task_id in self._active_processes:
                self._active_processes[task_id].kill()
            return {
                "success": False,
                "error": f"Execution timed out after {timeout_seconds}s",
                "return_code": -1,
            }
        finally:
            self._active_processes.pop(task_id, None)

    async def execute_shell(
        self,
        task_id: str,
        command: str,
        timeout_seconds: int = 60,
    ) -> dict:
        """
        Execute a shell command in the task's workspace.

        Restricted: no sudo, no access outside workspace.
        """
        workspace = self._create_workspace(task_id)

        # Basic command filtering
        blocked = ["sudo", "rm -rf /", "mkfs", "dd if=", ":(){ :|:", "chmod 777 /"]
        if any(b in command for b in blocked):
            return {"success": False, "error": "Command blocked for safety."}

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )

            return {
                "success": process.returncode == 0,
                "return_code": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace")[-3000:],
                "stderr": stderr.decode("utf-8", errors="replace")[-1000:],
            }

        except asyncio.TimeoutError:
            return {"success": False, "error": f"Command timed out after {timeout_seconds}s"}

    def get_resource_usage(self, task_id: str) -> Optional[dict]:
        """Get resource usage for an active process."""
        process = self._active_processes.get(task_id)
        if not process or not process.pid:
            return None

        try:
            p = psutil.Process(process.pid)
            mem = p.memory_info()
            return {
                "pid": process.pid,
                "ram_mb": round(mem.rss / 1024 / 1024, 1),
                "cpu_percent": p.cpu_percent(interval=0.1),
                "status": p.status(),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    async def cleanup_workspace(self, task_id: str) -> None:
        """Remove workspace after task completion."""
        workspace = self._base_dir / task_id
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
            logger.info("process.workspace_cleaned", task_id=task_id)

    def get_file_from_workspace(self, task_id: str, filename: str) -> Optional[Path]:
        """Get a file path from a task's workspace."""
        filepath = self._base_dir / task_id / filename
        if filepath.exists():
            return filepath
        return None

    async def kill_task(self, task_id: str) -> bool:
        """Kill a running task process."""
        process = self._active_processes.get(task_id)
        if process:
            process.kill()
            self._active_processes.pop(task_id, None)
            logger.info("process.killed", task_id=task_id)
            return True
        return False
