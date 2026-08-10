"""配置 schema"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict


class ConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    value_str: Optional[str] = None
    value_json: Optional[Dict[str, Any]] = None
    description: str = ""
    updated_at: datetime


class ConfigUpdate(BaseModel):
    value_str: Optional[str] = None
    value_json: Optional[Dict[str, Any]] = None
    description: Optional[str] = None


class LLMConfigUpdate(BaseModel):
    LLM_provider: Optional[str] = None
    LLM_key: Optional[str] = None
    LLM_model: Optional[str] = None
    LLM_baseurl: Optional[str] = None
    type: Optional[str] = None
    # 自定义 Provider 的 API 协议格式：openai | anthropic
    LLM_format: Optional[str] = None


class CodeAgentConfigUpdate(BaseModel):
    code_agent_provider: Optional[str] = None
    code_agent_key: Optional[str] = None
    code_agent_model: Optional[str] = None
    code_agent_baseurl: Optional[str] = None
    code_agent_engine: Optional[str] = None
    type: Optional[str] = None
