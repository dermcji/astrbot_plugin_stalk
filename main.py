"""群友视奸：盯住某个人，他隔一阵子第一次冒泡时，@ 他或连戳他几下。

不监听「每一句话」——那样太吵。等的是「隔了一阵子第一次出现」：
记下他每次说话的时间，这次距上次超过阈值才算数，动完手把时间刷成现在，
所以他接着聊下去不会再被触发。

指令
----
/视奸                  看现在盯着谁、各自多久没说话了
/视奸 加 <QQ>          盯上这个人
/视奸 减 <QQ>          不盯了
/视奸 清               全清
/戳一下 <QQ>           手动戳一下，验证戳一戳通道通不通
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

try:  # @ 某人要用
    from astrbot.api.message_components import At
except ImportError:  # 老版本没有 At，退化成分纯文本
    At = None  # type: ignore

PLUGIN_NAME = "astrbot_plugin_stalk"
PLUGIN_VER = "1.0.1"
_DEFAULT_DATA = Path(__file__).parent / "data"
_STALK_FILE = "stalk.json"
# 发言时间最多留一周，再久的也没参考价值了
TS_KEEP = 86400 * 7

# 没有可用模型时兜底，{name} 换成对方群名片
FALLBACK_LINES = [
    "哟，{name}，多久没见你冒泡了。",
    "{name} 你终于出来了，还以为你退群了。",
    "稀客啊 {name}，这几天跑哪去了？",
    "抓到 {name} 了，别潜水了。",
    "{name} 一出来我就看见了。",
]


def _data_dir() -> Path:
    """插件数据目录。优先用 AstrBot 的 data/plugin_data，重装插件不丢。"""
    target: Path | None = None
    try:
        from astrbot.api.star import StarTools

        target = Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:  # noqa: BLE001 - 拿不到就用包内目录
        target = None
    if target is None:
        target = _DEFAULT_DATA
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        target = _DEFAULT_DATA
        target.mkdir(parents=True, exist_ok=True)
    # 从包内目录搬一次旧数据
    if target != _DEFAULT_DATA and _DEFAULT_DATA.is_dir():
        for old in _DEFAULT_DATA.glob("*.json"):
            new = target / old.name
            if not new.exists():
                try:
                    shutil.copy2(old, new)
                except OSError:
                    pass
    return target


def _load_json(name: str, default):
    try:
        with open(_data_dir() / name, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(name: str, data) -> None:
    path = _data_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    tmp.replace(path)


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "还没记录"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "时间无效"


def _fmt_gap(seconds: float) -> str:
    """把秒数说成「2 小时 15 分」这种。"""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时{f' {minutes} 分' if minutes else ''}"
    days, hours = divmod(hours, 24)
    return f"{days} 天{f' {hours} 小时' if hours else ''}"


def _at_me(event: AstrMessageEvent) -> bool:
    """消息链里是否真的 @ 了我。"""
    try:
        me = str(event.get_self_id())
        for comp in event.get_messages():
            t = getattr(comp, "type", "")
            if getattr(t, "name", t) == "At" and str(getattr(comp, "qq", "")) == me:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


@register(PLUGIN_NAME, "dsh", "盯住某个群友，他隔一阵子第一次冒泡时 @ 他或连戳他", PLUGIN_VER)
class StalkPeek(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        state = _load_json(_STALK_FILE, {})
        # 通过 /视奸 加 存下来的目标，与面板里填的合并
        self._extra: list[str] = [
            str(x).strip() for x in (state.get("targets") or []) if str(x).strip()
        ]
        self._last: dict[str, float] = {}
        for k, v in (state.get("last") or {}).items():
            try:
                self._last[str(k)] = float(v)
            except (TypeError, ValueError):
                continue

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default=None):
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        try:
            return self.config[key]
        except Exception:  # noqa: BLE001
            return default

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("stalk_enabled", True))

    @property
    def targets(self) -> list[str]:
        out: list[str] = []
        for x in self._extra:
            if x and x not in out:
                out.append(x)
        raw = self._cfg("stalk_qq", [])
        if isinstance(raw, str):
            raw = re.split(r"[,，\s]+", raw)
        for x in raw or []:
            s = str(x).strip()
            if s and s not in out:
                out.append(s)
        return out

    @property
    def gap(self) -> float:
        """多久没说话才算「隔一阵子第一次出现」。"""
        try:
            minutes = int(self._cfg("stalk_gap_minutes", 120) or 120)
        except (TypeError, ValueError):
            minutes = 120
        return max(1, minutes) * 60.0

    @property
    def action(self) -> str:
        """poke=连戳 / at=@他说一句 / both=先说再戳 / say=不@只说一句。"""
        act = str(self._cfg("stalk_action", "poke") or "poke").strip().lower()
        return act if act in ("poke", "at", "both", "say") else "poke"

    @property
    def poke_times(self) -> int:
        try:
            n = int(self._cfg("stalk_poke_times", 3) or 3)
        except (TypeError, ValueError):
            n = 3
        return max(1, min(n, 10))

    @property
    def poke_interval(self) -> float:
        try:
            gap = float(self._cfg("stalk_poke_interval", 0.8) or 0.8)
        except (TypeError, ValueError):
            gap = 0.8
        return max(0.2, min(gap, 5.0))

    @property
    def lines(self) -> list[str]:
        raw = self._cfg("stalk_lines", [])
        if isinstance(raw, str):
            raw = [s for s in raw.splitlines() if s.strip()]
        return [str(s).strip() for s in (raw or []) if str(s).strip()]

    @property
    def groups(self) -> set[str]:
        raw = self._cfg("stalk_groups", [])
        if isinstance(raw, str):
            raw = re.split(r"[,，\s]+", raw)
        return {str(g).strip() for g in (raw or []) if str(g).strip()}

    @property
    def allowed_qq(self) -> set[str]:
        raw = self._cfg("allowed_qq", [])
        if isinstance(raw, str):
            raw = re.split(r"[,，\s]+", raw)
        return {str(x).strip() for x in (raw or []) if str(x).strip()}

    @property
    def allowed_groups(self) -> set[str]:
        raw = self._cfg("allowed_groups", [])
        if isinstance(raw, str):
            raw = re.split(r"[,，\s]+", raw)
        return {str(x).strip() for x in (raw or []) if str(x).strip()}

    def _allowed(self, event: AstrMessageEvent) -> bool:
        """谁能调整这个插件。留空就是不限制。"""
        qq = self.allowed_qq
        if qq and str(event.get_sender_id() or "") not in qq:
            logger.info(f"[stalk] 拦下非白名单用户 {event.get_sender_id()}")
            return False
        groups = self.allowed_groups
        gid = str(event.get_group_id() or "")
        if groups and gid and gid not in groups:
            logger.info(f"[stalk] 拦下非白名单群 {gid}")
            return False
        return True

    # ------------------------------------------------------------------ 状态

    def _save(self) -> None:
        cutoff = time.time() - TS_KEEP
        self._last = {k: v for k, v in self._last.items() if float(v) >= cutoff}
        try:
            _save_json(_STALK_FILE, {"targets": list(self._extra), "last": self._last})
        except OSError as e:
            # 写不进去也不能把消息处理带崩，内存里的时间戳还够用
            logger.warning(f"[stalk] 状态没写进磁盘: {e}")

    # ------------------------------------------------------------------ 动作

    async def _poke(self, event: AstrMessageEvent, user_id: str, group_id: str) -> int:
        """戳一戳。返回真正戳成功的次数（0 表示这条通道不通）。"""
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            logger.warning("[stalk] 当前平台适配器没有 call_action，戳不了")
            return 0
        params: dict = {}
        try:
            params["user_id"] = int(user_id)
            if group_id:
                params["group_id"] = int(group_id)
        except (TypeError, ValueError):
            logger.warning(f"[stalk] 戳一戳的号码不合法: {user_id}/{group_id}")
            return 0
        done = 0
        for i in range(self.poke_times):
            try:
                await bot.call_action("send_poke", **params)
                done += 1
            except Exception as e:  # noqa: BLE001 - NapCat 不支持会直接抛
                logger.warning(f"[stalk] 戳一戳失败: {e}")
                break
            if i + 1 < self.poke_times:
                await asyncio.sleep(self.poke_interval)
        return done

    async def _persona(self, umo: str) -> str:
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        try:
            persona = await pm.get_default_persona_v3(umo)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[stalk] 读取人设失败: {e}")
            return ""
        if isinstance(persona, dict):
            return str(persona.get("prompt") or "")
        return str(getattr(persona, "prompt", "") or "")

    async def _line(self, umo: str, name: str, said: str, gap: float) -> str:
        """动手时说的那句话。配了话术就随机挑，没配就让人格模型现编。"""
        lines = self.lines
        if lines:
            return random.choice(lines).replace("{name}", name)
        fallback = random.choice(FALLBACK_LINES).replace("{name}", name)
        try:
            provider = self.context.get_using_provider(umo=umo)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[stalk] 取模型失败，用兜底话术: {e}")
            provider = None
        if provider is None:
            return fallback
        hours = max(1, int(gap // 3600))
        prompt = (
            f"群友「{name}」大概 {hours} 小时没在群里说话了，刚才突然冒了一句"
            f"「{said or '（没说话，只是出现了）'}」。\n"
            "用你自己的语气跟他打个招呼：可以带点惦记、调侃或者装熟，"
            "一两句话，别用括号写动作，别提自己是 AI，别解释你在干什么。"
        )
        try:
            persona = await self._persona(umo)
        except Exception:  # noqa: BLE001
            persona = ""
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=persona or "", contexts=[]),
                timeout=30,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            text = text.strip('"“”「」\'')
            return text[:150] or fallback
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[stalk] 生成话术失败，用兜底: {e}")
            return fallback

    # ------------------------------------------------------------------ 主逻辑

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def watch(self, event: AstrMessageEvent):
        """被盯的人隔一阵子第一次冒泡时动手。

        只在群里生效——「隔一段时间没说话」在私聊里没有意义。
        时间戳落盘，重启后不会把「刚说过话」误判成「好久没见」。
        """
        if not self.enabled:
            return
        targets = self.targets
        if not targets:
            return
        sender = str(event.get_sender_id() or "")
        if sender not in targets:
            return
        gid = str(event.get_group_id() or "")
        if not gid:
            return
        allow = self.groups
        if allow and gid not in allow:
            return
        said = (event.get_message_str() or "").strip()
        # 他自己在使唤机器人时不掺和，免得两边打架
        if said.startswith("/") or _at_me(event):
            return
        key = f"{gid}:{sender}"
        now = time.time()
        last = self._last.get(key)
        self._last[key] = now
        self._save()
        if last is None:
            # 没有历史记录：不知道他到底隔了多久，按「刚重逢」处理。
            # 否则新加的人要冒两次泡才有反应，看起来像插件没生效。
            gap = float(self.gap)
        else:
            gap = now - float(last)
            if gap < self.gap:
                return
        name = str(event.get_sender_name() or sender)
        act = self.action
        logger.info(f"[stalk] 触发 {key}，隔了 {gap / 60:.0f} 分钟，动作={act}")
        if act in ("at", "both", "say"):
            line = await self._line(event.unified_msg_origin, name, said, gap)
            if act == "say":
                yield event.plain_result(line)
            else:
                chain = []
                if At is not None:
                    chain.append(At(qq=sender))
                    chain.append(Plain(" "))
                chain.append(Plain(line))
                yield event.chain_result(chain)
        if act in ("poke", "both"):
            done = await self._poke(event, sender, gid)
            if done == 0 and act == "poke":
                # 戳不通至少别让场面冷着
                yield event.plain_result(
                    random.choice(FALLBACK_LINES).replace("{name}", name)
                )
        event.stop_event()  # 话已经说完了，别让主流程再补一遍

    # ------------------------------------------------------------------ 指令

    def _status_lines(self) -> list[str]:
        targets = self.targets
        if not targets:
            return ["还没盯任何人。用 /视奸 加 <QQ号> 加一个。"]
        now = time.time()
        act_text = {
            "poke": f"连戳 {self.poke_times} 下",
            "at": "@ 他说一句",
            "both": f"先说一句再连戳 {self.poke_times} 下",
            "say": "不 @ 只说一句",
        }.get(self.action, self.action)
        lines = [
            f"盯着 {len(targets)} 个人，隔 {int(self.gap // 60)} 分钟没说"
            f"就算「重新出现」，动作：{act_text}"
        ]
        for qq in targets:
            mine = [(k, v) for k, v in self._last.items() if k.endswith(f":{qq}")]
            if not mine:
                lines.append(f"· {qq}：还没记录，他下次冒泡就动手")
                continue
            for k, v in sorted(mine, key=lambda kv: kv[1], reverse=True)[:2]:
                gid = k.split(":", 1)[0]
                quiet = now - float(v)
                due = (
                    "已经够久，下次冒泡就动手"
                    if quiet >= self.gap
                    else f"还要 {_fmt_gap(self.gap - quiet)}"
                )
                lines.append(
                    f"· {qq} @群 {gid}：上次说话 {_fmt_ts(v)}"
                    f"（{_fmt_gap(quiet)}前），{due}"
                )
        return lines

    @filter.command("视奸")
    async def cmd(self, event: AstrMessageEvent, action: str = "", target: str = ""):
        """查看 / 增删盯着的人。"""
        if not self._allowed(event):
            return
        action = (action or "").strip()
        target = (target or "").strip()
        if not action:
            yield event.plain_result(
                f"视奸开关：{'开' if self.enabled else '关'}\n"
                + "\n".join(self._status_lines())
                + "\n\n用法：/视奸 加 <QQ号>　/视奸 减 <QQ号>　/视奸 清"
            )
            return
        if action in ("清", "清空", "全清"):
            self._extra = []
            self._save()
            yield event.plain_result("全清了，现在一个人都不盯。")
            return
        if not target.isdigit():
            yield event.plain_result("得给我一个 QQ 号，比如 /视奸 加 123456789")
            return
        if action in ("加", "盯", "添加"):
            if target in self.targets:
                yield event.plain_result(f"{target} 本来就在盯着。")
                return
            self._extra.append(target)
            self._save()
            yield event.plain_result(
                f"盯上 {target} 了。他下次隔 {int(self.gap // 60)} 分钟"
                f"才冒泡的话我就动手。"
            )
            return
        if action in ("减", "去", "取消", "删"):
            if target in self._extra:
                self._extra.remove(target)
            for k in [k for k in self._last if k.endswith(f":{target}")]:
                self._last.pop(k, None)
            self._save()
            yield event.plain_result(f"不盯 {target} 了。")
            return
        yield event.plain_result(
            "看不懂这个动作，用：/视奸 加 <QQ号>、/视奸 减 <QQ号>、/视奸 清"
        )

    @filter.command("戳一下")
    async def poke_cmd(self, event: AstrMessageEvent, target: str = ""):
        """手动戳一下，验证 send_poke 这条通道通不通。"""
        if not self._allowed(event):
            return
        target = (target or "").strip() or str(event.get_sender_id() or "")
        if not target.isdigit():
            yield event.plain_result("给我一个 QQ 号，比如 /戳一下 123456789")
            return
        gid = str(event.get_group_id() or "")
        yield event.plain_result(f"戳 {target} 试试…")
        done = await self._poke(event, target, gid)
        if done:
            yield event.plain_result(f"戳了 {done} 下，看看他手机有没有抖。")
        else:
            yield event.plain_result(
                "戳不动。多半是 NapCat 没开这个接口，或者当前不是 QQ 平台。"
            )
        event.stop_event()

    async def terminate(self):
        logger.info("[stalk] 已停止")
