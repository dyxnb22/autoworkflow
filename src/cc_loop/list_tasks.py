"""List tasks under a cc-loop state root."""

from __future__ import annotations

from pathlib import Path

from cc_loop.inspect import state_mtime_iso
from cc_loop.state import load_state


def iter_tasks(state_root: Path, *, repo: Path | None = None) -> list[dict]:
    tasks_dir = state_root / "tasks"
    if not tasks_dir.is_dir():
        return []

    resolved_repo = repo.expanduser().resolve() if repo is not None else None
    items: list[dict] = []

    for candidate in sorted(tasks_dir.iterdir()):
        if not candidate.is_dir():
            continue
        path = candidate / "state.json"
        if not path.is_file():
            continue
        state = load_state(candidate.name, state_root)
        target = Path(state.target_repo).resolve()
        if resolved_repo is not None and target != resolved_repo:
            continue
        attempt = state.history[-1] if state.history else None
        phase = attempt.phase.value if attempt is not None else "-"
        items.append(
            {
                "task_id": state.task_id,
                "status": state.status.value,
                "target_repo": str(target),
                "phase": phase,
                "updated_at": state_mtime_iso(state_root, state.task_id),
                "goal": state.goal,
                "iteration": state.iteration,
            }
        )

    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return items


def format_task_line(item: dict) -> str:
    return "\t".join(
        [
            item["task_id"],
            item["status"],
            item["target_repo"],
            item["phase"],
            item["updated_at"],
        ]
    )
