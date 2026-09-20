"""Resolve uploaded files without escaping their owning project."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


class ProjectFileResolutionError(ValueError):
    """A requested project file is missing, ambiguous, or outside its scope."""


_MANAGED_DIRECTORY_KINDS = (
    "documents",
    "plans",
    "dialux-results",
    "design-runs",
)


def _normalized_suffixes(suffixes: Iterable[str]) -> frozenset[str]:
    return frozenset(
        value.casefold() if value.startswith(".") else f".{value.casefold()}"
        for value in suffixes
    )


def _inside(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def _belongs_to_project(root: Path, target: Path, project_id: str) -> bool:
    """Reject explicit paths into another project in a shared legacy root."""

    relative = target.relative_to(root)
    owned_directories = {
        f"{project_id}.{kind}".casefold() for kind in _MANAGED_DIRECTORY_KINDS
    }
    managed_suffixes = tuple(f".{kind}" for kind in _MANAGED_DIRECTORY_KINDS)
    for part in relative.parts[:-1]:
        folded = part.casefold()
        if folded.endswith(managed_suffixes) and folded not in owned_directories:
            return False
    return True


def resolve_project_file(
    root: Path,
    project_id: str,
    source: str,
    *,
    suffixes: Iterable[str],
) -> Path:
    """Resolve a project-relative path or an uploaded file's returned basename.

    Upload endpoints intentionally store assets in project-owned subdirectories,
    while the chat context exposes only the resulting filename. A basename is
    therefore searched inside this project's managed directories after the
    ordinary project-relative lookup fails.
    """

    project_root = Path(root).resolve()
    value = Path(source)
    target = value.resolve() if value.is_absolute() else (project_root / value).resolve()
    allowed_suffixes = _normalized_suffixes(suffixes)

    if not _inside(project_root, target) or not _belongs_to_project(
        project_root, target, project_id
    ):
        raise ProjectFileResolutionError("输入文件必须位于当前项目目录内")

    if target.is_file():
        if target.suffix.casefold() not in allowed_suffixes:
            expected = "、".join(sorted(allowed_suffixes))
            raise ProjectFileResolutionError(
                f"项目文件格式不受支持：{target.suffix or '无扩展名'}；需要 {expected}"
            )
        return target

    matches: list[Path] = []
    if not value.is_absolute() and value.parent == Path("."):
        requested_name = value.name.casefold()
        for kind in _MANAGED_DIRECTORY_KINDS:
            directory = project_root / f"{project_id}.{kind}"
            if not directory.is_dir():
                continue
            for candidate in directory.rglob("*"):
                if (
                    candidate.is_file()
                    and candidate.name.casefold() == requested_name
                    and candidate.suffix.casefold() in allowed_suffixes
                ):
                    resolved = candidate.resolve()
                    if _inside(directory.resolve(), resolved):
                        matches.append(resolved)

    unique_matches = list(dict.fromkeys(matches))
    if len(unique_matches) == 1:
        return unique_matches[0]
    if len(unique_matches) > 1:
        locations = "、".join(
            str(path.relative_to(project_root).as_posix()) for path in unique_matches
        )
        raise ProjectFileResolutionError(f"项目中存在多个同名文件，请指定相对路径：{locations}")

    expected = "、".join(sorted(allowed_suffixes))
    raise ProjectFileResolutionError(f"项目中不存在可用的 {expected} 文件：{source}")


__all__ = ["ProjectFileResolutionError", "resolve_project_file"]
