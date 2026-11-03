"""项目应用服务"""
from __future__ import annotations

import ipaddress
import shutil
import stat
import socket
import subprocess
import uuid
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple, cast
from urllib.parse import urlparse

from src.api.exceptions import AppException
from src.tmp_dir import ensure_tmp_base
from src.infrastructure.db import session_scope
from src.infrastructure.db.models import Project
from src.repositories.project_repository import ProjectListRow, ProjectRepository
from src.schemas.common import IdNameItem
from src.schemas.project import (
    HealthStatus,
    ProjectCreate,
    ProjectListItem,
    ProjectSourceType,
    ProjectStats,
    ProjectUpdate,
)

VALID_HEALTH_STATUSES = frozenset({"normal", "risk", "pending_scan"})
VALID_SOURCE_TYPES = frozenset({"git", "upload", "path"})

# 常见无参考价值/可重建目录：依赖缓存、构建产物、编辑器缓存等
COMMON_NOISE_DIR_NAMES = {
    "node_modules",
    ".pnpm-store",
    ".npm",
    ".yarn",
    ".yarn-cache",
    ".cache",
    ".turbo",
    ".parcel-cache",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".idea",
    ".vscode",
    ".vs",
    ".svn",
    ".hg",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
}

COMMON_NOISE_FILE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".class",
    ".o",
    ".obj",
    ".so",
    ".dll",
    ".dylib",
}

COMMON_NOISE_FILE_NAMES = {
    ".DS_Store",
    "Thumbs.db",
}


def _resolve_project_base_dir() -> Path:
    base = ensure_tmp_base()
    project_root = base / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    return project_root


def _is_private_or_loopback_ip(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


def _validate_git_url(git_url: str) -> None:
    parsed = urlparse(git_url)
    if parsed.scheme not in {"http", "https"}:
        raise AppException("git 仓库地址仅允许 http/https")
    host = parsed.hostname
    if not host:
        raise AppException("git 仓库地址无效")
    if host.lower() in {"localhost"}:
        raise AppException("不允许访问 localhost")

    try:
        addrs = socket.getaddrinfo(host, None)
    except socket.gaierror as ex:
        raise AppException(f"无法解析 git 仓库域名: {host}") from ex

    for entry in addrs:
        ip_str = entry[4][0]
        if _is_private_or_loopback_ip(ip_str):
            raise AppException("git 仓库地址解析到了内网地址，已拒绝")


def _ensure_child_path(base_dir: Path, target: Path) -> Path:
    base_resolved = base_dir.resolve()
    target_resolved = target.resolve()
    try:
        target_resolved.relative_to(base_resolved)
    except ValueError as ex:
        raise AppException("检测到非法路径访问") from ex
    return target_resolved


def _clone_git_repo(git_url: str, git_branch: str, dest_dir: Path) -> None:
    _validate_git_url(git_url)
    cmd = [
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        git_branch,
        "--single-branch",
        git_url,
        str(dest_dir),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AppException(f"拉取 git 仓库失败: {proc.stderr.strip() or proc.stdout.strip()}")


def _extract_archive_flatten_root(archive_path: Path, dest_dir: Path) -> None:
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            names = [info.filename for info in zf.infolist() if info.filename and not info.filename.endswith("/")]
            for name in names:
                candidate = dest_dir / name
                _ensure_child_path(dest_dir, candidate)
            for info in zf.infolist():
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise AppException("压缩包中不允许符号链接")
            zf.extractall(dest_dir)
    except zipfile.BadZipFile as ex:
        raise AppException("仅支持 zip 压缩包") from ex

    entries = [p for p in dest_dir.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        root = entries[0]
        for child in list(root.iterdir()):
            shutil.move(str(child), str(dest_dir / child.name))
        root.rmdir()


def _validate_source_path(source_path: str) -> Path:
    src = Path(source_path).expanduser().resolve()
    if not src.exists() or not src.is_dir():
        raise AppException("source_path 不存在或不是目录")
    # 开发阶段先限制必须是绝对路径，避免相对路径逃逸。
    if not Path(source_path).is_absolute():
        raise AppException("source_path 必须为绝对路径")
    return src


def _assert_no_symlink_recursive(src: Path) -> None:
    for child in src.rglob("*"):
        if child.is_symlink():
            raise AppException("source_path 中不允许符号链接")


def cleanup_project_noise(project_path: str) -> None:
    """
    后台清理项目目录中的常见噪音目录/缓存文件。
    只在项目根目录内做递归清理，不抛出异常影响主流程。
    """
    root = Path(project_path).resolve()
    if not root.exists() or not root.is_dir():
        return

    # 先删目录（从深到浅），避免父目录先删后子目录访问异常
    dirs_to_delete = []
    for p in root.rglob("*"):
        if p.is_dir() and p.name in COMMON_NOISE_DIR_NAMES:
            dirs_to_delete.append(p)
    for d in sorted(dirs_to_delete, key=lambda x: len(x.parts), reverse=True):
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

    # 再删文件噪音
    for p in root.rglob("*"):
        try:
            if not p.is_file():
                continue
            if p.name in COMMON_NOISE_FILE_NAMES or p.suffix.lower() in COMMON_NOISE_FILE_SUFFIXES:
                p.unlink(missing_ok=True)
        except Exception:
            # 清理任务不应影响主请求
            continue


def create_project_from_source(
    *,
    name: str,
    source_type: str,
    git_url: Optional[str] = None,
    git_branch: Optional[str] = None,
    source_path: Optional[str] = None,
    upload_file_path: Optional[Path] = None,
) -> Project:
    source_type = source_type.strip().lower()
    project_uuid = str(uuid.uuid4())
    project_dir = _resolve_project_base_dir() / project_uuid
    project_dir.mkdir(parents=True, exist_ok=False)

    try:
        if source_type == "git":
            if not git_url or not git_branch:
                raise AppException("git 数据源必须提供仓库地址和分支")
            _clone_git_repo(git_url, git_branch, project_dir)
        elif source_type == "upload":
            if upload_file_path is None:
                raise AppException("upload 数据源必须上传压缩包")
            _extract_archive_flatten_root(upload_file_path, project_dir)
        elif source_type == "path":
            if not source_path:
                raise AppException("path 数据源必须提供 source_path")
            src = _validate_source_path(source_path)
            _assert_no_symlink_recursive(src)
            for child in src.iterdir():
                target = project_dir / child.name
                if child.is_dir():
                    shutil.copytree(child, target, dirs_exist_ok=False)
                else:
                    shutil.copy2(child, target)
        else:
            raise AppException("不支持的数据源类型")
    except Exception:
        shutil.rmtree(project_dir, ignore_errors=True)
        raise

    with session_scope() as session:
        repo = ProjectRepository(session)
        project = Project(
            name=name,
            path=str(project_dir),
            project_uuid=project_uuid,
            source_type=source_type,
            source_git_url=git_url if source_type == "git" else None,
            source_git_branch=git_branch if source_type == "git" else None,
            source_path=source_path if source_type == "path" else None,
            storage_path=str(project_dir),
            session_id="",
            description="",
            description_compact="",
        )
        repo.add(project)
        session.expunge(project)
        return project


def create_project(data: ProjectCreate) -> Project:
    with session_scope() as session:
        repo = ProjectRepository(session)
        project = Project(
            name=data.name,
            path=data.path,
            project_uuid=data.project_uuid,
            source_type=data.source_type,
            source_git_url=data.source_git_url,
            source_git_branch=data.source_git_branch,
            source_path=data.source_path,
            storage_path=data.storage_path,
            session_id="",
            description=data.description or "",
            description_compact=data.description_compact or "",
        )
        repo.add(project)
        session.expunge(project)
        return project


def get_project(project_id: str) -> Optional[Project]:
    with session_scope() as session:
        repo = ProjectRepository(session)
        project = repo.get(project_id)
        if project:
            session.expunge(project)
        return project


def _normalize_source_type(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    value = raw.strip().lower()
    if value == "archive":
        return "upload"
    return value


def _validate_list_filters(*, source_type: Optional[str], health_status: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    normalized_source = _normalize_source_type(source_type)
    if source_type and normalized_source not in VALID_SOURCE_TYPES:
        raise AppException("source_type 无效，允许值：git、upload、path")
    if health_status and health_status not in VALID_HEALTH_STATUSES:
        raise AppException("health_status 无效，允许值：normal、risk、pending_scan")
    return normalized_source, health_status


def _repo_path(project: Project) -> Optional[str]:
    source_type = _normalize_source_type(project.source_type)
    if source_type == "git":
        return project.source_git_url
    if source_type == "path":
        return project.source_path
    return None


def _to_project_list_item(row: ProjectListRow) -> ProjectListItem:
    project = row.project
    source_type = _normalize_source_type(project.source_type)
    normalized_source = source_type if source_type in VALID_SOURCE_TYPES else None
    lang = project.language_stats
    return ProjectListItem(
        id=project.id,
        name=project.name,
        path=project.path,
        repo_path=_repo_path(project),
        branch=project.source_git_branch,
        source_type=cast(Optional[ProjectSourceType], normalized_source),
        health_status=cast(HealthStatus, row.health_status),
        language=lang if isinstance(lang, dict) and lang else None,
        vulnerability_count=row.vulnerability_count,
        high_risk_count=row.high_risk_count,
        file_count=project.file_count or 0,
        line_count=project.line_count or 0,
        last_scanned_at=row.last_scanned_at,
    )


def list_project_id_names() -> List[IdNameItem]:
    with session_scope() as session:
        rows = ProjectRepository(session).list_id_names()
        return [IdNameItem(id=row.id, name=row.name) for row in rows]


def list_projects(
    *,
    keyword: Optional[str] = None,
    source_type: Optional[str] = None,
    health_status: Optional[str] = None,
    current: int = 1,
    page_size: int = 20,
) -> Tuple[List[ProjectListItem], int]:
    normalized_source, normalized_health = _validate_list_filters(
        source_type=source_type, health_status=health_status
    )
    with session_scope() as session:
        repo = ProjectRepository(session)
        rows, total = repo.list(
            keyword=keyword,
            source_type=normalized_source,
            health_status=normalized_health,
            current=current,
            page_size=page_size,
        )
        return [_to_project_list_item(row) for row in rows], total


def project_stats(
    *,
    keyword: Optional[str] = None,
    source_type: Optional[str] = None,
) -> ProjectStats:
    normalized_source, _ = _validate_list_filters(source_type=source_type, health_status=None)
    with session_scope() as session:
        repo = ProjectRepository(session)
        row = repo.stats(keyword=keyword, source_type=normalized_source)
        return ProjectStats(
            total=row.total,
            normal=row.normal,
            risk=row.risk,
            pending_scan=row.pending_scan,
        )


def update_project(project_id: str, data: ProjectUpdate) -> Optional[Project]:
    with session_scope() as session:
        repo = ProjectRepository(session)
        project = repo.get(project_id)
        if project is None:
            return None
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(project, field, value)
        repo.update(project)
        session.expunge(project)
        return project


def delete_project(project_id: str) -> bool:
    with session_scope() as session:
        repo = ProjectRepository(session)
        project = repo.get(project_id)
        if project is None:
            return False
        project_path_raw = (project.storage_path or project.path or "").strip()
        if project_path_raw:
            base_dir = _resolve_project_base_dir()
            project_path = _ensure_child_path(base_dir, Path(project_path_raw).expanduser().resolve())
            if project_path.exists():
                shutil.rmtree(project_path, ignore_errors=False)
        repo.delete(project)
        return True
