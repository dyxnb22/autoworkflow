"""Tests for reviewer prompt structure and cache-friendly ordering."""

from __future__ import annotations

from cc_loop.config import merge_config
from cc_loop.run import (
    DYNAMIC_REVIEW_MARKER,
    TASK_REVIEW_CONTEXT_MARKER,
    build_reviewer_prompt,
    build_reviewer_prompt_metrics,
    summarize_diff_stat,
)
from cc_loop.state import AttemptRecord, TaskState, TaskStatus, plan_artifact_paths


def _state(goal: str = "Build the thing", *, config: dict | None = None) -> TaskState:
    return TaskState(
        task_id="task-a",
        goal=goal,
        target_repo="/repo",
        base_branch="main",
        base_commit="base-a",
        status=TaskStatus.RUNNING,
        iteration=1,
        config=merge_config(config or {}),
        providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
    )


def _attempt(
    *,
    iteration: int = 1,
    base_commit: str = "base-a",
    head_commit: str = "head-a",
) -> AttemptRecord:
    return AttemptRecord(
        iteration=iteration,
        retry=0,
        created_at="2026-07-02T00:00:00+00:00",
        base_commit=base_commit,
        head_commit=head_commit,
        implementer_exit_code=0,
    )


def test_reviewer_prompt_keeps_stable_contract_before_dynamic_payload() -> None:
    prompt = build_reviewer_prompt(
        state=_state(),
        attempt=_attempt(),
        diff_stat=" README.md | 1 +",
        patch_body="diff --git a/README.md b/README.md\n",
        test_status="passed",
    )

    stable_index = prompt.index("## Stable Review Contract")
    rubric_index = prompt.index("## Stable Review Rubric")
    json_index = prompt.index("## Stable JSON Output Contract")
    task_context_index = prompt.index(TASK_REVIEW_CONTEXT_MARKER)
    payload_index = prompt.index(DYNAMIC_REVIEW_MARKER)
    metadata_index = prompt.index("### Attempt metadata")
    diff_index = prompt.index("### Diff stat")
    patch_index = prompt.index("### Selected patches")

    assert stable_index < rubric_index < json_index < task_context_index < payload_index
    assert payload_index < metadata_index < diff_index < patch_index
    assert prompt.index('"decision": "approve"') < payload_index
    assert prompt.index("Task goal: Build the thing") < payload_index
    assert prompt.index("- Iteration: 1") > payload_index
    assert prompt.index("diff --git") > patch_index
    assert "diff --git" not in prompt[:payload_index]
    assert "### Test result" in prompt[payload_index:]


def test_reviewer_prompt_task_context_stable_across_attempts() -> None:
    first = build_reviewer_prompt(
        state=_state(goal="First goal"),
        attempt=_attempt(iteration=1, base_commit="base-a", head_commit="head-a"),
        diff_stat=" first.txt | 1 +",
        patch_body="diff --git a/first.txt b/first.txt\n",
        test_status="passed",
    )
    second = build_reviewer_prompt(
        state=_state(goal="Second goal"),
        attempt=_attempt(iteration=2, base_commit="base-b", head_commit="head-b"),
        diff_stat=" second.txt | 2 ++",
        patch_body="diff --git a/second.txt b/second.txt\n",
        test_status="failed",
    )

    first_task_context = first[first.index(TASK_REVIEW_CONTEXT_MARKER) : first.index(DYNAMIC_REVIEW_MARKER)]
    second_task_context = second[second.index(TASK_REVIEW_CONTEXT_MARKER) : second.index(DYNAMIC_REVIEW_MARKER)]

    assert first_task_context != second_task_context
    assert "First goal" in first_task_context
    assert "Second goal" in second_task_context

    first_contract = first[: first.index(TASK_REVIEW_CONTEXT_MARKER)]
    second_contract = second[: second.index(TASK_REVIEW_CONTEXT_MARKER)]
    assert first_contract == second_contract


def test_reviewer_prompt_metrics_describe_cache_layout() -> None:
    diff_stat = " README.md | 1 +"
    patch_body = "diff --git a/README.md b/README.md\n"
    prompt = build_reviewer_prompt(
        state=_state(),
        attempt=_attempt(),
        diff_stat=diff_stat,
        patch_body=patch_body,
        test_status="passed",
    )

    metrics = build_reviewer_prompt_metrics(
        prompt=prompt,
        diff_stat=diff_stat,
        patch_body=patch_body,
    )

    assert metrics["schema_version"] == 1
    assert metrics["layout"] == "stable-prefix-v1"
    assert metrics["prompt_chars"] == len(prompt)
    assert metrics["stable_prefix_chars"] == prompt.index(DYNAMIC_REVIEW_MARKER)
    assert metrics["task_context_chars"] > 0
    assert metrics["task_context_ratio"] > 0
    assert metrics["contract_prefix_chars"] == prompt.index(TASK_REVIEW_CONTEXT_MARKER)
    assert metrics["dynamic_payload_chars"] == len(prompt) - metrics["stable_prefix_chars"]
    assert metrics["stable_prefix_ratio"] > 0.35
    assert metrics["cache_health"] == "good"
    assert metrics["patch_body_chars"] == len(patch_body)
    assert metrics["diff_stat_chars"] == len(diff_stat)
    assert metrics["estimated_prompt_tokens"] == (len(prompt) + 3) // 4


def test_artifact_refs_reviewer_omits_full_diff_stat() -> None:
    diff_lines = [f" file-{idx}.py | {idx} +++" for idx in range(30)]
    diff_lines.append(" 30 files changed, 120 insertions(+), 5 deletions(-)")
    diff_stat = "\n".join(diff_lines)
    config = merge_config({"review_context_mode": "artifact_refs"})
    paths = plan_artifact_paths(__import__("pathlib").Path("/tmp/reviewer-diff-stat-test"))
    prompt = build_reviewer_prompt(
        state=_state(config={"review_context_mode": "artifact_refs"}),
        attempt=_attempt(),
        diff_stat=diff_stat,
        patch_body="x" * 9000,
        test_status="passed",
        config=config,
        artifact_paths=paths,
        patch_paths=[],
        inline_patch=False,
        context_mode="artifact_refs",
    )

    assert "### Diff stat artifact" in prompt
    assert "Full diff stat:" in prompt
    assert str(paths["diff_stat"]) in prompt
    assert str(paths["diff_files"]) in prompt
    assert "Changed files: 30" in prompt
    assert "Preview:" not in prompt
    assert "file-29.py" not in prompt
    assert "30 files changed" not in prompt


def test_inline_reviewer_still_inlines_diff_stat() -> None:
    diff_stat = " README.md | 1 +\n 1 file changed, 1 insertion(+)"
    prompt = build_reviewer_prompt(
        state=_state(),
        attempt=_attempt(),
        diff_stat=diff_stat,
        patch_body="diff --git a/README.md b/README.md\n",
        test_status="passed",
        context_mode="inline",
        inline_patch=True,
    )
    assert "### Diff stat\n" in prompt
    assert "1 file changed" in prompt
    assert "### Diff stat summary" not in prompt


def test_summarize_diff_stat_parses_counts() -> None:
    diff_stat = " a.py | 2 ++\n b.py | 1 +\n 2 files changed, 3 insertions(+), 1 deletion(-)"
    summary = summarize_diff_stat(diff_stat)
    assert summary["changed_file_count"] == 2
    assert summary["inserted_lines"] == 3
    assert summary["deleted_lines"] == 1
    assert len(summary["preview_lines"]) <= 20


def test_review_prompt_metrics_artifact_path_is_part_of_attempt_contract(tmp_path) -> None:
    paths = plan_artifact_paths(tmp_path)

    assert paths["review_prompt_metrics"] == tmp_path / "review.prompt.metrics.json"
