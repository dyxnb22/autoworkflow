"""Tests for reviewer prompt structure and cache-friendly ordering."""

from __future__ import annotations

from cc_loop.config import merge_config
from cc_loop.run import build_reviewer_prompt, build_reviewer_prompt_metrics
from cc_loop.state import AttemptRecord, TaskState, TaskStatus, plan_artifact_paths


def _state(goal: str = "Build the thing") -> TaskState:
    return TaskState(
        task_id="task-a",
        goal=goal,
        target_repo="/repo",
        base_branch="main",
        base_commit="base-a",
        status=TaskStatus.RUNNING,
        iteration=1,
        config=merge_config({}),
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
    payload_index = prompt.index("## Dynamic Review Payload")
    metadata_index = prompt.index("### Attempt metadata")
    diff_index = prompt.index("### Diff stat")
    patch_index = prompt.index("### Selected patches")

    assert stable_index < rubric_index < json_index < payload_index
    assert payload_index < metadata_index < diff_index < patch_index
    assert prompt.index('"decision": "approve"') < payload_index
    assert prompt.index("Task ID: task-a") > payload_index
    assert prompt.index("diff --git") > patch_index
    assert "diff --git" not in prompt[:payload_index]
    assert "### Test result" in prompt[payload_index:]


def test_reviewer_prompt_prefix_is_identical_before_dynamic_payload() -> None:
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

    marker = "## Dynamic Review Payload"
    first_prefix = first[: first.index(marker)]
    second_prefix = second[: second.index(marker)]

    assert first_prefix == second_prefix
    assert "First goal" not in first_prefix
    assert "Second goal" not in second_prefix
    assert len(first_prefix) > 1500


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
    assert metrics["stable_prefix_chars"] == prompt.index("## Dynamic Review Payload")
    assert metrics["dynamic_payload_chars"] == len(prompt) - metrics["stable_prefix_chars"]
    assert metrics["stable_prefix_ratio"] > 0.75
    assert metrics["cache_health"] == "good"
    assert metrics["patch_body_chars"] == len(patch_body)
    assert metrics["diff_stat_chars"] == len(diff_stat)
    assert metrics["estimated_prompt_tokens"] == (len(prompt) + 3) // 4


def test_review_prompt_metrics_artifact_path_is_part_of_attempt_contract(tmp_path) -> None:
    paths = plan_artifact_paths(tmp_path)

    assert paths["review_prompt_metrics"] == tmp_path / "review.prompt.metrics.json"
