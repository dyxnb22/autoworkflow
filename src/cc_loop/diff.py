"""Bounded diff collection for reviewer context."""

from __future__ import annotations

from pathlib import Path

from cc_loop.git import _git_output

_GENERATED_SUFFIXES = {
    ".min.js",
    ".min.css",
    ".map",
    ".pyc",
    ".pyo",
    ".class",
    ".o",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".ico",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".jar",
    ".war",
    ".whl",
}

_LOCKFILE_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "go.sum",
    "Gemfile.lock",
    "composer.lock",
}

_SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".m",
    ".mm",
    ".scala",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".html",
    ".css",
    ".scss",
    ".md",
    ".rst",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".xml",
}


def _changed_files(worktree: Path, base_commit: str) -> list[str]:
    name_text = _git_output(worktree, "diff", "--name-only", f"{base_commit}...HEAD").strip()
    files = [line.strip() for line in name_text.splitlines() if line.strip()]
    if files:
        return files

    porcelain = _git_output(worktree, "status", "--porcelain").strip()
    unstaged: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        unstaged.append(path)
    return unstaged


def _is_generated(path: str) -> bool:
    lowered = path.lower()
    for suffix in _GENERATED_SUFFIXES:
        if lowered.endswith(suffix):
            return True
    parts = Path(path).parts
    if "node_modules" in parts or "__pycache__" in parts or ".egg-info" in parts:
        return True
    return False


def _is_test_file(path: str) -> bool:
    lowered = path.lower()
    name = Path(path).name.lower()
    return (
        "/test/" in lowered
        or lowered.startswith("test/")
        or lowered.startswith("tests/")
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("_test.go")
        or name.endswith(".test.js")
        or name.endswith(".test.ts")
        or name.endswith(".spec.js")
        or name.endswith(".spec.ts")
    )


def _is_source_file(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    return suffix in _SOURCE_EXTENSIONS


def _priority(path: str) -> tuple[int, str]:
    if _is_test_file(path):
        return (0, path)
    if _is_source_file(path):
        return (1, path)
    if Path(path).name in _LOCKFILE_NAMES:
        return (4, path)
    if _is_generated(path):
        return (5, path)
    return (3, path)


def _file_patch(worktree: Path, base_commit: str, file_path: str) -> str:
    committed = _git_output(
        worktree,
        "diff",
        f"{base_commit}...HEAD",
        "--",
        file_path,
    ).strip()
    if committed:
        return committed
    return _git_output(worktree, "diff", "HEAD", "--", file_path).strip()


def collect_bounded_review_patches(
    worktree: Path,
    base_commit: str,
    *,
    patches_dir: Path,
    max_bytes: int,
) -> tuple[list[Path], str, int]:
    """Collect per-file patches up to ``max_bytes`` total for reviewer context."""
    patches_dir.mkdir(parents=True, exist_ok=True)
    changed = sorted(set(_changed_files(worktree, base_commit)), key=_priority)

    selected_paths: list[Path] = []
    prompt_sections: list[str] = []
    used_bytes = 0
    omitted: list[str] = []

    for file_path in changed:
        if Path(file_path).name in _LOCKFILE_NAMES:
            omitted.append(f"{file_path} (lockfile omitted from patch body)")
            continue
        if _is_generated(file_path):
            omitted.append(f"{file_path} (generated file omitted)")
            continue

        patch_text = _file_patch(worktree, base_commit, file_path)
        if not patch_text:
            continue

        patch_bytes = len(patch_text.encode("utf-8"))
        if used_bytes + patch_bytes > max_bytes:
            omitted.append(f"{file_path} (exceeds max_review_patch_bytes budget)")
            continue

        safe_name = file_path.replace("/", "__")
        patch_path = patches_dir / f"{safe_name}.patch"
        patch_path.write_text(patch_text + "\n", encoding="utf-8")
        selected_paths.append(patch_path)
        prompt_sections.append(f"### {file_path}\n```diff\n{patch_text}\n```")
        used_bytes += patch_bytes

    if omitted:
        prompt_sections.append("### Omitted files\n" + "\n".join(f"- {item}" for item in omitted))

    combined = "\n\n".join(prompt_sections)
    return selected_paths, combined, used_bytes


def read_diff_stat_summary(worktree: Path, base_commit: str) -> str:
    """Return git diff --stat for base_commit...HEAD."""
    stat = _git_output(worktree, "diff", "--stat", f"{base_commit}...HEAD").strip()
    if stat:
        return stat
    return _git_output(worktree, "diff", "--stat", "HEAD").strip() or "(no diff)"
