"""立绘三轴查表 + 情绪持久状态机（M1.3 → M-SPRITE 简化，2026-09-12）。

状态机（M-SPRITE 简化版）：
  IDLE（持久心境：显示最后情绪）⇄ EMOTE（仅限 manifest 配了 frames 的真 motion
  演出，300ms 自动回 IDLE）；30min 无交互 → DORMANT（趴桌 sleepy），任何交互恢复。

  历史沿革：旧版有 EMOTE(300ms 表情闪现)/TALKING(口型)/呼吸/眨眼——实测
  ①blink/mouth 无素材从未显示、②breathe 计算后被渲染端丢弃、③IDLE 强制回
  normal 使情绪变成 300ms 闪现（2026-09-12 用户报「闪一帧 happy 又回 neutral」）。
  现改为**持久心境**：情绪一经设置持续有效直到下一句覆盖；表情轴外的
  呼吸/眨眼/口型作为无消费者的死代码整体移除（素材补齐时按 motion 框架回归）。

三轴查表：emotion × outfit × pose，缺项逐级 fallback 至 normal；
manifest 读取三级回退：角色包 manifest → data/fallback/ 内置最小 manifest →
内存兜底定义（9 元情绪各一张单色占位图路径，M1.11 落盘实际 PNG）。

演出渲染在 presenter（M1.4）；本模块只产出 SpriteFrame 描述。
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from server.constants import APP_ROOT, resolve_character_package
from server.emotion_defs import DEFAULT_EMOTION, SENTIMENT_CLOSE_SET

logger = logging.getLogger(__name__)

STATE_IDLE = "idle"
STATE_EMOTE = "emote"          # 仅真 motion（manifest 配 frames）使用
STATE_DORMANT = "dormant"

EMOTE_DURATION_S = 0.3
DORMANT_AFTER_IDLE_S = 30 * 60    # 30min 空闲趴桌

REPO_ROOT = APP_ROOT                                # 冻结（exe）形态=exe 所在目录
CHAR_MANIFEST_PATH = resolve_character_package() / "sprites" / "manifest.json"
FALLBACK_MANIFEST_PATH = REPO_ROOT / "data" / "fallback" / "manifest.json"

AUTO_POSE_THRESHOLD_PX = 150    # 气泡中心偏离立绘中心超过此距离 → 左/右朝向


def resolve_auto_pose(dx: int, threshold: int = AUTO_POSE_THRESHOLD_PX) -> str:
    """朝向跟随（M-SPRITE）：气泡中心相对立绘中心的横向偏移 → 朝向键。

    气泡在右（dx>阈值）→ right（B 右向き）、在左 → left（C 左向き）、
    近距 → stand 正面。纯函数可测。
    """
    if dx > threshold:
        return "right"
    if dx < -threshold:
        return "left"
    return "stand"


@dataclass(frozen=True)
class SpriteFrame:
    """一帧演出描述：presenter 据此取图渲染。"""

    state: str                      # idle / emote / dormant
    emotion: str                    # 当前应显示的情绪（闭集内，持久心境）
    image_key: Optional[str]        # manifest 解析出的图片相对路径；None=无可用图


def builtin_fallback_manifest() -> dict:
    """内存兜底：9 元情绪各一张单色占位图（M1.11 落盘 PNG 后由文件 manifest 覆盖）。"""
    return {
        "version": 0,
        "builtin": True,
        "default_outfit": "default",
        "sprites": {
            emotion: {"default": {"stand": f"sprites/{emotion}.png"}}
            for emotion in SENTIMENT_CLOSE_SET
        },
        "dormant": "sprites/dormant.png",
    }


class SpriteManager:
    """立绘状态机（持久心境）。输入：set_emotion / set_outfit / set_pose /
    trigger_motion / on_activity；输出：tick() 逐帧产出 SpriteFrame。
    时间与随机源均可注入（单测确定性）。"""

    def __init__(
        self,
        *,
        manifest_path: Path | None = None,
        fallback_path: Path | None = None,
        rng: random.Random | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._now = now_fn or time.monotonic
        self._manifest, self._manifest_dir = self._load_manifest(
            manifest_path or CHAR_MANIFEST_PATH,
            fallback_path or FALLBACK_MANIFEST_PATH,
        )
        self._state = STATE_IDLE
        self._emotion = DEFAULT_EMOTION
        self._outfit: Optional[str] = None      # None → manifest.default_outfit
        self._pose = "stand"                    # stand / right / left（M-SPRITE 朝向）
        self._motion: Optional[str] = None      # 当前 motion 名（EMOTE 期间有效）
        self._state_started = self._now()
        self._last_activity = self._now()

    # ---------------- manifest 三级回退 ----------------
    @staticmethod
    def _load_manifest(primary: Path, fallback: Path) -> tuple[dict, Path]:
        """读 manifest；返回 (manifest, image_base_dir)。

        image_base_dir = image_key 相对路径的解析基准目录，随回退层级走：
        角色包 manifest 的 key 相对 <包>/sprites；data/fallback 文件 manifest
        的 key（bare 文件名）相对 data/fallback/sprites；内存兜底定义的 key
        自带 ``sprites/`` 前缀，相对 data/fallback。此前渲染端一律按角色包
        sprites 目录解析 → 走到回退层时全部落空（宠物隐形无提示）。
        """
        for path, label, base in (
                (primary, "角色包", primary.parent),
                (fallback, "data/fallback 内置", fallback.parent / "sprites")):
            try:
                if path.is_file():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and isinstance(data.get("sprites"), dict):
                        return data, base
                    logger.warning("%s manifest 结构非法，尝试下一级", label)
            except Exception:
                logger.warning("%s manifest 读取失败，尝试下一级：%s", label, path,
                               exc_info=True)
        logger.warning("角色包与内置 manifest 均不可用，使用内存兜底定义（9 元单色占位）")
        return builtin_fallback_manifest(), fallback.parent

    @property
    def base_dir(self) -> Path:
        """image_key 相对路径的解析基准目录（渲染端 QPixmap(manager.base_dir / key)）。"""
        return self._manifest_dir

    # ---------------- 三轴查表（逐级 fallback 至 normal） ----------------
    def motion_names(self) -> tuple[str, ...]:
        """manifest.motions 段的可用 motion 名（只读访问器，presenter 的闸门数据源）。"""
        return tuple(sorted((self._manifest.get("motions") or {}).keys()))

    def image_key(self, emotion: str, outfit: str | None = None,
                  pose: str | None = None) -> Optional[str]:
        sprites = self._manifest.get("sprites") or {}
        default_outfit = self._manifest.get("default_outfit", "default")
        outfit = outfit or self._outfit or default_outfit
        pose = pose or self._pose or "stand"
        for e in (emotion, DEFAULT_EMOTION):              # 缺情绪 → normal
            by_outfit = sprites.get(e) or {}
            for o in dict.fromkeys((outfit, default_outfit)):  # 缺服装 → 默认服装
                by_pose = by_outfit.get(o) or {}
                key = by_pose.get(pose) or by_pose.get("stand")  # 缺姿态 → stand
                if key:
                    return key
        return None

    # ---------------- 外部输入 ----------------
    def set_emotion(self, emotion: str) -> None:
        """Sentence.emotion 驱动：**持久心境**——持续有效直到下一句覆盖。

        M-SPRITE 修订：不再进 EMOTE 态（普通情绪无独立演出帧，300ms 闪现
        + IDLE 回退 normal 是「闪一帧就变脸」的根因）。EMOTE 保留给真 motion。
        回复本身即交互：调用即从 DORMANT 唤醒（2026-09-12 回归修复——移出
        EMOTE 后回复路径失去隐式唤醒口，休眠中收到回复立绘永远 sleepy）。
        """
        if emotion not in SENTIMENT_CLOSE_SET:
            logger.warning("未知情绪 %r → normal（闭集见 emotion_defs）", emotion)
            emotion = DEFAULT_EMOTION
        self._emotion = emotion
        self._motion = None
        self.on_activity()

    def set_outfit(self, outfit: str) -> None:
        """服装轴（M-SPRITE 换装）：立即生效于后续帧。"""
        self._outfit = outfit

    def set_pose(self, pose: str) -> None:
        """朝向轴（M-SPRITE 朝向跟随）：stand/right/left，立即生效。"""
        self._pose = pose

    def trigger_motion(self, name: str) -> bool:
        """Sentence.motion 触发 EMOTE（M+2.4）：manifest motions 闸门，未登记忽略。

        M-SPRITE 修订：只有 manifest 配了 ``frames`` 的真 motion 才进 EMOTE
        演出（120ms 轮换）；未配 frames 的登记名接受但无视觉（不闪现）。
        """
        if name not in self.motion_names():
            logger.info("motion %r 不在 manifest motions 内，忽略（可用=%s）",
                        name, list(self.motion_names()))
            return False
        frames = (self._manifest.get("motions") or {}).get(name, {}).get("frames")
        if not frames:
            logger.info("motion %r 未配 frames，无演出（忽略）", name)
            return True
        self._motion = name
        self._enter(STATE_EMOTE)
        return True

    def motion_frames(self) -> list[str]:
        """当前 motion 的帧清单（presenter 渲染用；无 motion 时为空）。"""
        if not self._motion:
            return []
        motions = self._manifest.get("motions") or {}
        frames = (motions.get(self._motion) or {}).get("frames") or []
        return [str(f) for f in frames]

    def on_activity(self) -> None:
        """任何交互（拖拽/输入）刷新空闲计时，从 DORMANT 唤醒。"""
        self._last_activity = self._now()
        if self._state == STATE_DORMANT:
            self._enter(STATE_IDLE)

    # ---------------- 状态机推进 ----------------
    def tick(self, now: float | None = None) -> SpriteFrame:
        now = self._now() if now is None else now

        if self._state != STATE_DORMANT and now - self._last_activity >= DORMANT_AFTER_IDLE_S:
            self._enter(STATE_DORMANT)
        if self._state == STATE_EMOTE and now - self._state_started >= EMOTE_DURATION_S:
            self._motion = None
            self._enter(STATE_IDLE)

        if self._state == STATE_DORMANT:
            return SpriteFrame(state=self._state, emotion="sleepy",
                               image_key=self._manifest.get("dormant"))
        return SpriteFrame(state=self._state, emotion=self._emotion,
                           image_key=self.image_key(self._emotion))

    # ---------------- 内部 ----------------
    def _enter(self, state: str) -> None:
        self._state = state
        self._state_started = self._now()

    @property
    def state(self) -> str:
        return self._state

    @property
    def emotion(self) -> str:
        return self._emotion
