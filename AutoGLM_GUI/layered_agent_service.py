"""Layered agent execution service for task-backed orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agents import Agent, Runner, SQLiteSession, function_tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from AutoGLM_GUI.config import ModelConfig
from AutoGLM_GUI.config_manager import config_manager
from AutoGLM_GUI.logger import logger
from AutoGLM_GUI.model.error_details import (
    serialize_model_error_async,
    trace_error_attrs,
)
from AutoGLM_GUI.trace import TraceSpan, summarize_text, trace_span

if TYPE_CHECKING:
    from agents.result import RunResultStreaming


PLANNER_INSTRUCTIONS = """## 核心目标
你是一个负责操控手机的高级智能中枢。你的任务是将用户的意图转化为**视觉模型（Vision Model）**可以执行的原子操作。

## ⚠️ 极其重要的限制：视觉模型的能力边界 (Must Read)
你的下级（Vision Model）是一个**纯粹的执行者和观察者**。
1. **无"记忆/笔记"功能**：它没有 `Note` 功能，无法为你保存数据。
2. **无"系统级"权限**：它不能复制源代码，不能直接提取文本，不能读取剪贴板。
3. **唯一的输出**：它只能通过**对话**告诉你它看到了什么，或者去**点击/滑动**屏幕。

## 交互策略 (Interaction Strategy)

### 1. 如果你需要"操作手机" (To Act)
下达明确的 UI 动作指令。
- ✅ "点击'设置'图标。"
- ✅ "向下滑动屏幕。"
- ✅ "打开微信。"

### 2. 如果你需要"获取信息" (To Read/Extract)
你必须通过**提问**的方式，让视觉模型在对话中把信息"念"给你听。
- ❌ **错误**: "把验证码保存下来。" (它做不到)
- ❌ **错误**: "使用 Note 功能记录价格。" (它没有这个功能)
- ✅ **正确**: 调用 `chat` 询问："请看屏幕，告诉我现在的订单总金额是多少？"
  - *结果*: 视觉模型会回复 "25.5元"。你需要自己处理这个文本信息。

### 3. 如果用户要求"复制/粘贴"
必须通过模拟手指操作来实现，不能直接操作剪贴板。
- ✅ **正确**: "长按这段文字，等待弹出菜单，然后点击'复制'按钮。"

## 任务拆解原则 (Decomposition Rules)

1. **原子化**: 每次只给一个动作。
2. **可视化**: 指令必须基于屏幕上**看得见**的元素。不要说"点击确认"，如果屏幕上显示的按钮叫"OK"，请说"点击'OK'按钮"。
3. **Fail Fast**: 如果视觉模型回复 `ELEMENT_NOT_FOUND`，不要死循环。询问它："那现在屏幕上有什么？"或者尝试滑动寻找。

## 核心工作流 (The Loop)
1. **Observe (看)**: 调用 `chat` 询问当前状态。
   - "现在屏幕上显示什么？" / "刚才的点击生效了吗？"
2. **Think (想)**:
   - 用户的目标是什么？
   - 我需要让视觉模型**做什么动作**，还是**回答什么问题**？
3. **Act (做)**:
   - **Case A (动作)**: 发送指令 `点击[坐标]...`
   - **Case B (询问)**: 发送问题 `请读取...`

## 工具集 (Tools)
1. `list_devices()`
2. `chat(device_id, message)`:
   - 发送操作指令（如"点击红色按钮"）。
   - 发送查询问题（如"那个验证码是多少？"）。
"""


class TracedSQLiteSession(SQLiteSession):
    """SQLiteSession wrapper that exposes planner memory operations as spans."""

    async def get_items(self, limit: int | None = None) -> list[Any]:
        with trace_span(
            "memory.read",
            attrs={
                "memory_type": "layered_session",
                "session_id": self.session_id,
                "limit": limit,
            },
        ) as span:
            items = await super().get_items(limit)
            span.set_attribute("item_count", len(items))
            return items

    async def add_items(self, items: list[Any]) -> None:
        with trace_span(
            "memory.write",
            attrs={
                "memory_type": "layered_session",
                "session_id": self.session_id,
                "item_count": len(items),
            },
        ):
            await super().add_items(items)

    async def pop_item(self) -> Any | None:
        with trace_span(
            "memory.delete",
            attrs={
                "memory_type": "layered_session",
                "session_id": self.session_id,
                "operation": "pop_item",
            },
        ) as span:
            item = await super().pop_item()
            span.set_attribute("deleted", item is not None)
            return item

    async def clear_session(self) -> None:
        with trace_span(
            "memory.clear",
            attrs={
                "memory_type": "layered_session",
                "session_id": self.session_id,
            },
        ):
            await super().clear_session()


_sessions: dict[str, TracedSQLiteSession] = {}
_active_runs: dict[str, "RunResultStreaming"] = {}
_active_runs_lock = threading.Lock()

_client: AsyncOpenAI | None = None
_agent: Agent[Any] | None = None
_cached_config_hash: str | None = None

# 单个 chat 工具返回文本进入 planner 上下文的最大字符数。
# 超长的子任务执行摘要会随轮次累积, 逐步撑大发给决策模型的上下文,
# 最终触发模型服务端内存守护 (process memory limit exceeded)。
_MAX_TOOL_RESULT_CHARS = 4000


def _truncate_tool_result(text: str, limit: int = _MAX_TOOL_RESULT_CHARS) -> str:
    """截断进入规划模型上下文的工具结果文本, 避免上下文无限膨胀。"""
    if len(text) <= limit:
        return text
    head = text[: limit - 200]
    tail = text[-200:]
    omitted = len(text) - limit
    return f"{head}\n...(已省略 {omitted} 字符)...\n{tail}"


def _summarize_execution_history(context: list[dict[str, Any]]) -> list[dict[str, str]]:
    """将 Agent 的原始上下文提炼为精简的执行动作历史。

    过滤掉冗长的 system prompt 和截图 base64 数据，只保留每一步视觉模型
    实际执行的动作（从 assistant 的 <answer> 中提取），便于上层规划模型
    快速理解失败原因，而不是被原始 dump 淹没。
    """
    steps: list[dict[str, str]] = []
    step_index = 0
    for message in context:
        if message.get("role") != "assistant":
            continue

        raw = message.get("content", "")
        if isinstance(raw, list):
            raw = " ".join(
                part.get("text", "")
                for part in raw
                if isinstance(part, dict) and part.get("type") == "text"
            )
        text = str(raw).strip()

        step_index += 1
        think_match = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
        answer_match = re.search(r"<answer>(.*?)</answer>", text, re.DOTALL)
        think = (think_match.group(1).strip() if think_match else "").strip()
        action = (answer_match.group(1).strip() if answer_match else text).strip()

        steps.append(
            {
                "step": str(step_index),
                "action": action or "(未解析到有效动作)",
                **({"think": think} if think else {}),
            }
        )
    return steps


def _detect_repeated_actions(steps: list[dict[str, str]]) -> str | None:
    """检测执行历史中是否存在连续重复动作（陷入死循环的典型特征）。"""
    actions = [s.get("action", "") for s in steps if s.get("action")]
    if len(actions) < 3:
        return None
    last = actions[-1]
    repeat = 1
    for act in reversed(actions[:-1]):
        if act == last:
            repeat += 1
        else:
            break
    if repeat >= 3:
        return f"视觉模型连续 {repeat} 次执行了相同动作「{last}」，可能已陷入死循环或滑动/点击未生效。"
    return None


# planner 会话历史保留的最大条目数 (滑动窗口)。
# SQLiteSession 会把每一轮的工具调用/工具输出/助手消息全量累积并整体发给
# 决策模型, 长任务下上下文会无限膨胀, 触发模型服务端内存守护
# (process memory limit exceeded)。修剪历史仅保留最近若干条, 可有效控制上下文体积。
_MAX_SESSION_ITEMS = 60


async def _trim_session_history(
    session: SQLiteSession, max_items: int = _MAX_SESSION_ITEMS
) -> None:
    """对 planner 会话历史做滑动窗口修剪, 仅保留最近 ``max_items`` 条。

    通过整体读取历史、清空后回写最近条目的方式实现; 避免历史随任务轮次
    线性增长而撑爆决策模型上下文。修剪失败不应阻断主流程。
    """
    try:
        items = await session.get_items()
    except Exception as exc:  # pragma: no cover - 修剪失败不应阻断主流程
        logger.warning(f"[LayeredAgent] Failed to read session for trim: {exc}")
        return

    if len(items) <= max_items:
        return

    keep = items[-max_items:]
    try:
        await session.clear_session()
        await session.add_items(keep)
        logger.info(
            f"[LayeredAgent] Trimmed session {session.session_id}: "
            f"{len(items)} -> {len(keep)} items"
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(f"[LayeredAgent] Failed to trim session history: {exc}")


def _get_or_create_session(session_id: str) -> SQLiteSession:
    if session_id not in _sessions:
        with trace_span(
            "memory.session.create",
            attrs={
                "memory_type": "layered_session",
                "session_id": session_id,
            },
        ):
            _sessions[session_id] = TracedSQLiteSession(session_id)
            logger.info(f"[LayeredAgent] Created new session: {session_id}")
    return _sessions[session_id]


def reset_session(session_id: str) -> bool:
    if session_id in _sessions:
        with trace_span(
            "memory.session.drop",
            attrs={
                "memory_type": "layered_session",
                "session_id": session_id,
            },
        ):
            del _sessions[session_id]
            logger.info(f"[LayeredAgent] Cleared session: {session_id}")
        return True
    return False


def get_planner_model() -> str:
    config_manager.load_file_config()
    effective_config = config_manager.get_effective_config()
    model_name = effective_config.decision_model_name

    if not model_name:
        raise ValueError(
            "决策模型未配置。使用分层代理模式需要配置决策模型。\n"
            "请在全局配置中设置决策模型的 Base URL、模型名称和 API Key。"
        )

    logger.info(f"[LayeredAgent] Using decision model: {model_name}")
    return model_name


def _setup_openai_client() -> AsyncOpenAI:
    config_manager.load_file_config()
    effective_config = config_manager.get_effective_config()

    decision_base_url = effective_config.decision_base_url
    decision_api_key = effective_config.decision_api_key

    if not decision_base_url:
        raise ValueError(
            "决策模型 Base URL 未配置。使用分层代理模式需要配置决策模型。\n"
            "请在全局配置中设置决策模型的 Base URL、模型名称和 API Key。"
        )

    planner_model = get_planner_model()

    logger.info("[LayeredAgent] Decision model config:")
    logger.info(f"  - Base URL: {decision_base_url}")
    logger.info(f"  - Model: {planner_model}")
    logger.info(f"  - API Key: {'***' if decision_api_key else 'None'}")

    return AsyncOpenAI(
        base_url=decision_base_url,
        api_key=decision_api_key or "EMPTY",
    )


def _planner_model_config() -> ModelConfig:
    config_manager.load_file_config()
    effective_config = config_manager.get_effective_config()
    return ModelConfig(
        base_url=effective_config.decision_base_url or "",
        api_key=effective_config.decision_api_key or "EMPTY",
        model_name=effective_config.decision_model_name or "",
    )


@function_tool
async def list_devices() -> str:
    from AutoGLM_GUI.api.devices import _build_device_response_with_agent
    from AutoGLM_GUI.device_manager import DeviceManager
    from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager

    with trace_span("layered.tool.list_devices"):
        logger.info("[LayeredAgent] list_devices tool called")

        device_manager = DeviceManager.get_instance()
        agent_manager = PhoneAgentManager.get_instance()

        if not device_manager.is_polling_active():
            logger.warning("Polling not started, performing sync refresh")
            device_manager.force_refresh()

        managed_devices = device_manager.get_devices()
        devices_with_agents = [
            _build_device_response_with_agent(d, agent_manager) for d in managed_devices
        ]
        devices_dict = [device.model_dump() for device in devices_with_agents]
        return json.dumps(devices_dict, ensure_ascii=False, indent=2)


@function_tool
async def chat(device_id: str, message: str) -> str:
    from AutoGLM_GUI.exceptions import DeviceBusyError
    from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager
    from AutoGLM_GUI.prompts import MCP_SYSTEM_PROMPT_ZH

    mcp_max_steps = 5

    with trace_span(
        "layered.tool.chat",
        attrs={
            "device_id": device_id,
            "task_preview": summarize_text(message) or "",
            "task_length": len(message),
        },
    ) as tool_span:
        logger.info(
            f"[LayeredAgent] chat tool called: device_id={device_id}, message={message}"
        )

        manager = PhoneAgentManager.get_instance()
        acquired = False

        try:
            with trace_span(
                "layered.tool.chat.acquire_device",
                attrs={"device_id": device_id},
            ):
                acquired = await manager.acquire_device_async(
                    device_id,
                    auto_initialize=True,
                    context="layered",
                )

            with trace_span(
                "layered.tool.chat.get_agent",
                attrs={"device_id": device_id},
            ):
                agent = await manager.get_agent_with_context_async(
                    device_id,
                    context="layered",
                    agent_type=None,
                )

            original_max_steps = agent.agent_config.max_steps
            original_system_prompt = agent.agent_config.system_prompt

            agent.agent_config.max_steps = mcp_max_steps
            agent.agent_config.system_prompt = MCP_SYSTEM_PROMPT_ZH

            try:
                with trace_span(
                    "layered.tool.chat.reset_agent",
                    attrs={"device_id": device_id},
                ):
                    agent.reset()

                with trace_span(
                    "layered.tool.chat.run_agent",
                    attrs={
                        "device_id": device_id,
                        "agent_type": agent.__class__.__name__,
                        "is_async_agent": True,
                    },
                ):
                    result = await agent.run(message)
                steps = agent.step_count

                if steps >= mcp_max_steps and result == "Max steps reached":
                    tool_span.set_attributes(
                        {
                            "success": False,
                            "steps": mcp_max_steps,
                            "error_kind": "max_steps",
                        }
                    )
                    history = _summarize_execution_history(agent.context)
                    history_lines = "\n".join(
                        f"  第{s['step']}步: {s['action']}" for s in history
                    ) or "  (无有效动作记录)"

                    diagnosis = _detect_repeated_actions(history)
                    hints = [
                        "将任务拆分为更小的原子子任务后重试",
                        "确认目标页面/元素确实存在，必要时补充更明确的操作指引",
                    ]
                    if diagnosis:
                        hints.insert(
                            0,
                            "避免让视觉模型重复同一动作，改为提供不同的定位方式或改用其他交互步骤",
                        )
                    suggestion = "\n".join(f"  -{h}" for h in hints)

                    result_lines = [
                        f"⚠️ 已达到最大步数限制（{mcp_max_steps} 步），子任务未完成。",
                    ]
                    if diagnosis:
                        result_lines.append(f"\n诊断: {diagnosis}")
                    result_lines.append(f"\n视觉模型执行动作:\n{history_lines}")
                    result_lines.append(f"\n建议:\n{suggestion}")

                    # 注意: 不要把完整 history 数组回传给规划模型。
                    # 该工具返回体会被 SQLiteSession 全量累积进 planner 上下文,
                    # 逐轮膨胀会触发模型服务端内存守护 (process memory limit exceeded)。
                    # history 明细仅用于 trace/诊断, 精简后的可读摘要已包含在 result 中。
                    return json.dumps(
                        {
                            "result": _truncate_tool_result("\n".join(result_lines)),
                            "steps": mcp_max_steps,
                            "success": False,
                            "reason": "max_steps",
                        },
                        ensure_ascii=False,
                    )

                tool_span.set_attributes({"success": True, "steps": steps})
                return json.dumps(
                    {
                        "result": _truncate_tool_result(str(result)),
                        "steps": steps,
                        "success": True,
                    },
                    ensure_ascii=False,
                )
            finally:
                agent.agent_config.max_steps = original_max_steps
                agent.agent_config.system_prompt = original_system_prompt
        except DeviceBusyError:
            tool_span.set_attributes({"success": False, "error_kind": "busy"})
            return json.dumps(
                {
                    "result": f"设备 {device_id} 正忙，请稍后再试。",
                    "steps": 0,
                    "success": False,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            tool_span.set_attributes({"success": False, "error_kind": "unexpected"})
            logger.error(f"[LayeredAgent] chat tool error: {exc}")
            return json.dumps(
                {
                    "result": str(exc),
                    "steps": 0,
                    "success": False,
                },
                ensure_ascii=False,
            )
        finally:
            if acquired:
                try:
                    with trace_span(
                        "layered.tool.chat.release_device",
                        attrs={"device_id": device_id},
                    ):
                        await manager.release_device_async(device_id, context="layered")
                except BaseException as exc:  # pragma: no cover - safety net
                    logger.error(
                        f"Failed to release device lock for {device_id}: {exc}"
                    )


def _create_planner_agent(client: AsyncOpenAI) -> Agent[Any]:
    planner_model = get_planner_model()
    model = OpenAIChatCompletionsModel(
        model=planner_model,
        openai_client=client,
    )

    return Agent(
        name="Planner",
        instructions=PLANNER_INSTRUCTIONS,
        model=model,
        tools=[list_devices, chat],
    )


def _compute_config_hash() -> str:
    import hashlib

    config = config_manager.get_effective_config()
    config_str = config.model_dump_json()
    return hashlib.md5(config_str.encode()).hexdigest()


def _ensure_agent() -> Agent[Any]:
    global _client, _agent, _cached_config_hash

    current_hash = _compute_config_hash()

    if _agent is None or _cached_config_hash != current_hash:
        if _agent is not None and _cached_config_hash != current_hash:
            logger.info(
                f"[LayeredAgent] Config changed (hash: {_cached_config_hash} -> {current_hash}), reloading agent..."
            )

        _client = _setup_openai_client()
        _agent = _create_planner_agent(_client)
        _cached_config_hash = current_hash
        logger.info(
            f"[LayeredAgent] Agent initialized/reloaded with config hash: {current_hash}"
        )

    return _agent


@dataclass
class LayeredTaskRun:
    task_id: str
    session_id: str
    result: "RunResultStreaming"
    device_id: str | None = None
    final_output: str = ""
    success: bool = False
    cancelled: bool = False
    finished: bool = False
    _current_tool_call: dict[str, Any] | None = field(default=None, init=False)
    _cancel_event: asyncio.Event | None = field(default=None, init=False)

    async def cancel(self) -> None:
        self.cancelled = True
        if self._cancel_event is not None:
            self._cancel_event.set()
        if hasattr(self.result, "cancel"):
            self.result.cancel(mode="immediate")
        if self.device_id:
            try:
                from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager

                # 尝试中止可能正在运行的下层 Phone Agent
                await PhoneAgentManager.get_instance().abort_streaming_chat_async(
                    self.device_id
                )
            except Exception as e:
                logger.warning(
                    f"[LayeredAgent] Failed to abort phone agent for device {self.device_id}: {e}"
                )

    async def stream_events(self) -> AsyncIterator[dict[str, Any]]:
        from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent

        self._cancel_event = asyncio.Event()
        if self.cancelled:
            self._cancel_event.set()

        model_span: TraceSpan | None = None
        raw_event_count = 0

        def close_model_span() -> None:
            nonlocal model_span, raw_event_count
            if model_span is not None:
                model_span.__exit__(None, None, None)
                model_span = None
                raw_event_count = 0

        async def get_next(aiter: Any) -> Any:
            try:
                return await aiter.__anext__()
            except StopAsyncIteration:
                return None

        next_task: asyncio.Task[Any] | None = None
        cancel_task: asyncio.Task[bool] | None = None
        iterator: Any | None = None

        try:
            with trace_span(
                "layered.planner.stream",
                attrs={
                    "task_id": self.task_id,
                    "session_id": self.session_id,
                },
            ) as stream_span:
                iterator = self.result.stream_events().__aiter__()
                while True:
                    if self.cancelled:
                        break

                    next_task = asyncio.create_task(get_next(iterator))
                    cancel_task = asyncio.create_task(self._cancel_event.wait())

                    assert next_task is not None
                    assert cancel_task is not None
                    done, pending = await asyncio.wait(
                        [next_task, cancel_task], return_when=asyncio.FIRST_COMPLETED
                    )

                    if cancel_task in done:
                        next_task.cancel()
                        break

                    cancel_task.cancel()
                    event = next_task.result()
                    if event is None:
                        break

                    if isinstance(event, RawResponsesStreamEvent):
                        if model_span is None:
                            model_span = trace_span(
                                "model.call",
                                attrs={
                                    "model_role": "layered_planner",
                                    "task_id": self.task_id,
                                    "session_id": self.session_id,
                                },
                            )
                            model_span.__enter__()
                        raw_event_count += 1
                        raw_data = getattr(event, "data", None)
                        raw_event_type = getattr(raw_data, "type", None)
                        model_span.set_attributes(
                            {
                                "raw_event_count": raw_event_count,
                                "last_raw_event_type": raw_event_type
                                or type(raw_data).__name__,
                            }
                        )
                        continue

                    close_model_span()
                    if not isinstance(event, RunItemStreamEvent):
                        continue

                    item = event.item
                    item_type = getattr(item, "type", None)

                    if item_type == "tool_call_item":
                        with trace_span(
                            "tool.call",
                            attrs={
                                "task_id": self.task_id,
                                "session_id": self.session_id,
                                "caller": "layered_planner",
                            },
                        ) as span:
                            event_payload = self._parse_tool_call(item)
                            tool_args = event_payload["tool_args"]
                            span.set_attributes(
                                {
                                    "tool_name": event_payload["tool_name"],
                                    "tool_arg_keys": sorted(tool_args.keys())
                                    if isinstance(tool_args, dict)
                                    else [],
                                    "tool_args_preview": summarize_text(
                                        json.dumps(
                                            tool_args,
                                            ensure_ascii=False,
                                            default=str,
                                        ),
                                        limit=512,
                                    ),
                                }
                            )
                            self._current_tool_call = {
                                "name": event_payload["tool_name"],
                                "args": event_payload["tool_args"],
                            }
                        yield {
                            "type": "tool_call",
                            "payload": event_payload,
                        }
                    elif item_type == "tool_call_output_item":
                        output = getattr(item, "output", "")
                        tool_name = (
                            self._current_tool_call["name"]
                            if self._current_tool_call
                            else "unknown"
                        )
                        with trace_span(
                            "tool.result",
                            attrs={
                                "task_id": self.task_id,
                                "session_id": self.session_id,
                                "caller": "layered_planner",
                                "tool_name": tool_name,
                            },
                        ) as span:
                            result_text, steps, sub_success = self._parse_tool_output(
                                output
                            )
                            span.set_attributes(
                                {
                                    "steps": steps,
                                    "success": sub_success,
                                    "result_preview": summarize_text(result_text, 512),
                                }
                            )
                            tool_result_payload: dict[str, Any] = {
                                "tool_name": tool_name,
                                "result": result_text,
                            }
                            if steps:
                                tool_result_payload["steps"] = steps
                            if sub_success is not None:
                                tool_result_payload["success"] = sub_success
                            self._current_tool_call = None
                        yield {
                            "type": "tool_result",
                            "payload": tool_result_payload,
                        }
                    elif item_type == "message_output_item":
                        with trace_span(
                            "layered.planner.message",
                            attrs={
                                "task_id": self.task_id,
                                "session_id": self.session_id,
                            },
                        ) as span:
                            content = self._extract_message_content(item)
                            span.set_attribute(
                                "content_preview",
                                summarize_text(content, 512),
                            )
                        if content:
                            yield {
                                "type": "message",
                                "payload": {"content": content},
                            }

                close_model_span()
                if self.cancelled:
                    self.final_output = "Task cancelled by user"
                    self.success = False
                    yield {
                        "type": "cancelled",
                        "payload": {"message": self.final_output},
                    }
                    return

                self.final_output = (
                    self.result.final_output
                    if hasattr(self.result, "final_output")
                    else ""
                )
                self.success = True
                stream_span.set_attributes(
                    {
                        "success": True,
                        "final_output_preview": summarize_text(
                            self.final_output,
                            512,
                        ),
                    }
                )
                yield {
                    "type": "done",
                    "payload": {
                        "content": self.final_output,
                        "success": True,
                    },
                }
        except asyncio.CancelledError:
            close_model_span()
            self.cancelled = True
            raise
        except Exception as exc:
            if self.cancelled:
                close_model_span()
                self.final_output = "Task cancelled by user"
                self.success = False
                yield {
                    "type": "cancelled",
                    "payload": {"message": self.final_output},
                }
            else:
                error_details = await serialize_model_error_async(
                    exc,
                    model_config=_planner_model_config(),
                    call_site="AutoGLM_GUI.layered_agent_service.LayeredTaskRun.stream_events",
                )
                if model_span is not None:
                    model_span.set_attributes(trace_error_attrs(error_details))
                close_model_span()
                logger.exception(f"[LayeredAgent] Error: {exc}")
                self.final_output = str(exc)
                self.success = False
                yield {
                    "type": "error",
                    "payload": {
                        "message": str(exc),
                        "error_details": error_details,
                    },
                }
        finally:
            for task in (next_task, cancel_task):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            if iterator is not None:
                aclose = getattr(iterator, "aclose", None)
                if callable(aclose):
                    with contextlib.suppress(Exception):
                        close_result = aclose()
                        if inspect.isawaitable(close_result):
                            await close_result
            with _active_runs_lock:
                _active_runs.pop(self.task_id, None)
            self.finished = True

    @staticmethod
    def _parse_tool_output(output: Any) -> tuple[str, int, bool | None]:
        """Unpack a tool output into ``(text, steps, sub_success)``.

        The ``chat`` tool returns a JSON envelope ``{"result", "steps", "success"}``
        describing the inner phone-agent run.  Other tools (e.g. ``list_devices``)
        return plain strings, in which case ``steps`` is ``0`` and ``sub_success``
        is ``None``.
        """
        text = output if isinstance(output, str) else str(output)
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return text, 0, None
        if isinstance(parsed, dict) and "steps" in parsed:
            raw_steps = parsed.get("steps", 0)
            steps = int(raw_steps) if isinstance(raw_steps, (int, float)) else 0
            success = parsed.get("success")
            return text, steps, success if isinstance(success, bool) else None
        return text, 0, None

    def _parse_tool_call(self, item: Any) -> dict[str, Any]:
        tool_name = "unknown"
        tool_args: dict[str, Any] = {}

        if hasattr(item, "raw_item") and item.raw_item:
            raw = item.raw_item
            if isinstance(raw, dict):
                tool_name = raw.get(
                    "name", raw.get("function", {}).get("name", "unknown")
                )
                args_str = raw.get(
                    "arguments", raw.get("function", {}).get("arguments", "{}")
                )
                tool_args = self._parse_json_args(args_str)
            else:
                func = getattr(raw, "function", None)
                if func:
                    tool_name = getattr(func, "name", "unknown")
                    args_val = getattr(func, "arguments", None)
                    if args_val:
                        tool_args = self._parse_json_args(args_val)
                else:
                    name_val = getattr(raw, "name", None)
                    if name_val:
                        tool_name = name_val
                        args_val = getattr(raw, "arguments", None)
                        if args_val:
                            tool_args = self._parse_json_args(args_val)

        if tool_name == "unknown":
            if hasattr(item, "name") and item.name:
                tool_name = item.name
            elif hasattr(item, "call") and item.call:
                call = item.call
                if hasattr(call, "function") and call.function:
                    if hasattr(call.function, "name"):
                        tool_name = call.function.name
                    if hasattr(call.function, "arguments"):
                        tool_args = self._parse_json_args(call.function.arguments)
                elif hasattr(call, "name"):
                    tool_name = call.name
                    if hasattr(call, "arguments"):
                        tool_args = self._parse_json_args(call.arguments)

        logger.info(
            f"[LayeredAgent] Tool call: {tool_name}, args keys: {list(tool_args.keys())}"
        )
        return {
            "tool_name": tool_name,
            "tool_args": tool_args,
        }

    @staticmethod
    def _parse_json_args(args_val: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(args_val) if isinstance(args_val, str) else args_val
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        except Exception:
            return {"raw": str(args_val)}

    @staticmethod
    def _extract_message_content(item: Any) -> str:
        content = ""
        raw_item = getattr(item, "raw_item", None)
        if raw_item:
            raw_content = getattr(raw_item, "content", None)
            if raw_content:
                for chunk in raw_content:
                    text_val = getattr(chunk, "text", None)
                    if text_val:
                        content += text_val
        return content


async def start_run(
    *,
    task_id: str,
    session_id: str,
    message: str,
    device_id: str | None = None,
) -> LayeredTaskRun:
    agent = _ensure_agent()
    session = _get_or_create_session(session_id)

    # 在构建 Runner 之前修剪历史, 防止长任务下 planner 上下文无限膨胀
    # 而触发决策模型服务端内存守护 (process memory limit exceeded)。
    await _trim_session_history(session)

    effective_config = config_manager.get_effective_config()
    max_turns = effective_config.layered_max_turns
    resolved_max_turns = max_turns if max_turns is not None else 100000
    with trace_span(
        "layered.planner.run_streamed",
        attrs={
            "task_id": task_id,
            "session_id": session_id,
            "device_id": device_id,
            "max_turns": resolved_max_turns,
            "message_preview": summarize_text(message, 512),
        },
    ):
        result = Runner.run_streamed(
            agent,
            message,
            max_turns=resolved_max_turns,
            session=session,
        )

    with _active_runs_lock:
        _active_runs[task_id] = result

    return LayeredTaskRun(
        task_id=task_id,
        session_id=session_id,
        result=result,
        device_id=device_id,
    )


async def cancel_task(task_id: str) -> bool:
    with _active_runs_lock:
        result = _active_runs.get(task_id)
    if result is None:
        return False

    if hasattr(result, "cancel"):
        if asyncio.iscoroutinefunction(result.cancel):
            await result.cancel()
        else:
            result.cancel(mode="immediate")

    logger.info(f"[LayeredAgent] Aborted task: {task_id}")
    return True
