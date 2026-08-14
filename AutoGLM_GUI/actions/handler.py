"""Action handler for executing phone operations."""

import base64
import io
import random
from typing import Any
from collections.abc import Callable

from AutoGLM_GUI.adb.timing import TIMING_CONFIG
from AutoGLM_GUI.device_protocol import DeviceProtocol
from AutoGLM_GUI.logger import logger
from AutoGLM_GUI.trace import trace_sleep, trace_span

from .types import ActionResult


# 滑动到底判定：两张截图缩略后平均像素差低于该阈值即视为"画面几乎未变"
BROWSE_NOTE_SIMILAR_DIFF_THRESHOLD = 3.0
_BROWSE_NOTE_THUMB_SIZE = (64, 64)


def images_similar(
    base64_a: str,
    base64_b: str,
    threshold: float = BROWSE_NOTE_SIMILAR_DIFF_THRESHOLD,
) -> bool:
    """判断两张 base64 截图是否几乎相同（用于检测滑动是否已到最后一张）。

    做法：各自解码 -> 灰度 -> 缩略到固定小尺寸 -> 逐像素平均绝对差，
    低于 threshold 认为画面基本未变化。任何异常都返回 False（不误判为到底）。
    """
    if not base64_a or not base64_b:
        return False
    try:
        from PIL import Image

        def _load(data: str) -> Image.Image:
            raw = base64.b64decode(data)
            img = Image.open(io.BytesIO(raw)).convert("L")
            return img.resize(_BROWSE_NOTE_THUMB_SIZE)

        img_a = _load(base64_a)
        img_b = _load(base64_b)
        pixels_a = list(img_a.getdata())
        pixels_b = list(img_b.getdata())
        if not pixels_a or len(pixels_a) != len(pixels_b):
            return False
        total = sum(abs(pa - pb) for pa, pb in zip(pixels_a, pixels_b))
        avg_diff = total / len(pixels_a)
        return avg_diff < threshold
    except Exception as e:  # noqa: BLE001
        logger.debug(f"images_similar failed, treat as not-similar: {e}")
        return False


class ActionHandler:
    def __init__(
        self,
        device: DeviceProtocol,
        confirmation_callback: Callable[[str], bool] | None = None,
        takeover_callback: Callable[[str], None] | None = None,
    ):
        self.device = device
        self.confirmation_callback = confirmation_callback or self._default_confirmation
        self.takeover_callback = takeover_callback or self._default_takeover

    def execute(
        self, action: dict[str, Any], screen_width: int, screen_height: int
    ) -> ActionResult:
        action_type = action.get("_metadata")
        action_name = action.get("action")
        with trace_span(
            "action.execute",
            attrs={
                "action_type": action_type,
                "action_name": action_name,
                "screen_width": screen_width,
                "screen_height": screen_height,
            },
        ) as span:
            if action_type == "finish":
                result = ActionResult(
                    success=True, should_finish=True, message=action.get("message")
                )
                span.set_attributes(
                    {
                        "success": result.success,
                        "should_finish": result.should_finish,
                    }
                )
                return result

            if action_type != "do":
                result = ActionResult(
                    success=False,
                    should_finish=True,
                    message=f"Unknown action type: {action_type}",
                )
                span.set_attributes(
                    {
                        "success": result.success,
                        "should_finish": result.should_finish,
                    }
                )
                return result

            if not isinstance(action_name, str) or not action_name:
                result = ActionResult(
                    success=False,
                    should_finish=False,
                    message=f"Unknown action: {action_name}",
                )
                span.set_attributes(
                    {
                        "success": result.success,
                        "should_finish": result.should_finish,
                    }
                )
                return result

            handler_method = self._get_handler(action_name)

            if handler_method is None:
                result = ActionResult(
                    success=False,
                    should_finish=False,
                    message=f"Unknown action: {action_name}",
                )
                span.set_attributes(
                    {
                        "success": result.success,
                        "should_finish": result.should_finish,
                    }
                )
                return result

            try:
                result = handler_method(action, screen_width, screen_height)
                span.set_attributes(
                    {
                        "success": result.success,
                        "should_finish": result.should_finish,
                    }
                )
                return result
            except Exception as e:
                result = ActionResult(
                    success=False, should_finish=False, message=f"Action failed: {e}"
                )
                span.set_attributes(
                    {
                        "success": result.success,
                        "should_finish": result.should_finish,
                    }
                )
                return result

    def _get_handler(
        self, action_name: str
    ) -> Callable[[dict[str, Any], int, int], ActionResult] | None:
        handlers = {
            "Launch": self._handle_launch,
            "Tap": self._handle_tap,
            "Type": self._handle_type,
            "Type_Name": self._handle_type,
            "Swipe": self._handle_swipe,
            "Back": self._handle_back,
            "Home": self._handle_home,
            "Double Tap": self._handle_double_tap,
            "Long Press": self._handle_long_press,
            "Wait": self._handle_wait,
            "Take_over": self._handle_takeover,
            "Note": self._handle_note,
            "Call_API": self._handle_call_api,
            "Interact": self._handle_interact,
            "Browse_Note": self._handle_browse_note,
        }
        return handlers.get(action_name)

    def _convert_relative_to_absolute(
        self, element: list[int], screen_width: int, screen_height: int
    ) -> tuple[int, int]:
        clamped_x = max(0, min(element[0], 1000))
        clamped_y = max(0, min(element[1], 1000))
        x = int(clamped_x / 1000 * screen_width)
        y = int(clamped_y / 1000 * screen_height)
        return x, y

    def _handle_launch(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        app_name = action.get("app")
        if not app_name:
            return ActionResult(False, False, "No app name specified")

        success = self.device.launch_app(app_name)
        if success:
            return ActionResult(True, False)
        return ActionResult(False, False, f"App not found: {app_name}")

    def _handle_tap(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        x, y = self._convert_relative_to_absolute(element, width, height)

        if "message" in action:
            if not self.confirmation_callback(action["message"]):
                return ActionResult(
                    success=False,
                    should_finish=True,
                    message="User cancelled sensitive operation",
                )

        self.device.tap(x, y)
        return ActionResult(True, False)

    _ADB_IME = "com.android.adbkeyboard/.AdbIME"

    def _handle_type(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        text = action.get("text", "")

        original_ime = self.device.detect_and_set_adb_keyboard()
        need_restore = self._ADB_IME not in original_ime

        if need_restore:
            trace_sleep(
                TIMING_CONFIG.action.keyboard_switch_delay,
                name="sleep.keyboard_switch",
                attrs={"action_name": "Type"},
            )

        self.device.clear_text()
        trace_sleep(
            TIMING_CONFIG.action.text_clear_delay,
            name="sleep.text_clear_delay",
            attrs={"action_name": "Type"},
        )

        self.device.type_text(text)
        trace_sleep(
            TIMING_CONFIG.action.text_input_delay,
            name="sleep.text_input_delay",
            attrs={"action_name": "Type", "text_length": len(text)},
        )

        if need_restore:
            self.device.restore_keyboard(original_ime)
            trace_sleep(
                TIMING_CONFIG.action.keyboard_restore_delay,
                name="sleep.keyboard_restore_delay",
                attrs={"action_name": "Type"},
            )

        return ActionResult(True, False)

    def _handle_swipe(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        start = action.get("start")
        end = action.get("end")

        if not start or not end:
            return ActionResult(False, False, "Missing swipe coordinates")

        start_x, start_y = self._convert_relative_to_absolute(start, width, height)
        end_x, end_y = self._convert_relative_to_absolute(end, width, height)

        self.device.swipe(start_x, start_y, end_x, end_y)
        return ActionResult(True, False)

    def _handle_back(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        self.device.back()
        return ActionResult(True, False)

    def _handle_home(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        self.device.home()
        return ActionResult(True, False)

    def _handle_double_tap(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        x, y = self._convert_relative_to_absolute(element, width, height)
        self.device.double_tap(x, y)
        return ActionResult(True, False)

    def _handle_long_press(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        x, y = self._convert_relative_to_absolute(element, width, height)
        self.device.long_press(x, y)
        return ActionResult(True, False)

    MAX_WAIT_SECONDS = 30

    def _handle_wait(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        duration_str = action.get("duration", "1 seconds")
        try:
            duration = float(duration_str.replace("seconds", "").strip())
        except ValueError:
            duration = 1.0

        duration = min(duration, self.MAX_WAIT_SECONDS)
        trace_sleep(
            duration,
            name="sleep.wait_action",
            attrs={"action_name": "Wait"},
        )
        return ActionResult(True, False)

    def _handle_takeover(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        message = action.get("message", "User intervention required")
        self.takeover_callback(message)
        return ActionResult(True, False, message=f"TAKEOVER_REQUIRED:\n {message}")

    def _handle_note(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        return ActionResult(True, False)

    def _handle_call_api(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        """Handle API call action (placeholder for summarization)."""
        # This action is typically used for content summarization
        # Implementation depends on specific requirements
        return ActionResult(True, False)

    def _handle_interact(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        """Handle interaction request (user choice needed)."""
        return ActionResult(
            True, False, message="INTERACT_REQUIRED: User interaction required"
        )

    # === Composite Actions ===
    BROWSE_NOTE_SWIPE_COUNT = 30
    # 整套「打开+滑动+返回」流程的循环次数（写死）
    BROWSE_NOTE_REPEAT_COUNT = 3
    # 每次点击/滑动坐标的随机抖动范围（像素，±该值）
    BROWSE_NOTE_JITTER_PX = 8
    # 滑动图片的时长（毫秒）：越小滑动越快。默认按距离计算约 1000-2000ms 偏慢，
    # 这里显式指定一个较短时长以加快浏览图片的速度。
    BROWSE_NOTE_SWIPE_DURATION_MS = 100
    # 两次滑动之间的间隔（秒）随机范围：在 [min, max] 内取随机值，模拟真人操作节奏。
    BROWSE_NOTE_SWIPE_DELAY_MIN_S = 0.3
    BROWSE_NOTE_SWIPE_DELAY_MAX_S = 0.6
    # 向左滑动相对坐标（0-1000）：从屏幕右侧滑到左侧，保持在垂直中部
    _BROWSE_SWIPE_START = [850, 500]
    _BROWSE_SWIPE_END = [150, 500]

    def _jitter(
        self, x: int, y: int, width: int, height: int, amount: int
    ) -> tuple[int, int]:
        """对绝对像素坐标加 ±amount 的随机偏移，并 clamp 到屏幕范围内。"""
        jx = x + random.randint(-amount, amount)
        jy = y + random.randint(-amount, amount)
        jx = max(0, min(jx, width - 1))
        jy = max(0, min(jy, height - 1))
        return jx, jy

    def _handle_browse_note(
        self, action: dict[str, Any], width: int, height: int
    ) -> ActionResult:
        """浏览笔记详情复合动作：整套「1 次 Tap 打开 + N 次向左 Swipe + 1 次 Back」循环 REPEAT 次。

        每次点击与滑动的坐标都会加一个小范围随机偏移，模拟真人操作。

        参数:
            element: 笔记入口的相对坐标 [x, y]（必填），用于打开笔记详情。
            swipe_count: 向左滑动次数，可选，默认 30。
        """
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        swipe_count = action.get("swipe_count", self.BROWSE_NOTE_SWIPE_COUNT)
        try:
            swipe_count = int(swipe_count)
        except (TypeError, ValueError):
            swipe_count = self.BROWSE_NOTE_SWIPE_COUNT

        tap_x, tap_y = self._convert_relative_to_absolute(element, width, height)
        start_x, start_y = self._convert_relative_to_absolute(
            self._BROWSE_SWIPE_START, width, height
        )
        end_x, end_y = self._convert_relative_to_absolute(
            self._BROWSE_SWIPE_END, width, height
        )
        jitter = self.BROWSE_NOTE_JITTER_PX

        # 整套流程循环 REPEAT 次
        for _ in range(self.BROWSE_NOTE_REPEAT_COUNT):
            # 1) Tap 打开笔记（坐标加随机抖动）
            jx, jy = self._jitter(tap_x, tap_y, width, height, jitter)
            self.device.tap(jx, jy)
            # 2) 向左 Swipe 滑动图片；滑到最后一张（画面不再变化）则提前停止
            prev_shot = self._safe_screenshot()
            for _ in range(swipe_count):
                sx, sy = self._jitter(start_x, start_y, width, height, jitter)
                ex, ey = self._jitter(end_x, end_y, width, height, jitter)
                self.device.swipe(
                    sx,
                    sy,
                    ex,
                    ey,
                    duration_ms=self.BROWSE_NOTE_SWIPE_DURATION_MS,
                    delay=random.uniform(
                        self.BROWSE_NOTE_SWIPE_DELAY_MIN_S,
                        self.BROWSE_NOTE_SWIPE_DELAY_MAX_S,
                    ),
                )
                cur_shot = self._safe_screenshot()
                if (
                    prev_shot is not None
                    and cur_shot is not None
                    and images_similar(prev_shot, cur_shot)
                ):
                    # 画面几乎未变化，判定已到最后一张，停止本轮滑动
                    break
                prev_shot = cur_shot
            # 3) Back 返回
            self.device.back()

        return ActionResult(True, False)

    def _safe_screenshot(self) -> str | None:
        """获取当前截图的 base64；失败返回 None（不影响主流程）。"""
        try:
            shot = self.device.get_screenshot()
            return shot.base64_data if shot else None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Browse_Note screenshot failed: {e}")
            return None

    @staticmethod
    def _default_confirmation(message: str) -> bool:
        response = input(f"\n⚠️  Confirm action: {message} (y/n): ")
        return response.lower() in ("y", "yes")

    @staticmethod
    def _default_takeover(message: str) -> None:
        input(f"\n🤚 {message}. Press Enter to continue...")
