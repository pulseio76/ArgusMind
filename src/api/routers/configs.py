"""配置路由（含 LLM 配置修改）"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

from fastapi import APIRouter

from src.api.exceptions import NotFoundError
from src.api.security import CurrentUserDep
from src.llm.client import LLMClient
from src.schemas.common import OkResponse
from src.schemas.config import (
    CodeAgentConfigUpdate,
    ConfigRead,
    ConfigUpdate,
    LLMConfigUpdate,
)
from src.services import config_service
from src.tools.opencode import OpenCodeTool

router = APIRouter(dependencies=[CurrentUserDep])


def _mask_secret(value: str) -> str:
    if not value:
        return value
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _sanitize_llm_config(config: dict) -> dict:
    sanitized = dict(config)
    if isinstance(sanitized.get("LLM_key"), str):
        sanitized["LLM_key"] = _mask_secret(sanitized["LLM_key"])
    return sanitized


def _sanitize_code_agent_config(config: dict) -> dict:
    sanitized = dict(config)
    if isinstance(sanitized.get("code_agent_key"), str):
        sanitized["code_agent_key"] = _mask_secret(sanitized["code_agent_key"])
    return sanitized


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            return _to_jsonable(dumped)
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            dumped = value.dict()
            return _to_jsonable(dumped)
        except Exception:
            pass
    return str(value)


@router.put("/llm", response_model=OkResponse[dict])
def update_llm(body: LLMConfigUpdate) -> OkResponse[dict]:
    merged = config_service.patch_llm_config(body.model_dump(exclude_unset=True))
    return OkResponse[dict](data=_sanitize_llm_config(merged))


@router.get("/llm", response_model=OkResponse[dict])
def get_llm_config() -> OkResponse[dict]:
    config = config_service.get_value_json(config_service.CFG_LLM) or {}
    return OkResponse[dict](data=_sanitize_llm_config(config))


@router.get("/llm/test", response_model=OkResponse[str])
def test_llm_config() -> OkResponse[str]:
    runtime_cfg = config_service.get_llm_runtime_config()
    if runtime_cfg is None:
        raise NotFoundError("LLM 配置不完整，无法测试")

    try:
        client = LLMClient(runtime_cfg)
        # 暴露实际路由参数，便于排查「连不上 / 报错」时定位是 model 前缀还是 base_url 问题
        routed_kwargs = client._litellm_kwargs(temperature=0.7, max_tokens=None)
        diagnosis = {
            "route": {
                "model": routed_kwargs.get("model"),
                "custom_llm_provider": routed_kwargs.get("custom_llm_provider"),
                "api_base": routed_kwargs.get("api_base"),
                "is_custom": (runtime_cfg.type or "").strip().lower() == "custom",
                "llm_format": runtime_cfg.llm_format,
            },
        }
        resp = client.call([{"role": "user", "content": "hello"}])
        diagnosis["reply"] = resp.content
        diagnosis["usage"] = resp.usage
        diagnosis["ok"] = True
        return OkResponse[str](data=json.dumps(diagnosis, ensure_ascii=False))
    except Exception as ex:
        return OkResponse[str](
            success=False,
            data=json.dumps({"ok": False, "error": str(ex)}, ensure_ascii=False),
        )


@router.get("/llm/provider-list", response_model=OkResponse[Any])
def get_llm_provider_list_config() -> OkResponse[Any]:
    config = config_service.get_value_json(config_service.CFG_LLM_PROVIDER_LIST) or {}
    return OkResponse[Any](data=config)


@router.put("/code-agent", response_model=OkResponse[dict])
async def update_code_agent(body: CodeAgentConfigUpdate) -> OkResponse[dict]:
    merged = await config_service.patch_code_agent_config(body)
    return OkResponse[dict](data=merged)


@router.get("/code-agent", response_model=OkResponse[dict])
def get_code_agent_config() -> OkResponse[dict]:
    config = config_service.get_value_json(config_service.CFG_CODE_AGENT) or {}
    return OkResponse[dict](data=_sanitize_code_agent_config(config))


@router.get("/code-agent/test", response_model=OkResponse[str])
def test_code_agent_config() -> OkResponse[str]:
    runtime_cfg = config_service.get_opencode_runtime_config()
    if runtime_cfg is None:
        raise NotFoundError("code-agent 配置不完整，无法测试")

    project_root = str(Path(__file__).resolve().parents[2])
    tool = OpenCodeTool(
        project_path=project_root,
        model_id=runtime_cfg.model_id,
        provider_id=runtime_cfg.provider_id,
        read_timeout=120.0,
    )
    try:
        sid = tool.create_session()
        raw_resp = tool.client.session.chat(
            id=sid,
            model_id=tool.model_id,
            provider_id=tool.provider_id,
            parts=[{"type": "text", "text": "hello"}],
            extra_body={
                "model": {
                    "providerID": tool.provider_id,
                    "modelID": tool.model_id,
                }
            },
        )
        raw_json = json.dumps(_to_jsonable(raw_resp), ensure_ascii=False)
        return OkResponse[str](data=raw_json)
    except Exception as ex:
        return OkResponse[str](
            success=False,
            data=json.dumps({"ok": False, "error": str(ex)}, ensure_ascii=False),
        )
    finally:
        tool.close()


@router.get("/code-agent/provider-list", response_model=OkResponse[Any])
def get_code_agent_provider_list_config() -> OkResponse[Any]:
    config = config_service.get_value_json(config_service.CFG_CODE_AGENT_PROVIDER_LIST) or {}
    return OkResponse[Any](data=config)
