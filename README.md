# NapCat 掉线邮件（astrbot_plugin_napcat_offline_mail）

给 **NapCat / OneBot v11** 用的掉线邮件告警：定时调用 `get_status`，连续失败或明确离线时发一封邮件给管理员。

基于 [VanillaNahida/astrbot_plugin_offline_notice](https://github.com/VanillaNahida/astrbot_plugin_offline_notice) 改造，按 NapCat 场景做了减法：

- **删掉 SnowLuma（SL）Webhook** 和相关的 HTTP 服务器、解析逻辑
- **不发上线/恢复邮件**：掉线一般要人工扫码或重启，恢复提醒只会变成噪音
- **一次掉线只发一封邮件**：发送成功即标记，失败才在下一轮重试；重启不会重复轰炸
- **只依赖 Python 标准库**：`smtplib` / `email` / `ssl`，不需要 aiohttp 或第三方包
- 状态持久化到插件数据目录，AstrBot 重启后仍知道「这次掉线已经通知过」

## 安装

要求 AstrBot **4.16+**，并且已经配好 aiocqhttp（NapCat）适配器。

```text
https://github.com/Geekertao-bot/astrbot_plugin_napcat_offline_mail
```

WebUI → 插件 → 从仓库安装，填上面地址；或把目录放进 `AstrBot/data/plugins/` 后重载。

## 配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `heartbeat_interval` | 60 秒 | 每隔多久调用一次 `get_status` |
| `fail_threshold` | 3 | 连续失败多少次判离线（3 × 60 秒 ≈ 3 分钟） |
| `cooldown_minutes` | 10 | 两次告警的最小间隔，防止抖动刷邮件；0 = 不限制 |
| `alert_when_no_adapter` | 关 | 找不到 aiocqhttp 适配器时是否也发邮件 |
| `email_config.sender` | — | 发信地址 |
| `email_config.username` | — | SMTP 用户名 |
| `email_config.password` | — | SMTP 密码/授权码（配置页掩码） |
| `email_config.smtp_server` | — | 如 `smtp.qq.com` |
| `email_config.smtp_port` | 465 | SSL 465 / TLS 587 / 不加密 25 |
| `email_config.encryption` | SSL | SSL / TLS / 不启用 |
| `email_config.recipients` | `[]` | 收件人列表 |
| `email_template.subject` | 见配置页 | 支持变量 |
| `email_template.body` | 见配置页 | HTML 正文，支持变量 |

模板变量：

```text
{{bot_id}} {{nickname}} {{platform}} {{timestamp}}
{{reason}} {{last_success}} {{fail_count}} {{hostname}}
```

## 检测逻辑

每 `heartbeat_interval` 秒遍历一次 aiocqhttp 适配器：

1. 调 `get_status`
2. 调用成功且 `online=true` → 记为在线，清空失败计数、重置「已通知」标记（**不发恢复邮件**）
3. 调用异常 → 失败计数 +1；连续达到 `fail_threshold` 才判离线并发邮件
4. `get_status` 成功但 `online=false` → 立即判离线并发邮件

掉线原因会写进邮件：API 异常类型、`online=false`、客户端不可用等。

### 时间线示例

```text
00:00  正常，get_status → online=true
00:01  NapCat 进程被杀，get_status 超时（失败 1/3）
00:02  失败 2/3
00:03  失败 3/3 → 发邮件，标记本次掉线已通知
00:04  仍失败，但已通知 → 不再发
（人工重启 NapCat）
00:10  get_status 恢复 → 重置标记，不发恢复邮件
```

## 指令（管理员）

```text
/掉线邮件 状态            查看每个 Bot 的在线状态、失败次数、最后在线时间
/掉线邮件 检测            立即执行一次检测
/掉线邮件 测试 you@qq.com 发一封测试邮件
```

## 状态文件

```text
data/plugin_data/astrbot_plugin_napcat_offline_mail/state.json
```

记录每个 Bot 的在线状态、失败计数、最后成功时间、是否已发过本次掉线邮件。删掉它等于重置告警状态。

## 已知边界

- 只能检测 AstrBot 进程内能看到的东西：NapCat 进程退出、反向 WS 断开、QQ 掉登录都会让 `get_status` 失败或返回 offline，能覆盖
- 如果 AstrBot 本身挂了，插件也不会发邮件（需要宿主层面的看门狗）
- 邮件发送失败会在下一轮心跳重试，但不会无限重试：只有「未标记已通知」时才会发
- 没有自动重连/自动扫码：按设计只告警，恢复动作交给人

## License

MIT。原插件版权归 VanillaNahida；本改造版由 Geekertao-bot 维护。
