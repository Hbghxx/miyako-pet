"""Toolbox（M-TOOL）—— 工具模式纯逻辑定时器域（零 Qt，时钟注入可测）。

域隔离定案（plan M-TOOL）：
· 与演出循环/音频链/识屏链零共享状态；事件只从 Toolbox.tick() 的返回值
  与 emit 回调离开，上层（run_client）决定 ToolEvent 上行（LLM 人格播报）
  还是本地固定台词气泡（mock/断线降级，文案见 LOCAL_FALLBACK_LINES）；
· 全部无视 DND（履约与提醒不是主动搭话，不占 behavior gate/冷却额度）；
· tools.enabled 总闸=False 时 tick() 短路（行为与旧版一致）。

组件：PomodoroSession（work→break 循环，休息结束自动进入下一段专注）/
AlarmStore（"HH:MM"=每日重复，"YYYY-MM-DD HH:MM"=一次性；错过不补触发，
一次性过期启动清理、触发即失效并写回）/ Countdown（一次性，内存态）/
PeriodicReminders（喝水/久坐：人在座才计时，离开即顺延）/
HourlyChime（整点翻转 + 免打扰时段，免打扰整点同样消耗防复活连发）/
FestivalChecker（公历固定 + 农历六大节日查表 2026~2035 + 自定义纪念日；
表外年份优雅跳过）。

时钟注入：clock 返回 datetime（挂钟语义——闹钟/整点/节日都对墙钟）；
idle_seconds 注入 platform.get_last_input_seconds（健康提醒「人在」判定，
None/负值按在座处理——平台不可用时提醒是好于不提醒的低风险默认）。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 本地固定台词（mock/断线降级；kind 与协议 ToolKind 一一对应，{detail} 同源）
LOCAL_FALLBACK_LINES = {
    "pomodoro_work_end": "番茄钟时间到，休息一下吧～",
    "pomodoro_break_end": "休息结束，要继续专注咯～",
    "alarm": "时间到啦！{detail}。",
    "countdown": "{detail} 分钟倒计时结束啦。",
    "water": "该喝水啦～",
    "sedentary": "坐久啦，起来活动一下吧～",
    "hourly": "现在是{detail}。",
    "festival": "今天是{detail}，节日快乐！",
}

# 农历六大节日查表（公历，2026~2035；2026-10-05 经新华社/香港天文台对照表/
# 多源万年历交叉核对）。表外年份优雅跳过（该年仅公历节日生效）。
# 元宵节 = 春节 + 14 天（同月内天数固定），由春节表推导不单列。
_LUNAR_FESTIVALS: dict[str, list[tuple[int, int, int]]] = {
    "春节": [(2026, 2, 17), (2027, 2, 6), (2028, 1, 26), (2029, 2, 13),
            (2030, 2, 3), (2031, 1, 23), (2032, 2, 11), (2033, 1, 31),
            (2034, 2, 19), (2035, 2, 8)],
    "端午节": [(2026, 6, 19), (2027, 6, 9), (2028, 5, 28), (2029, 6, 16),
              (2030, 6, 5), (2031, 6, 24), (2032, 6, 12), (2033, 6, 2),
              (2034, 6, 20), (2035, 6, 10)],
    "七夕": [(2026, 8, 19), (2027, 8, 8), (2028, 8, 26), (2029, 8, 16),
            (2030, 8, 5), (2031, 8, 24), (2032, 8, 12), (2033, 8, 2),
            (2034, 8, 20), (2035, 8, 10)],
    "中秋节": [(2026, 9, 25), (2027, 9, 15), (2028, 10, 3), (2029, 9, 22),
              (2030, 9, 12), (2031, 10, 1), (2032, 9, 19), (2033, 9, 8),
              (2034, 9, 27), (2035, 9, 16)],
    "重阳节": [(2026, 10, 18), (2027, 10, 8), (2028, 10, 26), (2029, 10, 16),
              (2030, 10, 5), (2031, 10, 23), (2032, 10, 12), (2033, 10, 1),
              (2034, 10, 20), (2035, 10, 9)],
}

# 公历固定节日（月, 日, 名称）
_SOLAR_FESTIVALS: list[tuple[int, int, str]] = [
    (1, 1, "元旦"), (2, 14, "情人节"), (3, 8, "妇女节"),
    (5, 1, "劳动节"), (6, 1, "儿童节"), (10, 1, "国庆节"), (12, 25, "圣诞节"),
]

# 一次性闹钟的迟到宽限窗：超过则静默丢弃（睡了两小时后不再播「该起床啦」）
_ALARM_ONCE_GRACE = timedelta(seconds=120)


def parse_alarm(entry: str) -> Optional[tuple[str, object]]:
    """解析闹钟条目 → ("daily", (h, m)) 或 ("once", datetime)；非法返回 None。"""
    s = str(entry).strip()
    try:
        if re.fullmatch(r"\d{1,2}:\d{2}", s):
            h, m = (int(x) for x in s.split(":"))
            if 0 <= h <= 23 and 0 <= m <= 59:
                return ("daily", (h, m))
            return None
        return ("once", datetime.strptime(s, "%Y-%m-%d %H:%M"))
    except ValueError:
        return None


def alarm_display(entry: str) -> str:
    """闹钟的播报/显示形（取 HH:MM 尾段，两种格式统一）。"""
    return str(entry).strip()[-5:]


class PomodoroSession:
    """番茄钟：work→break 自动循环，直到 stop()。"""

    def __init__(self, work_min: int = 25, break_min: int = 5) -> None:
        self.work_min = max(1, int(work_min))
        self.break_min = max(1, int(break_min))
        self._phase: Optional[str] = None     # None | "work" | "break"
        self._end: Optional[datetime] = None

    @property
    def active(self) -> bool:
        return self._phase is not None

    @property
    def phase(self) -> Optional[str]:
        return self._phase

    def start(self, now: datetime) -> None:
        self._phase = "work"
        self._end = now + timedelta(minutes=self.work_min)

    def stop(self) -> None:
        self._phase = None
        self._end = None

    def remaining_seconds(self, now: datetime) -> int:
        if self._end is None:
            return 0
        return max(0, int((self._end - now).total_seconds()))

    def tick(self, now: datetime) -> Optional[tuple[str, str]]:
        """相位到点 → 产出事件并自动翻相位（挂起恢复后同样只补一声）。"""
        if self._phase is None or self._end is None or now < self._end:
            return None
        if self._phase == "work":
            event = ("pomodoro_work_end", str(self.work_min))
            self._phase = "break"
            self._end = now + timedelta(minutes=self.break_min)
        else:
            event = ("pomodoro_break_end", "")
            self._phase = "work"
            self._end = now + timedelta(minutes=self.work_min)
        return event


class AlarmStore:
    """闹钟集：每日重复按「条目+日期」去重；一次性触发即失效并写回。"""

    def __init__(self, entries, *, on_change: Optional[Callable[[list], None]] = None) -> None:
        self._entries = [str(e) for e in (entries or [])]
        self._on_change = on_change
        self._fired_daily: set[tuple[str, str]] = set()
        self._fired_once: set[str] = set()

    def entries(self) -> list[str]:
        return list(self._entries)

    def add(self, entry: str) -> bool:
        entry = str(entry).strip()
        if not parse_alarm(entry) or entry in self._entries:
            return False
        self._entries.append(entry)
        self._persist()
        return True

    def remove(self, entry: str) -> bool:
        if entry not in self._entries:
            return False
        self._entries.remove(entry)
        self._fired_daily.discard(entry)
        self._fired_once.discard(entry)
        self._persist()
        return True

    def purge_expired(self, now: datetime) -> list[str]:
        """移除已过期一次性闹钟（启动时调用），返回移除列表。"""
        removed = [e for e in self._entries
                   if (p := parse_alarm(e)) and p[0] == "once" and p[1] < now]
        for e in removed:
            self._entries.remove(e)
        if removed:
            self._persist()
        return removed

    def tick(self, now: datetime) -> Optional[tuple[str, str]]:
        for entry in list(self._entries):
            parsed = parse_alarm(entry)
            if parsed is None:
                continue
            kind, val = parsed
            if kind == "daily":
                h, m = val
                day_key = (entry, now.strftime("%Y-%m-%d"))
                if (now.hour, now.minute) == (h, m) and day_key not in self._fired_daily:
                    self._fired_daily.add(day_key)
                    return ("alarm", alarm_display(entry))
            else:
                if entry in self._fired_once:
                    continue
                if val <= now <= val + _ALARM_ONCE_GRACE:
                    self._fired_once.add(entry)
                    self.remove(entry)          # 一次性：触发即失效并写回
                    return ("alarm", alarm_display(entry))
                if now > val + _ALARM_ONCE_GRACE:
                    self._fired_once.add(entry)  # 迟到超宽限：静默丢弃
                    logger.info("一次性闹钟已迟到丢弃：%s", entry)
        return None

    def _persist(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change(self._entries)
            except Exception:
                logger.warning("闹钟列表写回失败（内存态已生效）", exc_info=True)


class Countdown:
    """一次性倒计时（内存态，不跨重启——产品定案）。"""

    def __init__(self) -> None:
        self._end: Optional[datetime] = None
        self._minutes = 0

    @property
    def active(self) -> bool:
        return self._end is not None

    def start(self, minutes: int, now: datetime) -> None:
        self._minutes = max(1, int(minutes))
        self._end = now + timedelta(minutes=self._minutes)

    def stop(self) -> None:
        self._end = None

    def remaining_seconds(self, now: datetime) -> int:
        if self._end is None:
            return 0
        return max(0, int((self._end - now).total_seconds()))

    def tick(self, now: datetime) -> Optional[tuple[str, str]]:
        if self._end is None or now < self._end:
            return None
        self._end = None
        return ("countdown", str(self._minutes))


class PeriodicReminders:
    """喝水/久坐周期提醒：人在座才计时，离开即顺延（回座重新计满整段）。"""

    def __init__(self, water_min: int = 0, sedentary_min: int = 0,
                 active_idle_s: int = 300) -> None:
        self.water_min = max(0, int(water_min))
        self.sedentary_min = max(0, int(sedentary_min))
        self.active_idle_s = max(0, int(active_idle_s))
        self._last_water: Optional[datetime] = None
        self._last_sed: Optional[datetime] = None

    def arm(self, now: datetime) -> None:
        """装配对表：从现在起计满整段间隔才首次提醒。"""
        self._last_water = self._last_water or now
        self._last_sed = self._last_sed or now

    def rearm(self, now: datetime) -> None:
        """重新对表（开关重新打开时：从当下计满整段，避免陈旧时刻秒触发）。"""
        self._last_water = now
        self._last_sed = now

    def tick(self, now: datetime,
             idle_seconds: Optional[float]) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        # None/负值（平台不可用）按在座处理：提醒是好于沉默的低风险默认
        active = (idle_seconds is None or idle_seconds < 0
                  or idle_seconds < self.active_idle_s)
        for name, interval_min, last_attr in (
                ("water", self.water_min, "_last_water"),
                ("sedentary", self.sedentary_min, "_last_sed")):
            if not interval_min:
                continue
            last = getattr(self, last_attr)
            if last is None:
                continue
            if not active:
                setattr(self, last_attr, now)    # 离开即顺延
                continue
            elapsed_s = (now - last).total_seconds()
            if elapsed_s >= interval_min * 60:
                setattr(self, last_attr, now)
                events.append((name, str(int(elapsed_s // 60))))
        return events


class HourlyChime:
    """整点报时：整点翻转触发；免打扰时段静默但同样消耗该整点（防复活连发）。"""

    def __init__(self, enabled: bool = False, quiet_start: int = 22,
                 quiet_end: int = 8) -> None:
        self.enabled = bool(enabled)
        self.quiet_start = int(quiet_start)
        self.quiet_end = int(quiet_end)
        self._last_key: Optional[tuple[str, int]] = None

    def tick(self, now: datetime) -> Optional[tuple[str, str]]:
        if not self.enabled or now.minute != 0:
            return None
        key = (now.strftime("%Y-%m-%d"), now.hour)
        if key == self._last_key:
            return None
        self._last_key = key
        if self._in_quiet(now.hour):
            return None
        return ("hourly", f"{now.hour} 点整")

    def _in_quiet(self, hour: int) -> bool:
        if self.quiet_start == self.quiet_end:
            return False
        if self.quiet_start < self.quiet_end:
            return self.quiet_start <= hour < self.quiet_end
        return hour >= self.quiet_start or hour < self.quiet_end   # 跨午夜区间


class FestivalChecker:
    """节日/纪念日问候：每日一次，到 configured hour 后的首个 tick 触发。"""

    def __init__(self, custom: str = "", hour: int = 9, enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self.hour = max(0, min(23, int(hour)))
        self._custom = self._parse_custom(custom)
        self._done_day: Optional[str] = None

    @staticmethod
    def _parse_custom(custom: str) -> list[tuple[tuple[int, int], str]]:
        """解析 "05-20 纪念日, 12-01 生日"（半/全角逗号容错；非法条目跳过）。"""
        out: list[tuple[tuple[int, int], str]] = []
        for part in re.split(r"[,，、]", str(custom)):
            part = part.strip()
            if not part:
                continue
            pieces = part.split(None, 1)
            m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", pieces[0])
            if not m:
                logger.warning("自定义纪念日格式非法，忽略：%r", part)
                continue
            month, day = int(m.group(1)), int(m.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                name = pieces[1].strip() if len(pieces) > 1 else "纪念日"
                out.append(((month, day), name))
            else:
                logger.warning("自定义纪念日日期非法，忽略：%r", part)
        return out

    def _match(self, now: datetime) -> str:
        names: list[str] = []
        for name, days in _LUNAR_FESTIVALS.items():
            for (y, m, d) in days:
                if y != now.year:
                    continue
                if (m, d) == (now.month, now.day):
                    names.append(name)
                if name == "春节" and (date(y, m, d) + timedelta(days=14)).month == now.month \
                        and (date(y, m, d) + timedelta(days=14)).day == now.day:
                    names.append("元宵节")
        for (m, d, name) in _SOLAR_FESTIVALS:
            if (m, d) == (now.month, now.day):
                names.append(name)
        for ((m, d), name) in self._custom:
            if (m, d) == (now.month, now.day):
                names.append(name)
        return "和".join(dict.fromkeys(names))

    def tick(self, now: datetime) -> Optional[tuple[str, str]]:
        day = now.strftime("%Y-%m-%d")
        if not self.enabled or self._done_day == day or now.hour < self.hour:
            return None
        name = self._match(now)
        if not name:
            return None
        self._done_day = day
        return ("festival", name)


class Toolbox:
    """工具模式门面：总闸 + 每秒 tick 入口 + 托盘操作面。"""

    def __init__(self, *, settings: Optional[dict] = None,
                 clock: Callable[[], datetime] = datetime.now,
                 idle_seconds: Callable[[], Optional[float]] = lambda: -1.0,
                 emit: Optional[Callable[[str, str], None]] = None,
                 on_alarms_change: Optional[Callable[[list], None]] = None) -> None:
        cfg = (settings or {}).get("tools") or {}
        self.enabled = bool(cfg.get("enabled", True))
        pom = cfg.get("pomodoro") or {}
        self.pomodoro = PomodoroSession(pom.get("work_min", 25),
                                        pom.get("break_min", 5))
        self.alarms = AlarmStore(cfg.get("alarms") or [], on_change=on_alarms_change)
        self.countdown = Countdown()
        hourly = cfg.get("hourly") or {}
        self.chime = HourlyChime(bool(hourly.get("enabled", False)),
                                 hourly.get("quiet_start", 22),
                                 hourly.get("quiet_end", 8))
        # 健康提醒的「配置默认间隔」：托盘开关语义 = 恢复默认间隔 / 归 0 关闭
        self.default_water_min = max(0, int(cfg.get("water_min", 45)))
        self.default_sedentary_min = max(0, int(cfg.get("sedentary_min", 90)))
        self.reminders = PeriodicReminders(self.default_water_min,
                                           self.default_sedentary_min,
                                           cfg.get("active_idle_s", 300))
        fest = cfg.get("festivals") or {}
        self.festivals = FestivalChecker(fest.get("custom", ""),
                                         fest.get("hour", 9),
                                         enabled=bool(fest.get("enabled", True)))
        self._clock = clock
        self._idle = idle_seconds
        self._emit = emit or (lambda kind, detail: None)
        self.reminders.arm(clock())
        try:
            self.alarms.purge_expired(clock())   # 过期一次性闹钟启动清理并写回
        except Exception:
            logger.warning("过期一次性闹钟清理失败（不影响启动）", exc_info=True)

    def tick(self) -> list[tuple[str, str]]:
        """每秒调用；返回本轮触发的全部事件，并逐个经 emit 回调发出。"""
        if not self.enabled:
            return []
        now = self._clock()
        events: list[tuple[str, str]] = []
        for ev in (self.alarms.tick(now), self.pomodoro.tick(now),
                   self.countdown.tick(now)):
            if ev:
                events.append(ev)
        events.extend(self.reminders.tick(now, self._safe_idle()))
        for ev in (self.chime.tick(now), self.festivals.tick(now)):
            if ev:
                events.append(ev)
        for kind, detail in events:
            try:
                self._emit(kind, detail)
            except Exception:
                logger.warning("工具事件 emit 回调失败 kind=%s", kind, exc_info=True)
        return events

    # ---- 托盘操作面 ----
    def set_enabled(self, on: bool) -> None:
        """总闸（托盘勾选实时生效；持久化由调用方写 settings）。"""
        self.enabled = bool(on)

    def set_water_enabled(self, on: bool) -> None:
        self.reminders.water_min = self.default_water_min if on else 0
        if on:
            self.reminders.rearm(self._clock())

    def set_sedentary_enabled(self, on: bool) -> None:
        self.reminders.sedentary_min = self.default_sedentary_min if on else 0
        if on:
            self.reminders.rearm(self._clock())

    def set_chime_enabled(self, on: bool) -> None:
        self.chime.enabled = bool(on)

    def set_festivals_enabled(self, on: bool) -> None:
        self.festivals.enabled = bool(on)

    def start_pomodoro(self) -> None:
        self.pomodoro.start(self._clock())

    def stop_pomodoro(self) -> None:
        self.pomodoro.stop()

    def pomodoro_label(self) -> str:
        """托盘动态文案；空闲返回空串（菜单项显示「开始番茄钟」）。"""
        if not self.pomodoro.active:
            return ""
        phase = "专注" if self.pomodoro.phase == "work" else "休息"
        remain_min = max(1, self.pomodoro.remaining_seconds(self._clock()) // 60)
        return f"番茄钟{phase}中（剩 {remain_min} 分钟）— 点击停止"

    def start_countdown(self, minutes: int) -> None:
        self.countdown.start(minutes, self._clock())

    def stop_countdown(self) -> None:
        self.countdown.stop()

    def add_alarm(self, entry: str) -> bool:
        return self.alarms.add(entry)

    def remove_alarm(self, entry: str) -> bool:
        return self.alarms.remove(entry)

    def alarm_entries(self) -> list[str]:
        return self.alarms.entries()

    def _safe_idle(self) -> Optional[float]:
        try:
            return self._idle()
        except Exception:
            return -1.0
