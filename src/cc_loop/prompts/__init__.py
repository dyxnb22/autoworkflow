"""Load composable prompt fragments from package defaults or a project override dir."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cc_loop.config import LoopConfig

PACKAGE_PROMPTS_DIR = Path(__file__).resolve().parent

_fragment_cache: dict[tuple[str, str], str] = {}


def clear_prompt_fragment_cache() -> None:
    """Clear the in-memory fragment cache (for tests)."""
    _fragment_cache.clear()


def resolve_prompts_dir(config: LoopConfig | None = None) -> Path:
    """Return the effective prompts directory for fragment loading."""
    if config:
        override = str(config.get("prompts_dir", "") or "").strip()
        if override:
            return Path(override).expanduser().resolve()
    return PACKAGE_PROMPTS_DIR


def load_prompt_fragment(
    relative_path: str,
    *,
    config: LoopConfig | None = None,
    **format_vars: Any,
) -> str:
    """Load a prompt fragment, preferring ``prompts_dir`` overrides when configured."""
    prompts_dir = resolve_prompts_dir(config)
    cache_key = (str(prompts_dir), relative_path)
    if cache_key not in _fragment_cache:
        override = prompts_dir / relative_path
        default = PACKAGE_PROMPTS_DIR / relative_path
        path = override if override.is_file() else default
        if not path.is_file():
            raise FileNotFoundError(
                f"Prompt fragment not found: {relative_path} "
                f"(searched {override} and {default})"
            )
        _fragment_cache[cache_key] = path.read_text(encoding="utf-8")
    text = _fragment_cache[cache_key]
    if format_vars:
        return text.format(**format_vars)
    return text
