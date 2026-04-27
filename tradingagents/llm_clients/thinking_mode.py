"""DeepSeek Thinking Mode 兼容层（共享 helper）。

DeepSeek V4 系列（deepseek-v4-pro / deepseek-v4-flash 等）思考模式要求：
- 请求里加 extra_body={"thinking": {"type": "enabled"}}，可选 reasoning_effort
- 不接受 temperature / top_p / presence_penalty / frequency_penalty
- 多轮 + tool_call 时，必须把上一条 assistant 的 reasoning_content 回传
- 非 tool_call 场景，回传 reasoning_content 也会被服务端忽略（不报错）

这两个客户端都需要这套逻辑：
- tradingagents.llm_clients.openai_client.NormalizedChatOpenAI（OpenAI 兼容统一封装）
- tradingagents.llm_adapters.deepseek_adapter.ChatDeepSeek（DeepSeek 专用）

参考: https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
"""

from __future__ import annotations

from typing import Any, Optional

from langchain_core.messages import AIMessage, BaseMessage


THINKING_FORBIDDEN_PARAMS = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
)


def is_thinking_model(model: Optional[str]) -> bool:
    """识别需要走 thinking 模式的模型。"""
    name = (model or "").lower()
    return name.startswith("deepseek-v4")


def apply_thinking_init_kwargs(model: Optional[str], kwargs: dict) -> dict:
    """如为 thinking 模型，就地修改 kwargs 注入 thinking 配置并剔除非法采样参数。

    返回修改后的 kwargs（同对象，便于链式使用）。
    """
    if not is_thinking_model(model):
        return kwargs

    extra_body = dict(kwargs.pop("extra_body", None) or {})
    extra_body.setdefault("thinking", {"type": "enabled"})
    kwargs["extra_body"] = extra_body
    kwargs.setdefault("reasoning_effort", "high")
    # thinking 模式不支持以下采样参数；显式置 None / 移除
    kwargs["temperature"] = None
    for k in THINKING_FORBIDDEN_PARAMS:
        if k != "temperature":
            kwargs.pop(k, None)
    return kwargs


def inject_reasoning_content_into_payload(payload: dict, input_) -> dict:
    """把 AIMessage.additional_kwargs.reasoning_content 注回 payload['messages'] 中
    对应的 assistant 字典。

    - input_ 可以是 PromptValue（带 to_messages）、BaseMessage 列表、或字符串
    - 顺序对齐：第 N 个 assistant dict 对应第 N 个 AIMessage
    - 已有 reasoning_content 字段则不覆盖（尊重上游已注入）
    """
    # 同时剔除 thinking 模式不支持的采样参数（防止上层 with_config 传进来）
    for k in THINKING_FORBIDDEN_PARAMS:
        payload.pop(k, None)

    if hasattr(input_, "to_messages"):
        src_msgs = input_.to_messages()
    elif isinstance(input_, list):
        src_msgs = [m for m in input_ if isinstance(m, BaseMessage)]
    else:
        src_msgs = []

    api_msgs = payload.get("messages") or []
    src_iter = iter(src_msgs)
    for api_msg in api_msgs:
        if api_msg.get("role") != "assistant":
            continue
        src_msg: Optional[AIMessage] = None
        for cand in src_iter:
            if isinstance(cand, AIMessage):
                src_msg = cand
                break
        if src_msg is None:
            break
        rc = (src_msg.additional_kwargs or {}).get("reasoning_content")
        if rc and "reasoning_content" not in api_msg:
            api_msg["reasoning_content"] = rc

    return payload


def attach_reasoning_content_to_result(response: Any, result: Any) -> Any:
    """把响应里的 reasoning_content 收进 ChatResult 各 generation 的 AIMessage.additional_kwargs。"""
    try:
        choices = response.choices if hasattr(response, "choices") else (response or {}).get("choices", [])
    except Exception:
        return result

    for i, choice in enumerate(choices or []):
        if i >= len(result.generations):
            break
        try:
            msg = choice.message if hasattr(choice, "message") else choice.get("message", {})
            if isinstance(msg, dict):
                rc = msg.get("reasoning_content")
            else:
                rc = getattr(msg, "reasoning_content", None)
        except Exception:
            rc = None
        if not rc:
            continue
        gen_msg = result.generations[i].message
        if isinstance(gen_msg, AIMessage):
            gen_msg.additional_kwargs["reasoning_content"] = rc

    return result
