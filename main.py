"""NapCat / OneBot v11 掉线邮件通知。

设计目标（与上游 VanillaNahida/astrbot_plugin_offline_notice 的差异）：
- 只服务 aiocqhttp / NapCat，删掉 SnowLuma Webhook 与全部相关代码
- 不发上线/恢复邮件：掉线通常需要人工处理，避免恢复邮件变成噪音
- 一次掉线只发一封邮件；发送失败会在下一次心跳重试
- 只用 Python 标准库（smtplib / email / ssl），无第三方依赖
"""

from __future__ import annotations

import asyncio
import html
import json
import socket
import time
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from smtplib import SMTP, SMTP_SSL
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools

from .core.logic import (
    BotState,
    mark_notified,
    on_failure,
    on_reported_offline,
    on_success,
    render_template,
)

PLUGIN_NAME = "astrbot_plugin_napcat_offline_mail"
REPO_URL = "https://github.com/Geekertao-bot/astrbot_plugin_napcat_offline_mail"


@register(
    PLUGIN_NAME,
    "Geekertao-bot",
    "NapCat 掉线邮件通知（基于 VanillaNahida/astrbot_plugin_offline_notice 改造）",
    "1.0.0",
    REPO_URL,
)
class NapcatOfflineMailPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._task: asyncio.Task | None = None
        self._states: dict[str, BotState] = {}
        self._save_lock = asyncio.Lock()

        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = data_dir / "state.json"
        self._load_state()

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self):
        self._task = asyncio.create_task(self._heartbeat_loop())
        logger.info(
            "[napcat-offline-mail] 心跳检测已启动，间隔 %s 秒，失败阈值 %s",
            self._cfg_int("heartbeat_interval", 60),
            self._cfg_int("fail_threshold", 3),
        )

    async def terminate(self):
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("[napcat-offline-mail] 已停止")

    async def _heartbeat_loop(self):
        while True:
            try:
                await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[napcat-offline-mail] 心跳检测异常")
            await asyncio.sleep(max(15, self._cfg_int("heartbeat_interval", 60)))

    # ------------------------------------------------------------------ 配置
    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _email_cfg(self) -> dict:
        raw = self.config.get("email_config") or {}
        return raw if isinstance(raw, dict) else {}

    def _template_cfg(self) -> dict:
        raw = self.config.get("email_template") or {}
        return raw if isinstance(raw, dict) else {}

    # ------------------------------------------------------------------ 状态存取
    def _load_state(self):
        try:
            if not self._state_file.exists():
                return
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
            bots = payload.get("bots") if isinstance(payload, dict) else None
            if isinstance(bots, dict):
                self._states = {
                    str(key): BotState.from_dict(value)
                    for key, value in bots.items()
                    if isinstance(value, dict)
                }
        except Exception:
            logger.warning("[napcat-offline-mail] 读取状态文件失败", exc_info=True)

    async def _save_state(self):
        async with self._save_lock:
            payload = {
                "updated_at": time.time(),
                "bots": {key: state.to_dict() for key, state in self._states.items()},
            }
            try:
                tmp = self._state_file.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp.replace(self._state_file)
            except Exception:
                logger.warning("[napcat-offline-mail] 保存状态文件失败", exc_info=True)

    # ------------------------------------------------------------------ 检测
    def _aiocqhttp_platforms(self) -> list:
        try:
            platforms = self.context.platform_manager.get_insts()
        except Exception:
            logger.exception("[napcat-offline-mail] 获取平台实例失败")
            return []
        return [
            platform
            for platform in platforms
            if platform.__class__.__name__ == "AiocqhttpAdapter"
        ]

    @staticmethod
    def _platform_key(platform, index: int) -> str:
        for attr in ("platform_id", "id"):
            value = getattr(platform, attr, None)
            if value:
                return str(value)
        bot = getattr(platform, "bot", None)
        self_id = getattr(bot, "self_id", None) if bot is not None else None
        if self_id:
            return f"aiocqhttp:{self_id}"
        return f"aiocqhttp:{index}"

    async def _check_once(self):
        platforms = self._aiocqhttp_platforms()
        now = time.time()
        if not platforms:
            if self._cfg_bool("alert_when_no_adapter", False):
                await self._handle_failure(
                    "__no_adapter__",
                    "AstrBot 里没有发现 aiocqhttp 适配器实例",
                    now,
                    platform_label="aiocqhttp",
                )
            else:
                logger.debug("[napcat-offline-mail] 未发现 aiocqhttp 适配器，跳过")
            return

        for index, platform in enumerate(platforms):
            key = self._platform_key(platform, index)
            try:
                client = platform.get_client()
            except Exception as exc:
                await self._handle_failure(key, f"获取客户端失败: {exc}", now)
                continue
            if client is None or getattr(client, "api", None) is None:
                await self._handle_failure(key, "客户端或 OneBot API 不可用", now)
                continue

            try:
                status = await client.api.call_action("get_status")
            except Exception as exc:
                await self._handle_failure(
                    key, f"get_status 调用失败: {type(exc).__name__}: {exc}", now
                )
                continue

            online = bool(status.get("online", False)) if isinstance(status, dict) else False
            if not online:
                await self._handle_reported_offline(
                    key, "get_status 返回 online=false", now
                )
                continue

            await self._handle_online(key, client, now)

    async def _handle_online(self, key: str, client, now: float):
        nickname = ""
        bot_id = ""
        try:
            info = await client.api.call_action("get_login_info")
            if isinstance(info, dict):
                nickname = str(info.get("nickname") or "")
                bot_id = str(info.get("user_id") or "")
        except Exception:
            logger.debug("[napcat-offline-mail] get_login_info 失败", exc_info=True)

        state = self._states.get(key, BotState())
        was_offline = not state.online
        self._states[key] = on_success(
            state, now, nickname=nickname, bot_id=bot_id
        )
        await self._save_state()
        if was_offline:
            # 按需求不发上线邮件，只记录日志并重置“本次掉线已通知”标记
            logger.info(
                "[napcat-offline-mail] %s 已恢复在线（不发恢复邮件）", key
            )

    async def _handle_failure(
        self,
        key: str,
        reason: str,
        now: float,
        *,
        platform_label: str = "",
    ):
        threshold = max(1, self._cfg_int("fail_threshold", 3))
        state = self._states.get(key, BotState())
        new_state, should_notify = on_failure(state, reason, threshold, now=now)
        if platform_label:
            new_state.bot_id = new_state.bot_id or platform_label
        self._states[key] = new_state
        await self._save_state()

        if should_notify:
            ok = await self._send_alert(key, new_state, reason)
            if ok:
                self._states[key] = mark_notified(new_state, now)
                await self._save_state()
                logger.warning(
                    "[napcat-offline-mail] %s 连续 %s 次失败，离线邮件已发送",
                    key,
                    new_state.fail_count,
                )
            else:
                logger.error(
                    "[napcat-offline-mail] %s 离线邮件发送失败，下轮重试", key
                )

    async def _handle_reported_offline(self, key: str, reason: str, now: float):
        state = self._states.get(key, BotState())
        new_state, should_notify = on_reported_offline(state, reason, now=now)
        self._states[key] = new_state
        await self._save_state()

        if should_notify:
            ok = await self._send_alert(key, new_state, reason)
            if ok:
                self._states[key] = mark_notified(new_state, now)
                await self._save_state()
                logger.warning("[napcat-offline-mail] %s 离线，邮件已发送", key)
            else:
                logger.error(
                    "[napcat-offline-mail] %s 离线邮件发送失败，下轮重试", key
                )

    # ------------------------------------------------------------------ 邮件
    def _recipients(self) -> list[str]:
        raw = self._email_cfg().get("recipients") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(item).strip() for item in raw if str(item).strip()]

    def _render_mail(
        self, key: str, state: BotState, reason: str
    ) -> tuple[str, str]:
        template = self._template_cfg()
        last_success = (
            datetime.fromtimestamp(state.last_success).strftime("%Y-%m-%d %H:%M:%S")
            if state.last_success
            else "未知"
        )
        values = {
            "bot_id": html.escape(state.bot_id or key),
            "nickname": html.escape(state.nickname or "未知"),
            "platform": "NapCat / OneBot v11",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "reason": html.escape(reason or state.last_reason or "未知"),
            "last_success": last_success,
            "fail_count": str(state.fail_count),
            "hostname": html.escape(socket.gethostname()),
        }
        subject = render_template(
            template.get("subject")
            or "【NapCat 掉线】{{nickname}}（{{bot_id}}）于 {{timestamp}} 离线",
            values,
        )
        body = render_template(
            template.get("body") or self._default_body(),
            values,
        )
        return subject, body

    @staticmethod
    def _default_body() -> str:
        return (
            '<div style="max-width:620px;margin:0 auto;font-family:Arial,sans-serif;color:#333;">'
            '<div style="background:#c0392b;padding:18px 22px;border-radius:8px 8px 0 0;">'
            '<h2 style="color:#fff;margin:0;font-size:20px;">NapCat 掉线告警</h2></div>'
            '<div style="background:#fff;padding:22px;border:1px solid #ddd;border-top:none;'
            'border-radius:0 0 8px 8px;">'
            '<p style="line-height:1.7;">检测到 NapCat / OneBot v11 连接已离线，需要人工处理'
            "（重新扫码、重启 NapCat 或检查网络）。</p>"
            '<table style="width:100%;border-collapse:collapse;margin-top:14px;font-size:14px;">'
            '<tr><td style="padding:9px 12px;background:#f8f9fa;border:1px solid #e9ecef;'
            'font-weight:bold;width:110px;">Bot</td><td style="padding:9px 12px;'
            'border:1px solid #e9ecef;">{{nickname}}（{{bot_id}}）</td></tr>'
            '<tr><td style="padding:9px 12px;background:#f8f9fa;border:1px solid #e9ecef;'
            'font-weight:bold;">平台</td><td style="padding:9px 12px;border:1px solid #e9ecef;">'
            "{{platform}}</td></tr>"
            '<tr><td style="padding:9px 12px;background:#f8f9fa;border:1px solid #e9ecef;'
            'font-weight:bold;">离线时间</td><td style="padding:9px 12px;border:1px solid #e9ecef;">'
            "{{timestamp}}</td></tr>"
            '<tr><td style="padding:9px 12px;background:#f8f9fa;border:1px solid #e9ecef;'
            'font-weight:bold;">最后在线</td><td style="padding:9px 12px;border:1px solid #e9ecef;">'
            "{{last_success}}</td></tr>"
            '<tr><td style="padding:9px 12px;background:#f8f9fa;border:1px solid #e9ecef;'
            'font-weight:bold;">原因</td><td style="padding:9px 12px;border:1px solid #e9ecef;">'
            "{{reason}}</td></tr>"
            '<tr><td style="padding:9px 12px;background:#f8f9fa;border:1px solid #e9ecef;'
            'font-weight:bold;">连续失败</td><td style="padding:9px 12px;'
            'border:1px solid #e9ecef;">{{fail_count}} 次</td></tr>'
            "</table>"
            '<p style="color:#888;font-size:12px;margin-top:16px;">主机：{{hostname}} · '
            "本次掉线只发送这一封邮件，恢复后不会另发上线提醒。</p>"
            "</div></div>"
        )

    async def _send_alert(self, key: str, state: BotState, reason: str) -> bool:
        recipients = self._recipients()
        if not recipients:
            logger.warning("[napcat-offline-mail] 未配置收件人，跳过离线邮件")
            return False
        subject, body = self._render_mail(key, state, reason)
        return await self._send_email(subject, body, recipients)

    async def _send_email(
        self, subject: str, body_html: str, recipients: list[str]
    ) -> bool:
        cfg = self._email_cfg()
        sender = str(cfg.get("sender") or "")
        username = str(cfg.get("username") or sender)
        password = str(cfg.get("password") or "")
        display_name = str(cfg.get("display_name") or "NapCat 掉线告警")
        smtp_server = str(cfg.get("smtp_server") or "")
        smtp_port = int(cfg.get("smtp_port") or 465)
        encryption = str(cfg.get("encryption") or "SSL")

        if not all([sender, password, smtp_server, recipients]):
            logger.error(
                "[napcat-offline-mail] 邮件配置不完整（sender/password/smtp_server/recipients）"
            )
            return False

        msg = MIMEText(body_html, "html", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((display_name, sender))
        msg["To"] = ", ".join(recipients)

        def _sync_send() -> None:
            if encryption == "SSL":
                server = SMTP_SSL(smtp_server, smtp_port, timeout=30)
            else:
                server = SMTP(smtp_server, smtp_port, timeout=30)
                if encryption == "TLS":
                    server.starttls()
            try:
                server.login(username, password)
                server.sendmail(sender, recipients, msg.as_string())
            finally:
                try:
                    server.quit()
                except Exception:
                    pass

        try:
            await asyncio.to_thread(_sync_send)
            logger.info(
                "[napcat-offline-mail] 邮件已发送至 %s", ", ".join(recipients)
            )
            return True
        except Exception:
            logger.exception("[napcat-offline-mail] 发送邮件失败")
            return False

    # ------------------------------------------------------------------ 指令
    @staticmethod
    def _command_args(event: AstrMessageEvent) -> str:
        text = (event.get_message_str() or "").strip()
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _render_status(self) -> str:
        if not self._states:
            return "还没有检测记录（插件可能刚启动）。"
        lines = ["NapCat 掉线监控状态："]
        for key, state in self._states.items():
            last_success = (
                datetime.fromtimestamp(state.last_success).strftime("%m-%d %H:%M:%S")
                if state.last_success
                else "无"
            )
            lines.append(
                f"- {state.nickname or key}（{state.bot_id or key}）："
                f"{'在线' if state.online else '离线'} · "
                f"失败 {state.fail_count} 次 · 最后在线 {last_success} · "
                f"{'已发过邮件' if state.notified else '本次未发邮件'}"
            )
        return "\n".join(lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("掉线邮件", alias={"napcat掉线", "offline_mail"})
    async def cmd_offline_mail(self, event: AstrMessageEvent):
        """NapCat 掉线邮件：状态 / 测试 / 检测。"""
        args = self._command_args(event)
        parts = args.split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not action or action in {"状态", "status"}:
            yield event.plain_result(self._render_status())
            return

        if action in {"检测", "check"}:
            await self._check_once()
            yield event.plain_result("已执行一次检测。\n" + self._render_status())
            return

        if action in {"测试", "test"}:
            recipients = [arg] if arg else self._recipients()[:1]
            if not recipients or "@" not in recipients[0]:
                yield event.plain_result(
                    "请提供测试邮箱，例如：/掉线邮件 测试 you@example.com"
                )
                return
            subject, body = self._render_mail(
                "test", BotState(nickname="测试", bot_id="0"), "这是一封测试邮件"
            )
            ok = await self._send_email(subject, body, recipients)
            yield event.plain_result(
                f"✅ 测试邮件已发送至 {recipients[0]}"
                if ok
                else "❌ 发送失败，请检查 SMTP 配置和日志。"
            )
            return

        yield event.plain_result(
            "用法：\n"
            "/掉线邮件 状态\n"
            "/掉线邮件 检测\n"
            "/掉线邮件 测试 you@example.com"
        )
