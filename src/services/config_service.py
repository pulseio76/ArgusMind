"""配置服务：通过 PostgreSQL `configs` 表管理 LLM / OpenCode 等运行期配置"""
from __future__ import annotations

import asyncio
import secrets
import string
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from sqlalchemy.orm import Session

from src.config import LLMConfig, OpenCodeConfig
from src.infrastructure.db import session_scope
from src.infrastructure.db.models import ConfigEntry
from src.schemas.config import CodeAgentConfigUpdate
from src.tmp_dir import tmp_base_glob
from src.tools.opencode import OpenCodeTool

CFG_LLM = "LLM_config"
CFG_CODE_AGENT = "code_agent_config"
CFG_LLM_PROVIDER_LIST = "LLM_provider_list"
CFG_CODE_AGENT_PROVIDER_LIST = "code_agent_provider_list"
_JWT_SECRET: Optional[str] = None
_JWT_SECRET_FILE = Path(__file__).resolve().parents[2] / ".jwt_secret"


def get_config(name: str) -> Optional[ConfigEntry]:
    with session_scope() as session:
        row = session.query(ConfigEntry).filter(ConfigEntry.name == name).one_or_none()
        if row is None:
            return None
        session.expunge(row)
        return row


def get_value_json(name: str) -> Optional[Dict[str, Any]]:
    row = get_config(name)
    return dict(row.value_json) if row and row.value_json else None


def get_value_str(name: str) -> Optional[str]:
    row = get_config(name)
    return row.value_str if row else None


def list_configs() -> List[ConfigEntry]:
    with session_scope() as session:
        rows = session.query(ConfigEntry).order_by(ConfigEntry.name).all()
        for r in rows:
            session.expunge(r)
        return rows


def upsert_config(
    name: str,
    *,
    value_json: Optional[Dict[str, Any]] = None,
    value_str: Optional[str] = None,
    description: Optional[str] = None,
) -> ConfigEntry:
    with session_scope() as session:
        row = session.query(ConfigEntry).filter(ConfigEntry.name == name).one_or_none()
        if row is None:
            row = ConfigEntry(name=name)
            session.add(row)
        if value_json is not None:
            row.value_json = value_json
        if value_str is not None:
            row.value_str = value_str
        if description is not None:
            row.description = description
        session.flush()
        session.expunge(row)
        return row


def patch_llm_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """对 LLM_config 的 JSONB 做部分字段更新，返回合并后的最新值"""
    with session_scope() as session:
        row = _get_or_create(session, CFG_LLM, default={})
        merged = dict(row.value_json or {})
        merged.update(updates or {})
        row.value_json = merged
        session.flush()
        return dict(merged)


async def patch_code_agent_config(payload: CodeAgentConfigUpdate) -> Dict[str, Any]:
    # 按覆盖更新语义处理：以当前请求体为准，不再与历史配置做 merge。
    merged: Dict[str, Any] = payload.model_dump()
    await _save_code_agent_config_to_opencode(merged)
    with session_scope() as session:
        row = _get_or_create(session, CFG_CODE_AGENT, default={})
        row.value_json = merged
        session.flush()
    return dict(merged)


def get_llm_runtime_config() -> Optional[LLMConfig]:
    """从 configs 表拼装出 LLMConfig"""
    cfg = get_value_json(CFG_LLM) or {}
    provider = (cfg.get("LLM_provider") or "").strip()
    model = (cfg.get("LLM_model") or "").strip()
    key = (cfg.get("LLM_key") or "").strip()
    tp = (cfg.get("type") or "").strip()
    if not (provider and model and key):
        return None
    return LLMConfig(
        provider=provider,
        api_key=key,
        model=model,
        type=tp,
        base_url=cfg.get("LLM_baseurl") or None,
        llm_format=(cfg.get("LLM_format") or "").strip().lower() or None,
    )


def get_opencode_runtime_config() -> Optional[OpenCodeConfig]:
    cfg = get_value_json(CFG_CODE_AGENT) or {}
    base_url = (cfg.get("code_agent_baseurl") or "").strip()
    # if not base_url:
    #     return None
    return OpenCodeConfig(
        base_url=base_url,
        model_id=cfg.get("code_agent_model") or None,
        provider_id=cfg.get("code_agent_provider") or None,
    )


def ensure_jwt_secret() -> str:
    """返回 JWT 密钥：优先读本地文件，不存在则生成并持久化。"""
    global _JWT_SECRET
    if _JWT_SECRET:
        return _JWT_SECRET

    if _JWT_SECRET_FILE.exists():
        try:
            secret_from_file = _JWT_SECRET_FILE.read_text(encoding="utf-8").strip()
            if secret_from_file:
                _JWT_SECRET = secret_from_file
                return _JWT_SECRET
        except Exception:
            # 文件不可读时回退到生成新密钥，避免影响服务启动。
            pass

    alphabet = string.ascii_letters + string.digits
    _JWT_SECRET = "".join(secrets.choice(alphabet) for _ in range(48))
    try:
        _JWT_SECRET_FILE.write_text(_JWT_SECRET, encoding="utf-8")
    except Exception:
        # 文件不可写时继续使用内存密钥（仅本进程有效）。
        pass
    return _JWT_SECRET


def _get_or_create(session: Session, name: str, *, default: Dict[str, Any]) -> ConfigEntry:
    row = session.query(ConfigEntry).filter(ConfigEntry.name == name).one_or_none()
    if row is None:
        row = ConfigEntry(name=name, value_json=default)
        session.add(row)
        session.flush()
    return row


def _project_root() -> str:
    return str(Path(__file__).resolve().parents[2])


def _build_opencode_config_payload(cfg: Dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    provider_id = (cfg.get("code_agent_provider") or "").strip()
    model_id = (cfg.get("code_agent_model") or "").strip()
    api_key = (cfg.get("code_agent_key") or "").strip()
    provider_base_url = (cfg.get("code_agent_baseurl") or "").strip()
    if not provider_id:
        raise ValueError("code_agent_provider 不能为空")
    if not model_id:
        raise ValueError("code_agent_model 不能为空")

    provider_cfg: dict[str, Any] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": provider_id,
        "models": {
            model_id: {
                "name": model_id,
            }
        },
    }
    if provider_base_url:
        provider_cfg["options"] = {"baseURL": provider_base_url}

    patch_body = {
        "provider": {
            provider_id: provider_cfg,
        },
        "disabled_providers": [],
        "model": f"{provider_id}/{model_id}",
    }
    auth_body = {
        "type": "api",
        "key": api_key,
    }
    return provider_id, patch_body, auth_body


def _is_custom_code_agent_model(cfg: Dict[str, Any]) -> bool:
    custom_type = str(cfg.get("type") or "").strip().lower()
    if custom_type == "custom":
        return True

    engine = str(cfg.get("code_agent_engine") or "").strip().lower()
    if engine in {"custom", "user_defined", "user-defined"}:
        return True

    provider_id = (cfg.get("code_agent_provider") or "").strip()
    model_id = (cfg.get("code_agent_model") or "").strip()
    provider_list = get_value_json(CFG_CODE_AGENT_PROVIDER_LIST) or {}
    providers = provider_list.get("providers")
    if not isinstance(providers, list) or not provider_id or not model_id:
        return True

    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if str(provider.get("id") or "").strip() != provider_id:
            continue
        models = provider.get("models")
        if not isinstance(models, list):
            return True
        for model in models:
            if isinstance(model, dict) and str(model.get("id") or "").strip() == model_id:
                return False
        return True
    return True


async def _save_code_agent_config_to_opencode(cfg: Dict[str, Any]) -> None:
    provider_id, patch_body, auth_body = _build_opencode_config_payload(cfg)
    mcp_patch_body = {
        "mcp": {
            "GitNexus": {
                "type": "local",
                "command": ["npx", "-y", "gitnexus", "mcp"],
                "enabled": True,
            }
        }
    }
    tmp_glob = tmp_base_glob()
    permission = {
        "permission": {
            "read": {"*": "allow"},
            "edit": {"*": "allow"},
            "external_directory": {tmp_glob: "allow"}
        }
    }
    tool = OpenCodeTool(project_path=_project_root())
    try:
        service_url = tool.get_url().rstrip("/")
        timeout = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            global_patch_body: Dict[str, Any] = {}
            if _is_custom_code_agent_model(cfg):
                global_patch_body.update(patch_body)
            global_patch_body.update(mcp_patch_body)
            global_patch_body.update(permission)
            patch_resp = await client.patch(f"{service_url}/global/config", json=global_patch_body)
            patch_resp.raise_for_status()
            auth_resp = await client.put(f"{service_url}/auth/{provider_id}", json=auth_body)
            auth_resp.raise_for_status()
    finally:
        await asyncio.to_thread(tool.close)
