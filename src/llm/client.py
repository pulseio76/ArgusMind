"""LLM 客户端（基于 LiteLLM 统一接口）"""
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.config import LLMConfig

try:
    import litellm
    from litellm import completion as litellm_completion
except ImportError:
    litellm = None
    litellm_completion = None

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """LLM 调用失败（鉴权失败、额度不足、网络错误、服务异常等）。

    这是一类**致命错误**：调用方不应将其当作"空响应"吞掉后继续重试并标记完成，
    而应让其向上传播，由编排层把任务标记为 ``failed``。

    Attributes:
        original: 底层抛出的原始异常（litellm/openai 等）
        retryable: 是否属于可重试的瞬时错误（已在客户端内重试耗尽）
        status_code: HTTP 状态码（若可获取），便于排查（401 鉴权 / 402,429 额度等）
    """

    def __init__(
        self,
        message: str,
        *,
        original: Optional[BaseException] = None,
        retryable: bool = False,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.original = original
        self.retryable = retryable
        self.status_code = status_code


# 可重试的瞬时错误（网络/超时/服务暂时不可用/限流）。
# 限流可能是真正的额度耗尽，也可能是临时节流；统一先按瞬时重试，
# 重试耗尽后仍会抛出 LLMError，保证不会被当作空响应吞掉。
_RETRYABLE_EXC_NAMES = frozenset({
    "Timeout",
    "APITimeoutError",
    "APIConnectionError",
    "ServiceUnavailableError",
    "InternalServerError",
    "RateLimitError",
})

@dataclass
class LLMResponse:
    """单次调用的返回：回复内容 + 本次对话消耗的 token"""

    content: str
    """AI 回复文本"""

    prompt_tokens: int = 0
    """输入（提示）消耗的 token 数"""

    completion_tokens: int = 0
    """输出（补全）消耗的 token 数"""

    total_tokens: int = 0
    """总 token 数（prompt_tokens + completion_tokens）"""

    raw: object | None = None
    """底层 LLM 原始响应对象（用于调试/透传）"""

    @property
    def usage(self) -> Dict[str, int]:
        """以字典形式返回用量，便于日志或上报"""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class LLMClient:
    """
    LLM 客户端，基于 [LiteLLM](https://docs.litellm.ai/docs/) 统一调用多种模型。

    所有参数通过 config 传入，不读写环境变量。
    模型格式为：`provider/model`，例如：
    - openai/gpt-4o
    - anthropic/claude-3-sonnet-20240229
    - azure/your-deployment-name
    - ollama/llama2（本地）
    - 自建服务：config 中设置 base_url，model 用 openai/your-model 或 custom/your-model
    """

    def __init__(self, config: LLMConfig):
        """
        初始化 LLM 客户端。

        Args:
            config: LLM 配置（api_key、base_url、api_version 等均通过 config 传入）
        """
        self.MAX_RETRIES = 3
        if litellm_completion is None:
            raise ImportError("请安装 litellm: pip install litellm")
        self.config = config

    def _litellm_kwargs(self, temperature: float = 0.7, max_tokens: Optional[int] = None, **kwargs) -> Dict:
        """从 config 构建 litellm completion 参数，不依赖环境变量。

        路由策略（单点收口）：
        - 内置 provider（deepseek / anthropic / openai / azure ...）：使用 litellm 原生
          `provider/model` 形式，由 litellm 自行解析 base_url / 鉴权。
        - 自定义 provider：统一通过 ``custom_llm_provider`` 指定上游协议格式，
          传 **裸模型名**（不带 provider 前缀）+ ``api_base``，避免把任意 provider ID
          拼成 ``<custom-id>/<model>`` 这种 litellm 无法识别的前缀。
          协议格式由 ``llm_format`` 决定（openai / anthropic），缺省时按 base_url 启发式。
        """
        model = (self.config.model or "").strip()
        provider = (self.config.provider or "").strip()
        is_custom = (self.config.type or "").strip().lower() == "custom"

        kw: Dict = {
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.config.api_key:
            kw["api_key"] = self.config.api_key
        if getattr(self.config, "api_version", None):
            kw["api_version"] = self.config.api_version

        api_base = self.config.base_url or getattr(self.config, "azure_endpoint", None)

        if is_custom:
            # 自定义 Provider：不要拼 provider 前缀，用 custom_llm_provider 路由协议格式
            fmt = (self.config.llm_format or "").strip().lower()
            if not fmt:
                # 启发式：base_url 含 /anthropic 视作 Anthropic 格式端点，否则 OpenAI 兼容
                fmt = "anthropic" if "/anthropic" in (api_base or "").lower() else "openai"
            fmt = fmt if fmt in ("openai", "anthropic") else "openai"
            kw["model"] = model
            kw["custom_llm_provider"] = fmt
            if api_base:
                kw["api_base"] = api_base
        else:
            # 内置 provider：litellm 原生 provider/model 形式
            litellm_model = model
            if "/" not in litellm_model and provider:
                litellm_model = f"{provider}/{litellm_model}"
            kw["model"] = litellm_model
            # 自建/兼容端点：builtin 模式下仍允许覆盖 base_url（如自托管的 openai 网关）
            if api_base:
                kw["api_base"] = api_base

        kw.update(kwargs)
        return {k: v for k, v in kw.items() if v is not None}

    def _is_retryable_error(self, exc: BaseException) -> bool:
        """判断 LLM 调用异常是否为可重试的瞬时错误。

        鉴权失败 / 额度不足 / 请求非法 / 上下文超长等视为致命，立即失败，
        不做无意义的重试。
        """
        # 通过异常类型名匹配，避免不同 litellm 版本类路径差异导致 import 失败
        for klass in type(exc).__mro__:
            if klass.__name__ in _RETRYABLE_EXC_NAMES:
                return True
        return False

    def _parse_usage(self, response) -> tuple[int, int, int]:
        """从 LiteLLM 响应中解析 usage，返回 (prompt_tokens, completion_tokens, total_tokens)"""
        prompt_tokens = completion_tokens = total_tokens = 0
        if getattr(response, "usage", None):
            u = response.usage
            prompt_tokens = getattr(u, "prompt_tokens", 0) or 0
            completion_tokens = getattr(u, "completion_tokens", 0) or 0
            total_tokens = getattr(u, "total_tokens", 0) or (prompt_tokens + completion_tokens)
        return (prompt_tokens, completion_tokens, total_tokens)

    def call(
            self,
            messages: List[Dict[str, str]],
            temperature: float = 0.7,
            max_tokens: Optional[int] = None,
            **kwargs,
    ) -> LLMResponse:
        """
        调用 LLM 接口。

        Args:
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}]
            temperature: 温度参数
            max_tokens: 最大 token 数
            **kwargs: 其他 LiteLLM 支持的参数（如 stream=True）

        Returns:
            LLMResponse：包含 content（回复文本）与 prompt_tokens/completion_tokens/total_tokens

        Raises:
            LLMError: 调用失败（鉴权/额度/网络/服务异常等），属于致命错误，
                调用方不应吞掉，应让其向上传播以将任务标记为 failed。
        """
        litellm_kw = self._litellm_kwargs(
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        last_exc: Optional[BaseException] = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = litellm_completion(
                    messages=messages,
                    **litellm_kw,
                )
            except Exception as e:
                last_exc = e
                retryable = self._is_retryable_error(e)
                status_code = getattr(e, "status_code", None)
                if retryable and attempt < self.MAX_RETRIES:
                    sleep_s = min(2 ** (attempt - 1), 8)
                    logger.warning(
                        "LLM 调用瞬时错误，第 %d/%d 次尝试失败，%ss 后重试: %r",
                        attempt, self.MAX_RETRIES, sleep_s, e,
                    )
                    time.sleep(sleep_s)
                    continue
                raise LLMError(
                    f"LLM 调用失败: {e}",
                    original=e,
                    retryable=retryable,
                    status_code=status_code,
                ) from e

            content = response.choices[0].message.content or ""
            prompt_tokens, completion_tokens, total_tokens = self._parse_usage(response)
            return LLMResponse(
                content=content,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                raw=response,
            )

        # 理论上不可达：重试循环只会以 return 或 raise 结束
        raise LLMError(f"LLM 调用失败: {last_exc}", original=last_exc, retryable=True)



    def supports_streaming(self) -> bool:
        """LiteLLM 支持流式，传 stream=True 即可"""
        return True

    def supports_json_mode(self) -> bool:
        """部分模型支持 response_format json_object"""
        return True
