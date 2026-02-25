"""
SimpleClaw v2.0 - Git Checkpoint Tool
=======================================
Manages git versioning for task execution.
Each successful step = one commit. Enables granular rollback.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from src.config.settings import get_settings

logger = structlog.get_logger()


class GitCheckpoint:
    """
    Git-based checkpoint system for task execution.

    Creates a git repo in the task's working directory,
    commits after each successful step, enabling rollback.
    """

    def __init__(self, work_dir: Optional[Path] = None):
        settings = get_settings()
        self._work_dir = work_dir or Path(settings.context_base_path) / "processing"
        self._initialized = False

    def _run_git(self, *args: str, cwd: Optional[Path] = None) -> tuple[int, str, str]:
        """Run a git command and return (returncode, stdout, stderr)."""
        try:
            result = subprocess.run(
                ["git"] + list(args),
                cwd=str(cwd or self._work_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            return 1, "", "Git command timed out"
        except FileNotFoundError:
            return 1, "", "Git not found. Install git."

    def init_repo(self, task_dir: Optional[Path] = None) -> bool:
        """Initialize a git repo in the task directory."""
        target = task_dir or self._work_dir
        target.mkdir(parents=True, exist_ok=True)

        code, _, err = self._run_git("init", cwd=target)
        if code != 0:
            logger.error("git.init_failed", error=err)
            return False

        # Configure git user for this repo
        self._run_git("config", "user.email", "simpleclaw@local", cwd=target)
        self._run_git("config", "user.name", "SimpleClaw", cwd=target)

        # Initial commit
        gitignore = target / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("__pycache__/\n*.pyc\n.env\n*.log\n")

        self._run_git("add", "-A", cwd=target)
        self._run_git("commit", "-m", "Initial: task started", "--allow-empty", cwd=target)

        self._initialized = True
        self._work_dir = target
        logger.info("git.initialized", path=str(target))
        return True

    def checkpoint(
        self,
        message: str,
        task_dir: Optional[Path] = None,
        tag: Optional[str] = None,
    ) -> Optional[str]:
        """
        Create a checkpoint (git commit) for the current state.

        Args:
            message: Commit message describing what was done
            task_dir: Override working directory
            tag: Optional git tag for this checkpoint

        Returns:
            Commit hash if successful, None if failed
        """
        target = task_dir or self._work_dir

        # Stage all changes
        self._run_git("add", "-A", cwd=target)

        # Check if there are changes to commit
        code, status, _ = self._run_git("status", "--porcelain", cwd=target)
        if code == 0 and not status:
            logger.debug("git.nothing_to_commit")
            # Still create an empty commit to mark the step
            timestamp = datetime.now().strftime("%H:%M:%S")
            self._run_git(
                "commit", "--allow-empty",
                "-m", f"[{timestamp}] {message}",
                cwd=target,
            )
        else:
            timestamp = datetime.now().strftime("%H:%M:%S")
            code, _, err = self._run_git(
                "commit", "-m", f"[{timestamp}] {message}",
                cwd=target,
            )
            if code != 0:
                logger.error("git.commit_failed", error=err)
                return None

        # Get commit hash
        code, commit_hash, _ = self._run_git("rev-parse", "HEAD", cwd=target)
        if code != 0:
            return None

        # Optional tag
        if tag:
            self._run_git("tag", tag, cwd=target)

        logger.info("git.checkpoint", hash=commit_hash[:8], message=message)
        return commit_hash

    def rollback(
        self,
        steps: int = 1,
        task_dir: Optional[Path] = None,
    ) -> bool:
        """
        Rollback to a previous checkpoint.

        Args:
            steps: Number of commits to go back
            task_dir: Override working directory

        Returns:
            True if rollback succeeded
        """
        target = task_dir or self._work_dir
        code, _, err = self._run_git("reset", "--hard", f"HEAD~{steps}", cwd=target)

        if code != 0:
            logger.error("git.rollback_failed", steps=steps, error=err)
            return False

        logger.info("git.rollback", steps=steps)
        return True

    def rollback_to_commit(
        self,
        commit_hash: str,
        task_dir: Optional[Path] = None,
    ) -> bool:
        """Rollback to a specific commit hash."""
        target = task_dir or self._work_dir
        code, _, err = self._run_git("reset", "--hard", commit_hash, cwd=target)

        if code != 0:
            logger.error("git.rollback_to_failed", hash=commit_hash, error=err)
            return False

        logger.info("git.rollback_to", hash=commit_hash[:8])
        return True

    def get_log(
        self,
        max_entries: int = 10,
        task_dir: Optional[Path] = None,
    ) -> list[dict]:
        """Get recent git log entries."""
        target = task_dir or self._work_dir
        code, output, _ = self._run_git(
            "log", f"--max-count={max_entries}",
            "--format=%H|%s|%ai",
            cwd=target,
        )

        if code != 0 or not output:
            return []

        entries = []
        for line in output.split("\n"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                entries.append({
                    "hash": parts[0][:8],
                    "message": parts[1],
                    "date": parts[2],
                })
        return entries

    def get_diff(
        self,
        task_dir: Optional[Path] = None,
    ) -> str:
        """Get current uncommitted changes."""
        target = task_dir or self._work_dir
        code, diff, _ = self._run_git("diff", cwd=target)
        return diff if code == 0 else ""
