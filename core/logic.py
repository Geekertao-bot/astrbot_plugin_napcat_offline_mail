"""NapCat 掉线邮件插件的纯逻辑（不依赖 AstrBot，方便单测）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass
class BotState:
    online: bool = True
    notified: bool = False
    fail_count: int = 0
    last_success: float = 0.0
    last_reason: str = ""
    nickname: str = ""
    bot_id: str = ""
    notified_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict | None) -> "BotState":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            online=bool(raw.get("online", True)),
            notified=bool(raw.get("notified", False)),
            fail_count=int(raw.get("fail_count", 0) or 0),
            last_success=float(raw.get("last_success", 0) or 0),
            last_reason=str(raw.get("last_reason", "") or ""),
            nickname=str(raw.get("nickname", "") or ""),
            bot_id=str(raw.get("bot_id", "") or ""),
            notified_at=float(raw.get("notified_at", 0) or 0),
        )


def on_success(
    state: BotState,
    now: float,
    *,
    nickname: str = "",
    bot_id: str = "",
) -> BotState:
    """一次成功的 get_status 调用：恢复在线状态（不发恢复邮件）。"""
    return replace(
        state,
        online=True,
        notified=False,
        fail_count=0,
        last_success=now,
        nickname=nickname or state.nickname,
        bot_id=bot_id or state.bot_id,
        last_reason="",
    )


def on_failure(
    state: BotState,
    reason: str,
    threshold: int,
    *,
    now: float,
) -> tuple[BotState, bool]:
    """一次失败调用。返回 (新状态, 是否应该发离线邮件)。

    连续失败达到阈值才判离线；已经发过邮件的一次掉线不会重复发。
    """
    fail_count = state.fail_count + 1
    reach_threshold = fail_count >= max(1, int(threshold))
    new_state = replace(
        state,
        fail_count=fail_count,
        online=False if reach_threshold else state.online,
        last_reason=reason or state.last_reason,
    )
    should_notify = reach_threshold and not state.notified
    return new_state, should_notify


def on_reported_offline(
    state: BotState,
    reason: str,
    *,
    now: float,
) -> tuple[BotState, bool]:
    """get_status 明确返回 online=false：立即判离线。"""
    new_state = replace(state, online=False, last_reason=reason, fail_count=0)
    return new_state, not state.notified


def mark_notified(state: BotState, now: float) -> BotState:
    return replace(state, notified=True, notified_at=now)


def render_template(template: str, values: dict) -> str:
    """把 {{key}} 替换成变量值。"""
    result = str(template or "")
    for key, value in values.items():
        result = result.replace("{{" + key + "}}", str(value))
    return result


def unwrap_payload(payload):
    """OneBot 可能返回裸对象，也可能包一层 data。"""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def parse_online(payload) -> bool:
    """从 get_status 返回里解析 online，兼容 bool / 字符串 / data 包装。"""
    data = unwrap_payload(payload)
    if not isinstance(data, dict):
        return False
    value = data.get("online", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}
