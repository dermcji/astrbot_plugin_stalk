# 更新日志

版本号与 [Releases](https://github.com/dermcji/astrbot_plugin_stalk/releases) 的 tag 一致，新版本在上。

## 1.0.0

首个版本。

**新增**

- 盯住指定群友：距上次说话超过 `stalk_gap_minutes`（默认 120 分钟）后第一次冒泡才动手，不是一说话就响应。
- `stalk_action` 四种动作：`poke`（连戳）、`at`（@ 说话）、`both`（先说再戳）、`say`（只说话不 @）。
- 说话时间落盘，重启不丢；刚装好时只记录不动手，避免重装后刷屏。
- `/视奸 加|删|列表` 管理名单，`/戳一下 <QQ>` 自检连戳通道。
- 纯标准库实现，无第三方依赖。
