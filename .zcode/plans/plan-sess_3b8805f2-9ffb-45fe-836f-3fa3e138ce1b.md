# M-TOOL 工具模式（提醒域）最终执行计划

## 定案记录（2026-10-05 讨论定稿，共九项）

| # | 决策点 | 定案 |
|---|---|---|
| 1 | 任务栏迷你窗（CPU/内存/时间/天气） | **不做**（连带：无天气外发面、platform 零改动、零新依赖） |
| 2 | 播报方式 | 优先 LLM 人格播报+TTS（`llm.provider: api`）；mock/断线→客户端本地固定台词气泡，**永不静默丢失** |
| 3 | 闹钟 | 托盘设置界面：多条、HH:MM、**每日重复或一次性**（"HH:MM"=每日，"YYYY-MM-DD HH:MM"=一次性） |
| 4 | 占线处理 | 服务端排队（cap 2，超限丢最旧），reply 结束后经 `on_reply_done` 钩子自动补播 |
| 5 | DND | 工具事件**全部无视 DND**；总控权由 `tools.enabled` 总闸补位 |
| 6 | 记忆写回 | tool 轮**完全不写回**；AI Native 铁律对工具轮豁免需文档化裁定（CI 只断言 user 轮，不受影响） |
| 7 | 隔离形态 | **域隔离六件套 + 总闸**，不引入模式状态机；未来「专注场景」= 预设翻转既有开关 |
| 8 | 播报上下文 | tool 轮 system prompt **只读 persona+tone_hint**：不检索记忆、不带历史 |
| 9 | 实现路线 | **守卫式**：tool 轮走同一条 reply 漏斗，orchestrator 加 `trigger=="tool"` 守卫（几十行 diff）；否决独立小漏斗（编排双份=维护地雷）。行为层主模式零变化由守卫+总闸+813 测试阵三道防线保证 |

已披露并接受的耦合点：播报占线期间（约 2.5s+TTS）用户打字被丢（与今天回复占线行为一致，Interrupt 可打断播报）。

## 范围（v1）

番茄钟（托盘开始/停止+动态剩余时间）、闹钟（重复/一次性）、倒计时、喝水/久坐提醒（人离开超 `active_idle_s` 不提醒）、整点报时（默认关+免打扰 22:00–8:00）、节日/纪念日（公历+六大农历硬编码 2026–2035 + 自定义 + 配置触发时点）。

## 新增文件

- **`client/toolbox.py`** — 纯逻辑定时器域（照 StateReporter/voice_vad 模式：`clock: Callable[[], float]` 注入，零 Qt）：`PomodoroSession` / `AlarmStore`（去重触发、启动清过期一次性并写回）/ `Countdown`（内存态）/ `PeriodicReminders`（water/sedentary，门=`get_last_input_seconds() < active_idle_s`）/ `HourlyChime`（整点翻转+quiet 时段）/ `FestivalChecker`（查表+自定义解析，每日一次）；统一出口 `event_ready(kind, detail)`，由上层决定 ToolEvent 或本地台词
- **`server/tool_announcer.py`** — `ToolAnnouncer`：kind→触发上下文模板；`submit(kind, detail)` 占线入队（cap 2 丢最旧）/闲时 `orch.handle_user_text(context, trigger="tool", source="tool")`；`drain()` 接 orchestrator `on_reply_done`
- **`client/alarm_dialog.py`** — 闹钟管理对话框（列表增删，挂 self 保活，照 card_import_dialog 教训；写回走 `_rewrite_key_at_path` flow-style 列表）
- **`tests/test_toolbox.py`**、**`tests/test_tool_announcer.py`**

## 修改文件

- **`server/protocol.py`**：`ToolEvent(Msg)`（`kind` Literal 闭集：pomodoro_work_end/pomodoro_break_end/alarm/countdown/water/sedentary/hourly/festival + `detail: str`）+ REGISTRY；`ReplyBegin.trigger` Literal 增 `"tool"`
- **`server/orchestrator.py`**（守卫式核心，全部 `trigger=="tool"` 条件）：①跳过 `_memory_lookup` ②`self._history` 不追加 ③后台槽全跳（`_emotion_merge`/`_apply_relationship`/store 写回/dialogue 落库）④system 组装=persona+tone_hint 两段（memory 段省略）；新增可选 `on_reply_done` 回调（reply_end 后调用）
- **`server/main.py`**：WS 分发加 `isinstance(msg, ToolEvent)` 分支；`create_app` 装配 ToolAnnouncer（读 `settings.get("tools")`）
- **`client/tray.py`**：「工具」动态子菜单（aboutToShow 模式）：总闸 ✓ / 番茄钟 开始·停止（进行中显示剩余分钟）/ 倒计时… / 闹钟管理… / 喝水 ✓ / 久坐 ✓ / 整点报时 ✓ / 节日 ✓
- **`run_client.py`**：`build_components` 装配 Toolbox（cfg + `get_platform()` + `ws.send_text`）；语音路由=`llm.provider=="api"`→ToolEvent 上行，mock/断线→`bubble.show_sentence(本地固定台词表)`；托盘回调注入
- **`client/settings_window.py`**：新增「工具」页签（Param 声明表追加全部 tools 键）
- **`config/settings.example.yaml` + `config/settings.yaml`**：新增 `tools:` 节（全键注释，遵守配置全键注释化定案）：
  ```yaml
  tools:
    enabled: true            # 工具模式总闸（false=全部提醒关闭，行为与现状一致）
    alarms: []               # "HH:MM"=每日 / "YYYY-MM-DD HH:MM"=一次性；托盘对话框维护
    pomodoro: {work_min: 25, break_min: 5}
    water_min: 45            # 0=关
    sedentary_min: 90        # 0=关
    active_idle_s: 300       # 离开输入超此秒数暂停健康提醒
    hourly: {enabled: false, quiet_start: 22, quiet_end: 8}
    festivals: {enabled: true, custom: "", hour: 9}   # custom: "05-20 纪念日, 12-01 生日"
  ```
- **`docs/plan.md`**：§0.4 速查表加「工具模式域隔离」行 + §2 加 M-TOOL 节；**`PROGRESS.md`** 批次记录；**`README.txt`** 功能清单补工具模式一段（无新外发面，透明度声明不动）

## 测试计划（全部 hermetic）

- **test_toolbox**（`_Clock` 注入）：番茄钟相位转移 / 闹钟每日去重与一次性触发 / 倒计时 / 健康提醒 active 门控 / 整点翻转+免打扰 / 节日表抽查（含 2026 春节）/ 总闸短路 / 语音路由判定（api→ToolEvent、mock→本地、断线→本地）
- **test_tool_announcer**（ScriptedLlm + collect_reply）：kind→模板、占线入队 cap 与 reply 后 drain、`ReplyBegin.trigger=="tool"`、session 桶 `"tool"`、**四个跳过断言**（store 零新增/历史零增长/retriever spy 未调/无 RelState 下行）
- test_protocol_roundtrip 增 ToolEvent；全量 `pytest tests/ --tb=no -q` 绿；收发闭环冒烟不受影响

## 验收标准

1. mock 出厂配置：番茄钟到点→本地固定台词气泡（零网络）；配 api 后→人格化台词+TTS；断线自动落回本地台词
2. 占线时闹钟触发→当前回复结束后补播
3. tool 轮后记忆库零新增、关系值不变、下轮历史与 prompt 无工具痕迹（测试断言）
4. DND 开时闹钟照常；`tools.enabled=false` 后行为与现状一致
5. 全量测试绿；plan.md/PROGRESS/README 同步

## 执行顺序与协调

TOOL.1 协议+配置底座 → TOOL.2 orchestrator 守卫语义（含单测）→ TOOL.3 ToolAnnouncer（含单测）→ TOOL.4 Toolbox 纯逻辑（含单测）→ TOOL.5 run_client 接线+托盘 → TOOL.6 闹钟对话框 → TOOL.7 设置窗页签 → TOOL.8 文档收尾+全量回归。每 task 收尾按 §3.2 更新 PROGRESS。
**并行会话协调**：开工前确认无其他会话正在改 `orchestrator.py`/`protocol.py`/`run_client.py` 三个热文件；提交只圈 M-TOOL 文件。