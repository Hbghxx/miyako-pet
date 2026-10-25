# miyako 产品介绍 —— 一个本地优先的桌面 AI 陪伴角色

> **一句话定位**：miyako 是一只跑在 Windows 桌面上的 AI 陪伴角色（桌宠）——完全本地运行、模型层完全可插拔、带持久记忆与关系系统、具备识屏/陪玩/摄像头感知等主动行为能力，且任何组件失败都有优雅的降级路径。
>
> 当前版本 **v0.8.0** · Python 3.10+ · 800+ 自动化测试用例（最近批次 813~826 passed）

---

## 目录

1. [产品理念](#1-产品理念)
2. [总体架构](#2-总体架构)
3. [通信协议层](#3-通信协议层)
4. [服务端模块](#4-服务端模块)
5. [客户端模块](#5-客户端模块)
6. [角色包系统](#6-角色包系统)
7. [Provider 可插拔体系与降级矩阵](#7-provider-可插拔体系与降级矩阵)
8. [Token 计量与成本观测](#8-token-计量与成本观测)
9. [延迟预算](#9-延迟预算)
10. [安全与隐私设计](#10-安全与隐私设计)
11. [工程纪律与质量保障](#11-工程纪律与质量保障)
12. [技术栈清单与产品边界](#12-技术栈清单与产品边界)
13. [发布与运维](#13-发布与运维)

---

## 1. 产品理念

### 1.1 为什么是「本地优先」

陪伴数据是对话史、记忆、关系数值——这是用户最私人的数据。miyako 的全部状态都落在本机 `data/` 目录，程序零遥测（不收集、不上传任何使用/崩溃/统计数据），数据外发面只有三种且全部由用户显式配置（见 §10）。

### 1.2 为什么是「单人可维护」

miyako 的目标形态是**一个维护者能长期养得起的项目**。所有架构决策都向「单人可维护」收敛：

- 进程边界最小化：音频链路与 AI 链路全部驻留 Python 单进程，原始 PCM 不跨进程边界；
- 切换 = 重启式而非热切换（90% 价值、30% 成本；热切换需要拆 orchestrator/TTS 锁/句柄重建，边界情况成本不成比例）；
- 拒绝 Electron/Tauri/Web 栈：原始 PCM 与 AEC 残差必须握在 Python 手里，浏览器音频管线是黑盒。

### 1.3 AI Native 铁律（最高优先级产品约束）

> 每一条 `reply_end` 之后必须有状态被写回（记忆 / 关系数值 / 话题钩子至少其一），且下一轮交互能感知到该状态。

这不是口号而是 CI 断言——`tests/test_native_loop.py` 在冻结时钟下驱动完整循环：`handle_user_text("…想吃橘子")` → 断言 entries + hooks 落库 → `retrieve("我想吃水果")` 必须召回「橘子」→ 下一轮 system prompt 必须含记忆段。**「聊过就忘」在这个产品里是编译期错误级别的缺陷。**

### 1.4 两条体验承诺

- **不打扰用户**：一切主动行为必须过时机判定门（gate），gate 的 LLM 判定失败一律按「不该说话」处理（fail-closed）。「不打扰」是不可逆的体验承诺——宁可沉默，不可误触。
- **降级永远可达**：任何 provider 挂掉都有降级路径，且降级语义事先定义在权威降级矩阵里（§7.2），不是事发时临时拍脑袋。

---

## 2. 总体架构

### 2.1 进程与通信拓扑

```
┌────────────────────── client（PySide6 GUI 进程，run_client.py）──────────────────────┐
│  桌宠窗 pet_window ← presenter(50ms 演出循环) ← sprite_manager（立绘资产）              │
│  气泡 bubble · 输入栏 input_bar（文本 + 🎤）· 托盘 tray · 设置窗 · 历史窗 · 导入对话框   │
│  音频链: audio_capture → AEC(speex/webrtc) → voice_vad/barge_in → audio_playback      │
│  感知链: screen_capture+ocr_filter · camera_capture/camera_watcher · state_reporter   │
│  平台隔离: platform/(base+win32+mock) —— ctypes/pycaw 只准出现在 platform/win32.py    │
└────────────▲──────────────────────────────────────────────┬──────────────────────────┘
             │ WebSocket ws://127.0.0.1:9800/ws              │ 首条 Auth(token) 鉴权
             │ 下行: ReplyBegin/Sentence/AudioBegin/…(seq 全序)│ 5s 未过鉴权 close(4401)
┌────────────┴──────────────────────────────────────────────▼──────────────────────────┐
│                          server（FastAPI 进程，run_server.py）                        │
│  main.py: WS 端点 + /metrics + create_app 装配（工厂读 settings.yaml 实例化全件）        │
│  orchestrator.py: 对话主链（检索→组装→流式→契约解析→合成→状态写回闭环）                   │
│  persona · relationship · behavior(主动搭话 gate) · screen_router(识屏/陪玩)            │
│  vision_observer(摄像头) · contracts/(输出契约) · protocol.py(唯一消息事实源)            │
│  memory/: store(SQLite WAL+写队列) · vector(sqlite-vec 三因子检索) · deduplicator       │
│           · summarizer · admin(历史对话)                                              │
│  providers/: llm_api/mock · asr_whisper/mock · tts_chain(gptsovits/cloud_clone/mock)   │
│              · vlm_openai/mock · voice_postprocess · factory                          │
│  session_stats · tokens · usage_store（Token 计量三层）                                │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 关键架构决策

| 决策 | 内容 | 理由 |
|---|---|---|
| C/S 而非单体 | client/server 分进程，WebSocket 通信（`proto_ver: 1`） | GUI 崩溃不拖垮服务端（crash guard：client 未捕获异常→错误弹窗+crash 日志+退出，服务端端口仍在等重连）；server 可无头运行 |
| 全 mock 可启动 | 出厂配置全 mock（零联网零密钥），UI 手感先行 | 设计原则「UI 与 AI 解耦」：M0~M1 全部用 mock 跑通，模型逐个换真 |
| 模型层完全可插拔 | LLM/ASR/TTS/VLM/AEC 全部 base 类+实现+配置驱动工厂 | provider 一律 lazy import，重依赖放 extras，装不上不影响主功能 |
| 协议先行 | `server/protocol.py` 是全部消息的唯一事实源 | 禁止其它文件硬编码消息类型，CI 有往返容错测试 |
| 单实例 | 命名互斥量 `CreateMutexW("miyako_single_instance")` | netstat 无法区分「miyako 已运行」与「端口被占」——可能误判的检查不如不做；互斥量名应用级不随角色变（单活保证） |
| 角色包单活 | `data/characters/*.char` 多包共存，settings 激活，每角色一库 | 见 §6 |

---

## 3. 通信协议层

`server/protocol.py`（Pydantic 模型）定义全部消息，双向共 19 条具体消息：

| 方向 | 消息 | 用途 |
|---|---|---|
| 上行 | `Auth` | 连接后首条鉴权（token 来自 `data/server.token`） |
| 上行 | `UserText` | 文本输入 / ASR 结果 / 主动搭话内容（trigger 区分） |
| 上行 | `AudioStart/AudioChunk/AudioEnd` | 语音上行（base64 PCM16 分块；Start 时服务端清残留帧缓冲） |
| 上行 | `ScreenOcrLine/ScreenShot` | 识屏链路（OCR 文本行 / 截图） |
| 上行 | `CameraFrame` | 摄像头帧（→ vision_observer，**永不落盘**） |
| 上行 | `Interrupt` | 用户打断（服务端 cancel 当轮合成与生成） |
| 上行 | `State` | 客户端状态上报（前台窗口/锁屏/DND；变更触发+5s 节流+首次必发） |
| 上行 | `Ping` | 保活 |
| 下行 | `ReplyBegin/ReplyEnd` | 一轮回复的边界（轮 = Token 计量的漏斗口径） |
| 下行 | `Sentence` | 一句话（text_zh 气泡显示源 / text_ja TTS 合成源 / 情绪 / tts_ok） |
| 下行 | `AudioBegin/AudioChunk` | 下行音频（**单发送队列按 seq 全序**；AudioBegin 如实上报采样率） |
| 下行 | `RelState` | 关系数值（affection/trust） |
| 下行 | `Error` | 降级提示（scope: llm/tts/asr/…） |
| 下行 | `Pong` | 心跳应答 |

**断线语义**：陪伴场景下静默丢弃优于错乱重放——断线时客户端只缓冲上行（重连补发），下行不补发，协议不做 seq 级 ACK。连续 3 次连接被拒且距进程启动超过 30s（豁免窗内只退避不诊断）→ 气泡提示安全模式排障并停止重连。

---

## 4. 服务端模块

### 4.1 `orchestrator.py` —— 对话主链

一轮回复的完整流水线：

```
submit_user_text / submit_behavior(主动搭话)
  │  1. _memory_lookup        检索记忆（asyncio.wait_for 200ms 超时→空 context，无感知）
  │  2. _compose_system       Section 冻结树组装 persona+memory 段
  │     · 上下文容器 8192 token（life 全量卡 persona 段实测 ~2463 tok，4096 会挤掉记忆段）
  │     · 超预算按 priority 从低「砍整段」（绝不字符串拦腰截断）
  │     · 用户文本节点 user_section() 类型层防注入（闭包冻结原文，永不参与 format 展开）
  │  3. LLM 流式（chat 槽，system= 逐轮覆盖缝）
  │  4. 行式契约解析 [e:情绪]中文|日本語（contracts/dialog_v1）
  │  5. _emit_sentence → Sentence 下行（text_zh 气泡 / text_ja 送 TTS）
  │  6. _synthesize → TTS → 单发送队列按 seq 下行 AudioBegin/Chunk
  │  7. ReplyEnd ── 之后是后台槽（不占对话延迟）：
  │  8. _emotion_merge       emotion 槽评估 {affection,trust,importance,open_question}
  │     · 解析失败先「修复重试」（原输出 append 回去+升级措辞+上限 2 次+每轮查取消）再静默缺省
  │     · 前台对话链保持 fail-open 不重试（延迟红线）——重试只给后台槽
  │  9. _apply_relationship   评估增量→dimensions 表钳制更新→RelState 下行
  │ 10. 写队列落库            fact(用户文本,importance) + emotion_tone + open_question→hooks
  ▼  AI Native 铁律闭环：下一轮 step 1 能检索到本轮状态
```

历史管理由 `HistoryCompressor` 负责（阈值触发 → 压到目标占比 → **保最近 N 条是硬承诺** → 摘要自身限预算 → 历史留「历史对话总结」标记行）；阈值/目标占比经 clamp 强制不变式（目标 ≥ 阈值直接拒绝）。

### 4.2 `contracts/` —— 输出契约

LLM 输出格式是行式 `[e:情绪]中文|日本語`，由 dialog_v1 契约解析，**失败不抛异常**：

- 缺 `|` → 整行当中文，`tts_ok=False`；
- 未知表情 → 归 `normal`；
- **多段行假名判别**（真机修复）：3 段及以上 `|` 分隔的行按假名归侧（含假名=日文/否则=中文，各按原顺序 `\n` 连接）——旧逻辑「第一个 | 右侧全归日文」会把第 2+ 句中文并进 TTS 导致「语音比气泡多几句」；
- 脏行进 orchestrator 行缓冲续走，脏行率在 `/metrics` 可见。

契约是单实现硬绑定，不做多契约抽象——收益是可测试性与 prompt 干净度。

### 4.3 `persona.py` + `relationship.py` —— 人格与关系

**Persona**（Section 树 persona 段）：消费角色包 `character_card.yaml`，兼容 miyako 原生卡与 life 卡字段——life 卡 `behavior_rules`（if-then「若…，…。」）与 `anti_patterns`（【角色反模式】独立段，不并入 taboos）按 `CONSUMED_CARD_FIELDS` 契约清单消费；`honesty_boundary` 有意不渲染（产品裁定）；旧包缺字段 fail-open 跳过。关系阶段的语气提示（tone_hint）作为 Section 注入。

**Relationship**：dimensions 表存 affection/trust，`relationship.yaml` 四分支（0-20/20-50/50-80/80+）按 affection 首中出 tone_hint；dimensions 的 initial/min/max/per_reply_delta 全消费（per-dimension 钳制，缺失回退 ±3 增量 / 0~100 区间，initial 钳进区间）；评估失败静默跳过数值不变。

### 4.4 `behavior.py` —— 主动搭话与 gate

五类信号源：时段问候（每日 1 次）/ 记忆钩子（8h，数据源 = hooks 表 open_question）/ 手动截屏（30s 冷却备槽）/ 闲置嗅探（默认 4h，`idle_sniff.hours` 真实接线，非法回落 4h、地板 60s）/ 未回扣话题（2h，追问一次即消费）。

**统一 gate 顺序：DND → 全屏白名单 → 冷却 → 信号强度 → LLM 时机判定**。任何异常、无 state 上报、LLM 失败一律不搭话（fail-closed，三重执法用例）。信号强度消费 `[reflect]` 反思条目（近 7 天条数×0.1 衰减，钳 0.2 下限——反思越多越收敛，防话痨）；未回扣话题的「回扣检测」复用记忆检索的 keyword_sim（bigram 命中率 ≥0.25 即视为用户已回应）。

### 4.5 `screen_router.py` —— 识屏双路与陪玩模式

**双路路由**（成本意识）：

- OCR 命中且文本充分 → **纯文本 LLM，VLM 一次都不调**（近零成本）；
- OCR 命中但需要画面语境 → 截图走 VLM；
- OCR 缺失/崩溃 → 禁用闲置嗅探，手动热键直接走 VLM 路；
- VLM 超时 >2s / 失败 → 跳过屏幕上下文继续主对话。

**陪玩模式（M-PLAY）**：名单格式 `进程名:类型`，三类型三套语义：

| 类型 | 触发语义 |
|---|---|
| `game` | 场景切换触发（画面 hash 大变化 + 稳定 ≥2s 滤转场）；音频无关——开黑语音不影响 |
| `video` | 音频静默 ≥`video_silence_s`（8s）才说话——只在暂停/片尾窗口开口，防剧透 |
| `web` | 按该进程有无声音自动切双语义：有声=看番（静默才说）/ 无声=看网页（翻页可说） |

判据工程细节：触发条件 = 前台窗口**覆盖显示器或工作区**（最大化亦计）；系统音量峰值用 pycaw **仅枚举渲染会话**（麦克风不进判别，不会把用户说话当视频声音）；陪玩域接管期间挂起闲置嗅探；独立冷却（3min）+ 会话上限（12 次）+ 会话间隔重置（10min）；VLM 一次调用两用（输出 SKIP=静默）；gate 拒绝不吞搭话额度（合规=事件触发，呼应原则 7「禁止 AI 调用轮询」——本地信号巡检零成本，云端上传必须事件触发）。

### 4.6 `vision_observer.py` —— 摄像头感知（Tier C，默认关）

隐私硬约束先于功能：首开显式同意框（consent 存档免重复弹）；帧与观察**内存态、永不落盘**（CI 有零文件写执法用例）；DND 时客户端 `camera_watcher` **源头停发**（不是服务端丢弃）；采样 interval 钳制 ≥15s。服务端 mini-gate：冷却（10min）/ 每日上限（5 次）/ reply 空闲 + inflight 防重入；VLM 输出 SKIP=静默，VLM 失败=静默。观察结果可作为主动搭话指令生成素材（走 behavior 的 gate）。

### 4.7 `memory/` —— 记忆子系统

**`store.py` MemoryStore（SQLite WAL）**：

- 单表 `entries`：四类条目 `dialogue / fact / diary / emotion_tone`，`queue_id UNIQUE` 作幂等键；
- `hooks` 表（open_question 落点）、`dimensions` 表（relationship 消费）随库建；
- **持久化写队列**：库旁 `{db}.pending.json`，入队=原子落盘（tmp+`os.replace`），flush 逐条 `INSERT OR IGNORE` 提交成功才出队，Store 初始化重放——**进程崩溃不丢 AI Native 状态写回**（有崩溃恢复与重复重放幂等单测）；
- 线程安全：单连接 `check_same_thread=False` + 一把锁串行化；
- **每角色一库** `data/memory/{package_id}.db`，角色隔离只靠 db 文件粒度——`character_id` 列仅作 forensic 期权标记（建表带列、无索引、写入恒 NULL、接口无参、源码扫描无过滤，五重执法）：查询零改动、备份/重置天然按角色；
- 旧单库 `memory.db` 首启自动迁移（`migrate_legacy_memory_db`，含 WAL 未 checkpoint 边车，幂等）。

**`vector.py` MemoryRetriever（sqlite-vec vec0 虚拟表，cosine）**：

- 三因子评分：`0.55*sim + 0.25*exp(-hours/(72*(0.5+importance))) + 0.20*importance`（半衰按 importance 加权，36h~108h）；粗召回 4×top_k → 精排截 top-k；
- 嵌入 = 本地确定性字符 bigram hash（纯 stdlib 零模型零下载），`embed_fn` 注入缝——未来换真模型只改一处；
- 检索超时 200ms → 空 context 无感知；sqlite-vec 装载失败 → 纯关键词降级（query bigram 命中率作 sim 代理），两模式同签名；
- **检索池白名单**：`RETRIEVAL_KINDS=(fact/diary/emotion_tone)`——dialogue 原文按组落库（单条=「[用户] …\n[miyako] …」，写队列+滚动裁剪 500 组）**只回放不召回**（托盘「查看历史对话」走 MemoryAdmin.dialogue_rounds），防止原文稀释语义检索。

另有 `deduplicator`（矛盾记忆清洗，后台槽，LLM 异常静默）与 `summarizer`（日记生成，≤200 字，`[reflect]` 前缀条数钳 2；**当前生产零接线——仅 [reflect] 手动调档触发，属已知待办**）。

### 4.8 `session_stats.py` + `tokens.py` + `usage_store.py` —— Token 计量三层

1. **唯一估算实现** `estimate_tokens()`：CJK 1 token/字、其余 len//4，加**消息信封常数**与**多模态占位清洗**（图片换占位符再计，防撑爆计数），保守回落偏大不偏小；与上下文预算同源，全项目禁止第二份实现；
2. **真实 usage 优先**：流式请求带 `stream_options:{"include_usage":true}`，厂商不支持自动回落估算并标 `estimated:true`——**禁止计量静默归零**；
3. **三层口径**：跨重启累计（`usage_store`，data/memory/{角色}.usage.json）→ 会话级（`session_stats` 内存态，重启清零=会话边界；轮 = ReplyBegin→ReplyEnd 漏斗，含总/轮次/单轮最高最低；来源分桶 user/behavior/screen/companion；VLM 走 `record_usage("vision")` + `record_extra` 进 `vlm_tokens` 桶——**不进轮次 max/min**，轮=reply 漏斗口径不变，陪玩桶=轮次+画面理解=真实总账）→ /metrics 暴露 `tokens_lifetime` 与 `tokens_session`。

---

## 5. 客户端模块

### 5.1 立绘系统：`pet_window.py` + `presenter.py` + `sprite_manager.py`

- **渲染循环**：`run_client` 50ms 演出循环 tick → SpriteFrame → 按 key 缓存 QPixmap → 窗口 set_sprite——表情切图、眨眼、说话口型从此真实可见（此前的状态机没有渲染方，表情不切换的根因）；
- **持久心境**：情绪是持久状态而非瞬态——IDLE 不回退 normal（瞬态演出=频闪根因）；
- **EMOTE 限真 motion**：motion 只在 manifest 声明了真实 frames 时播放（300ms EMOTE + 120ms 帧轮换），退出清 motion；化妆类 breathe/blink/mouth 等无素材死代码已删除；
- **多轴利用**：5 套服装（casual/parka/uniform/waitress/naked）× 9 情绪；朝向 `auto` 跟随气泡位置（阈值 150px）或手动锁定；
- **降级**：sprites manifest 全缺 → 回退内置最小 manifest（`data/fallback/`）占位图；
- **import 约束 B**：`presenter.py` 与 `sprite_manager.py` 之外禁止 import sprite_manager（AST 扫描执法）。

### 5.2 音频链

**上行采集** `audio_capture.py`：sounddevice 回调模式（16kHz、blocksize=512≈32ms、PCM16），PyAudio 兜底；麦克风开关状态持久化（`voice.autostart_mic` 行级写回 settings）。

**回声消除** `aec_speex.py` / `aec_webrtc.py`：Windows 缺省 = `pyaec`（Rust 版 Speex AEC，官方 win_amd64 轮子，合成信号实测 **33.9dB** 消回声）；webrtc-audio-processing 在 Windows 无轮子（PyPI 实锤全版本仅 sdist+Linux ARMv7 轮子、2020 停更），保留为 Linux/macOS 选项，extras 按 `sys_platform` 标记。工程要点：far/near 内部缓冲 **10ms 帧对齐**（frame=采样率/100）；`threading.Lock` 包住 APM 入口与缓冲（播放线程 far/采集线程 mic 并发，并发死锁测试执法）；`process_mic` 前置 `set_stream_delay_ms`（playback 的 stream_delay 信号接线更新）。

**打断** `barge_in.py` 双档：

- AEC 就绪：残差 >0.03 持续 400ms 触发；播放期**不丢帧**（残差检测替代半双工硬门）；
- 无 AEC：非播放期能量 >0.06 持续 600ms；播放期丢帧（半双工防自听回环）；
- 实机阈值 = max(表值, 2×rms_floor)——3s 环境噪声基线标定（50 样本或 3s 先到即关，浮点累加防卡死），无样本退表值；触发后回落再武装（防连发）；
- **SystemAudioDefense**：播放期查询系统混音峰值（pycaw，兼容新版 GetSpeakers 的 AudioDevice 包装取 `._dev`）减自身贡献仍显著 = 别人在放音频 → 播放期全丢弃；peak=-1（不可用）同样静音门（保守强制）；粘滞解除（外部峰值回落过阈值半）；
- 触发即 **Interrupt + playback.flush()**（乐观打断 v1.8）+ VAD/预滚清理。

**断句** `voice_vad.py`：能量断句状态机（threshold 0.03 / 尾点 700ms **自适应**——切早 +150ms/次上浮、安静 1.5s 恢复窗 -100ms/次回落、上限 1500ms / 最短 250ms / 最长 15s 强制截断；时间注入保证测试确定性）。VAD 选型实测三方案 CPU 均 <0.5%，Silero-ONNX 主选/能量兜底的双层方案已规划。

**下行播放** `audio_playback.py`：服务端**单发送队列按 seq 全序**，客户端到达即播；跨 reply 采样率变化重建播放流；interrupt = 本地 flush + 服务端 cancel。

### 5.3 感知链

- `screen_capture.py` + `ocr_filter.py`：截屏与 OCR 过滤（OCR 可选组件，缺失按降级矩阵走）；
- `camera_capture.py`：QtMultimedia 抓帧（**零新增依赖**），按需 `max_side` 钳制 JPEG；
- `camera_watcher.py`：interval ≥15s 钳制，DND/断连**源头停发**；
- `state_reporter.py`：周期采集前台窗口/锁屏/DND 上报（**变更触发 + 最小 5s 节流**纯函数 `should_send_state`，首次必发，采集失败按 null 上报——null 是服务端 gate fail-closed 的判据）。

### 5.4 UI 组件

- `bubble.py`：打字机气泡（同屏句数 1~6、缩放/字体/颜色/透明度全套实时可调）；
- `input_bar.py`：文本输入 + 🎤 麦克风按钮（开关状态行级写回 `voice.autostart_mic`，启动自动恢复）；
- `tray.py`：完整托盘菜单（UI 显隐/静音/DND/设置/历史/检查更新/TTS 状态指示/两个导入入口/切换角色/立绘服装朝向/两档退出）；降级发生时 TTS 状态项变为「TTS 降级：scope → fallback」；
- `settings_window.py`：五页签（参数调整/Providers 探活/关系/记忆/Token 监控），隐私组（摄像头感知、麦克风自启）在此，QCheckBox live 信号接线；
- `history_window.py`：只读历史对话窗口（对话框轮次渲染）；
- `switcher.py`：角色切换编排（见 §6）；
- `updater.py`：检查更新（默认 URL 空=完全不工作）。

### 5.5 `platform/` —— 系统调用隔离

`base.py`（PlatformProvider 接口）+ `win32.py`（ctypes/pycaw 唯一合法居所）+ `mock.py`（无窗口单测注入点）。**import 约束 A**：`client/` 下其余文件禁止 import ctypes/win32api/pycaw（AST 扫描执法）。平台抽象是测试接缝，不是跨平台承诺。

### 5.6 酒馆卡导入：`tavern_card.py` + `card_importer.py` + `tavern_import_dialog.py`

SillyTavern 卡（chara_card_v1/v2/v3，`.json` 或 `.png` 内嵌 chara/ccv3）→ **独立模式**转包导入：原卡链路零改动（仅复用 card_importer 的 scan/import/wire）；**音色恒借激活包参考池**（reference_donor → GptsovitsTts 候选池兜底接管，出声即当前角色音色，换卡不掉声音）；无配套权重自动接线 `data/tts/` 默认权重（条件缺失跳过、失败保卡）；宏替换 `{{user}}/{{char}}`；卡面图九情绪统一映射（无图落占位）；world book 不导入。

---

## 6. 角色包系统

### 6.1 包结构

```
data/characters/{package_id}.char/
├── manifest.yaml          # schema_version + 资产索引（表情/服装/朝向轴）
├── character_card.yaml    # 人设卡（persona.py 消费；CONSUMED_CARD_FIELDS 为契约清单）
├── relationship.yaml      # 关系维度与阶段分支（relationship.py 消费）
├── tts_config.yaml        # 音色配置（voice_postprocess + gptsovits 参考池）
├── sprites/manifest.json  # 立绘清单（9 情绪 × 5 服装 × 朝向）
├── reference_audio/       # TTS 参考音频
└── skill/                 # 角色技能资产
```

### 6.2 单活激活与安全语义

- 激活 = settings.yaml `character.package_id`（缺省 miyako），经 `resolve_character_package()`（`server/constants.py`）解析，含**路径穿越字符防护**；
- 启动时包缺失/manifest 非法 → 回退内置 miyako.char + 启动横幅 ⚠️ + 托盘提示；
- 切换 = 托盘重启式：目标包校验失败**拒绝切换**（不写 settings）；reply 进行中 → interrupt 后等后台评估落库（≤3s 超时强制）再重启，气泡「稍等，把这句记完…」；
- **每角色一库**：记忆/usage 全部随 `package_id` 派生路径，两层隔离断言（文件粒度 + SQL 无过滤）；
- 产品级标识（日志目录/互斥量/产品名）不随角色变。

---

## 7. Provider 可插拔体系与降级矩阵

### 7.1 工厂与实现

`providers/factory.py` 读 settings.yaml 实例化，全部 lazy import：

| 层 | 实现 | 要点 |
|---|---|---|
| LLM | `llm_api`（OpenAI 兼容端点，任意厂商或本地 Ollama）/ `llm_mock` | 四槽位路由（chat/emotion/correction/diary，`roles: inherit` 跟随默认或点名大杯）；`llm.api.extra` 浅合并进请求体（extra 键覆盖同名基础键，槽位可携带 extra 差异化）；**chat 槽默认禁 thinking**（实测对话首字 8.8s→0.8s，质量 A/B 无实质差异；后台槽不赶时间可单独开启） |
| ASR | `asr_whisper`（faster-whisper 本地 CPU int8）/ `asr_mock` | **幻听防线**：Silero VAD 预过滤 + `condition_on_previous_text=False` + 段级质量闸（`no_speech_prob>0.6` / `avg_logprob<-1.0` 可配置）+ 幻觉短语黑名单；空转写**静默丢弃**（零下行，区别于「没听清」降级行） |
| TTS | `tts_chain` 编排 `gptsovits`（自训音色，9880 探活，api_v2）/ `cloud_clone`（OpenAI 兼容 `/audio/speech`，wav 流式剥头，采样率按 fmt 段如实上报）/ `mock` | 音色级失败 reply 级锁定；**链尾 mock 构建为 silent=纯气泡**（空流零音频块）——「听错误音色不如纯文本」；仅 `providers.tts: mock` 显式出厂配置保持有声（CI 口型依赖） |
| VLM | `vlm_openai`（OpenAI 兼容多模态，vision 节回落 llm.api，key 只从环境变量）/ `vlm_mock` | 摄像头与识屏共用 |
| 后处理 | `voice_postprocess` | 双语机械修正（替换/守卫/种子确定性）；失败/配置缺失 **fail-open 原文直通**，不阻断流水线，托盘 ⚠️ 一次防刷屏 |

**GPT-SoVITS 细节**：启动硬化（bat `/D` 钉 cwd——GPT_SoVITS 按 getcwd() 找依赖，错 cwd 静默崩溃是「机械音降级」真因；控制台重定向 `logs/tts_console.log`）；tts_gptsovits 段缺参考配置时 `candidates.yaml` rank#1 自动补位（**同源配对**：显式 ref 只配同名转写，宁缺毋错）。

### 7.2 降级矩阵（节选权威清单）

| 失败点 | 行为 |
|---|---|
| LLM API 失败 | `tone_examples` 随机一句 + `Error{scope:"llm"}` 气泡兜底台词 |
| TTS 音色级探活失败 | reply 级锁定至 silent mock → 本轮纯气泡，托盘提示 |
| TTS 中途失效 | 当场降级：剩余句纯气泡 + Error 提示（句间不切换音色——**音色撕裂比音质下降更破坏人格一致性**） |
| voice_postprocess 失败 | 原文直通（fail-open），指纹兜底失效可接受 |
| ASR 失败 | Error{scope:"asr"}，「没听清」 |
| AEC 缺失/初始化失败 | 直通 + 播放期静音门 + barge_in 强制 false |
| 系统他方音频 | 播放期全丢弃退静音门（含峰值不可用 -1 的保守情形） |
| OCR 缺失 | 禁闲置嗅探；手动热键直走 VLM |
| VLM 超时/失败 | 跳过屏幕上下文继续对话 |
| 记忆检索超时 200ms | 空 context 无感知 |
| sqlite-vec 装载失败 | 纯关键词降级（同签名） |
| sprites manifest 全缺 | 内置 fallback manifest 占位图 |
| 关系评估失败 | 静默跳过数值不变 |
| gate LLM 失败/无 state | **不搭话（fail-closed）** |
| WS 连接被拒 | 3 次且 >30s 豁免窗 → 气泡提示安全模式 → 停止重连 |
| WS 断线 | 上行缓冲补发、下行静默丢弃、指数退避、托盘灰图标 |
| extras 安装失败 | 不阻断安装，preflight 如实 ⚠️，按矩阵降级 |

每行都有对应单测（矩阵里的「单测」列与 `tests/` 附录清单一一对应）。

---

## 8. Token 计量与成本观测

见 §4.8 三层口径。补充成本面：

- **cached_tokens 归一采集**：智谱 `prompt_tokens_details` / DeepSeek `prompt_cache_hit_tokens` 各自归一；估算路径不造假（标 estimated）；
- **缓存差价**：`price_cny_per_1m` + `price_cny_per_1m_cached`（厂商缓存命中约 5 折）→ /metrics 与设置窗 Token 页显示金额；未配置显示「未配置」；
- **VLM 独立计量**：`record_usage("vision",…)`（真实 usage 优先，否则文本估算），陪玩模式的画面理解进 companion 桶，账目可分摊到「聊天/搭话/识屏/陪玩」四个来源；
- 上下文缓存摊薄增量：容器 8192 的 token 成本由 prompt cache（命中约 5 折）摊薄。

---

## 9. 延迟预算

| 环节 | SLA |
|---|---|
| ASR 尾点 | ≤800ms |
| 记忆检索 | ≤200ms（超时空返回，无感知） |
| LLM 首句 | ≤1200ms |
| voice_postprocess | ≤5ms |
| TTS 首块 | ≤500ms |
| **合计** | **≤2.5s** |

`/metrics` 暴露 p50/p95（`test_latency_budget` 守护埋点）。已落地的延迟工程：chat 槽禁 thinking（8.8s→0.8s）、whisper 模型基准（base 比 small 快 ~3.5× 且中文准确率持平，`tools/bench_whisper.py` 可复测）、上下文缓存验证（热轮省 ~500ms）。

---

## 10. 安全与隐私设计

| 面 | 设计 |
|---|---|
| 网络边界 | server **仅监听 127.0.0.1**（读到其它值强制覆盖 + warning） |
| 鉴权 | 首条 `Auth(token)`，token 文件 `data/server.token`，5s 未过 close(4401)，未鉴权连接**零业务消息**（攻击者/误连进程静默失败） |
| 密钥 | BYOK：只放 `.env`（不进 git、不进发布包），settings.yaml 禁止写密钥；VLM key 只从环境变量读 |
| 遥测 | **零遥测**：不收集/不上传使用、崩溃、统计数据 |
| 外发面 | 仅三类且全部用户显式开启：自配 AI 服务（对话/画面/合成文本）、手动检查更新（一次无数据 GET）、云端克隆 TTS（合成文本）。本地路径永不外发 |
| 摄像头 | 默认关 + 首开同意框 + 帧与观察永不落盘（CI 零文件写执法）+ DND 源头停发 |
| 进程单实例 | 命名互斥量（应用级，不随角色变） |
| 崩溃防护 | client 未捕获异常 → 错误弹窗 + crash 日志落盘（滚 3 份）+ 退出；server 端口保留等重连 |
| 配置安全 | settings 读取单一入口 fail-open（缺失/非法回内置默认）；server 节 host 写别值强制覆盖 |

---

## 11. 工程纪律与质量保障

### 11.1 十二条设计原则（违反 = 返工）

模型层完全可插拔 / 协议先行 / 每层有 fallback / UI 与 AI 解耦 / 状态沉淀闭环 / 不打扰用户 / 禁止 AI 调用轮询（云端上传必须事件触发+过 gate）/ 分发友好 / 进程边界最小化 / 角色包单活+禁止投机通用化 / 单一职责验收 / 系统调用隔离。

### 11.2 CI 执法的硬约束

| 约束 | 执法方式 |
|---|---|
| AI Native 铁律 | `test_native_loop.py`：冻结时钟下完整循环，写回与召回断言 |
| import 约束 A（platform 边界） | `test_platform_provider.py` AST 扫描 |
| import 约束 B（presenter 边界） | `test_stage_presenter.py` AST 扫描 |
| settings 单一入口 | `test_settings_single_entry.py` 源码扫描（safe_load 与 settings.yaml 引用仅豁免 `load_settings`） |
| character_id 期权列 | 建表/无索引/恒 NULL/无参接口/源码扫描五重断言 |
| 摄像头零落盘 | 零文件写执法用例 |
| 帧对齐/并发安全 | AEC 10ms 对齐、APM 并发锁死锁测试、下行 seq 全序测试 |
| 记忆崩溃恢复 | 入队不 flush → 新实例补写；重复重放幂等 |

### 11.3 SQL 与测试纪律

- **SQL 全部 execute 调用点内联静态字面量 + 参数绑定**——模块级 SQL 常量、f-string 动态 IN、跨行隐式拼接一律禁止（安全扫描三轮拦截驱动）；动态过滤改 Python 侧完成（单用户数据量小，`_READ_CAP=2000`）；PRAGMA 用表值函数绑定；
- 测试全部 hermetic（MockLLM/Mock 注入缝），跨会话并行开发只圈自己的文件；固定时钟（freezegun）保证衰减/迁移类断言确定性。

### 11.4 配置工程

- 应用层配置合一：`settings.yaml` 单文件分节（ui_theme/screen_triggers 并入），`.env` 只放机密——用户可编辑面 = 1 配置文件 + 1 机密文件；
- settings 读取唯一入口 `server/constants.py load_settings()`（fail-open + 注入缝）；行级写入器（保注释写回）不属此列——托盘/设置窗改键时**逐行替换保注释**，用户手写注释不丢；
- 角色包是资源数据包不属配置层。

---

## 12. 技术栈清单与产品边界

### 12.1 技术栈

| 层 | 选型 |
|---|---|
| 语言/UI | Python 3.10+ / PySide6 |
| 服务 | FastAPI + uvicorn（WebSocket）/ Pydantic |
| 记忆 | SQLite（WAL）+ sqlite-vec（vec0 虚拟表） |
| 语音 | sounddevice（PyAudio 兜底）/ faster-whisper（CPU int8）/ pyaec（Rust Speex AEC）/ webrtc-audio-processing（非 Windows）/ pycaw / miniaudio |
| 摄像头 | QtMultimedia（零新增依赖） |
| LLM/VLM | OpenAI 兼容端点（任意厂商 / Ollama 本地） |
| 本地 TTS | GPT-SoVITS api_v2（旁侧服务，9880） |
| 打包分发 | bat 薄壳（不用 PyInstaller——bat 分发路线经用户数据验证） |

### 12.2 否决清单（永久边界，除非触发条件成立）

Electron/Tauri/Web 栈（无翻案条件）/ Live2D（用户明确要求 Live2D 级表现力时经 StagePresenter 接口重评）/ C# WPF / Flutter/Unity/Godot / PyInstaller / **多宠同屏并存、同窗群聊、运行时热切换、character_id SQL 过滤**（单活插拔形态的永久边界）/ dialog_json_v1 弱模型契约 / 识屏规则自动调优（规则 >50 且有误判数据时重评）/ 重型向量库、paddleocr、ffmpeg 依赖。

---

## 13. 发布与运维

- **安装**：`install.bat` 薄壳（找 Python → .venv → 依赖三源自动切换 → `setup_env.py` 幂等初始化 → preflight 约 15 项体检）；`check_req_hash.py` 依赖清单哈希校验（清单变更提示重装）；
- **启动**：`启动miyako.bat`（依赖校验 → 拉起 TTS 旁侧服务 → `pythonw start ""` 无窗口化 server——黑窗口被误关=server 无声死亡的根治；uvicorn `log_config=None` 兼容 pythonw 无 stdout）；`启动miyako(安全模式).bat` 前台 debug 控制台；
- **日志**：`server/logsetup.py` 统一装配——`logs/server.log`、`client.log` 各 RotatingFileHandler（5MB×3，INFO，utf-8）；root 恒 INFO 防三方刷屏，应用包级别随 `--log-level`/`MIYAKO_LOG_LEVEL` 放开；幂等重挂 + fail-open；
- **发布检查**：`RELEASE_CHECKLIST.md` + `tests/e2e/test_release.py` 发布三用例。

---

*本文对应仓库 v0.8.0。各配置键的完整注释见 `config/settings.example.yaml`；安装与日常操作见《miyako使用手册.md》。*
