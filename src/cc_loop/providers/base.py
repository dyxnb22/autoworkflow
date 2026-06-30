"""Provider adapter contract for built-in local agents."""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cc_loop.config import LoopConfig


@dataclass
class ProviderRunResult:
    provider: str
    exit_code: int
    raw_artifact_path: Path
    timed_out: bool = False
    interrupted: bool = False
    summary: str = ""


class ProviderAdapter(ABC):
    """Base class for built-in provider adapters."""

    name: str

    @abstractmethod
    def build_args(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
    ) -> list[str]:
        """Build an argv list for subprocess invocation (shell=False)."""

    @abstractmethod
    def run(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
    ) -> ProviderRunResult:
        """Execute the provider and capture raw output artifacts."""

    @abstractmethod
    def parse_planner_output(self, last_message_path: Path) -> dict[str, Any]:
        """Parse planner output into normalized planner JSON."""

    @abstractmethod
    def parse_reviewer_output(self, last_message_path: Path) -> dict[str, Any]:
        """Parse reviewer output into normalized reviewer JSON."""

    @abstractmethod
    def preflight_check_argv(self) -> list[str]:
        """Return argv for a lightweight install/availability check."""

    def preflight_check(self) -> None:
        completed = subprocess.run(
            self.preflight_check_argv(),
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            cmd = " ".join(self.preflight_check_argv())
            stderr = completed.stderr.strip() or completed.stdout.strip()
            detail = f": {stderr}" if stderr else ""
            raise RuntimeError(f"provider preflight check failed for {self.name} ({cmd}){detail}")


PROVIDER_REGISTRY: dict[str, type[ProviderAdapter]] = {}


def register_provider(adapter_cls: type[ProviderAdapter]) -> type[ProviderAdapter]:
    PROVIDER_REGISTRY[adapter_cls.name] = adapter_cls
    return adapter_cls


def get_provider(name: str) -> ProviderAdapter:
    try:
        adapter_cls = PROVIDER_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"unknown provider: {name}") from exc
    return adapter_cls()
