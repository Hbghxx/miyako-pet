# miyako —— 本地优先的桌面 AI 陪伴角色

基于 **Python 3.10+ / PySide6 / FastAPI / SQLite** 的 Windows 桌宠(运行在桌面上的 AI 角色),
完全本地运行、模型层完全可插拔、带持久记忆与关系系统,具备识屏 / 陪玩 / 摄像头感知等主动行为能力,
且任何组件失败都有降级路径。

## 产品理念(最高优先级约束)

- **本地优先**:对话史、记忆、关系数值全部落在本机 `data/`,程序**零遥测**;数据外发面只有三种且全部由用户显式配置(见「隐私」)。
- **单人可维护**:进程边界最小化(音频与 AI 链路全驻留 Python 单进程,原始 PCM 不跨进程);切换 = 重启式;拒绝 Electron/Tauri/Web 栈。
- **AI Native 铁律**:每一条 `reply_end` 之后必须有状态被写回(记忆 / 关系数值 / 话题钩子至少其一),且下一轮交互能感知——**CI 断言级约束**(`test_native_loop.py` 冻结时钟跑完整循环),「聊过就忘」是编译期错误级别的缺陷。
- **两条体验承诺**:不打扰用户(一切主动行为过 gate,LLM 判定失败一律按「不该说话」处理,fail-closed);降级永远可达(降级语义事先定义在权威降级矩阵,不是事发时拍脑袋)。

## 功能特性

**陪伴体验**

- 文字 + 语音双通道:打字回车即回;点 🎤 直接说话,自动断句(静默约 0.7s),本地 faster-whisper 识别、录音不上传,键盘/电视等杂音自动过滤
- 9 情绪立绘 × 5 套服装,朝向自动跟随气泡;打字机气泡逐字呈现,同屏句数 1~6 可调
- TTS 开口说话:本地自训音色(GPT-SoVITS)或云端克隆音色,失败降级纯文字气泡——**音色撕裂比音质下降更破坏人格一致性**,句间不切换音色
- 打断:配好 AEC + `audio.barge_in: true` 后说话即打断(默认关,保守半双工防自听回环)

**记忆与关系**

- 三因子检索:`0.55*相似度 + 0.25*时间衰减 + 0.20*重要度`,sqlite-vec cosine,粗召回 4×top_k 精排截 top-k;检索超时 200ms 空返回无感知
- 持久化写队列:库旁 `{db}.pending.json`,入队即原子落盘,进程崩溃不丢状态写回(重复重放幂等)
- 关系四阶段(0-20/20-50/50-80/80+)按 affection 出语气提示;评估失败静默跳过数值不变
- **每角色一库** `data/memory/{package_id}.db`,角色隔离只靠 db 文件粒度

**主动与感知**

- 五类信号源:时段问候(每日 1 次)/ 记忆钩子(8h)/ 手动截屏热键(30s 冷却)/ 闲置嗅探(默认 4h)/ 未回扣话题(2h,追问一次即消费)
- 识屏双路路由:OCR 命中且文本充分 → 纯文本 LLM,VLM 一次不调(近零成本);OCR 缺失 → 手动热键直走 VLM
- 陪玩模式三类型:游戏/视频只在暂停片尾静默窗口开口/浏览器按有无声音自动切双语义(防剧透);独立冷却 + 会话上限
- 摄像头感知(默认关):首开显式同意框,帧与观察永不落盘,DND 时客户端源头停发
- **统一 gate 顺序:DND → 全屏白名单 → 冷却 → 信号强度 → LLM 时机判定**,任何异常/无 state 上报/LLM 失败一律不搭话

**可插拔与降级**

- LLM/ASR/TTS/VLM/AEC 全部 base 类 + 实现 + 配置驱动工厂,provider 一律 lazy import,重依赖放 extras
- 出厂配置全 mock(零联网零密钥),UI 手感先行——设计原则「UI 与 AI 解耦」
- 权威降级矩阵(见下),每行都有对应单测

**角色系统**

- 角色包单活:`data/characters/*.char` 多包共存,settings 激活,含路径穿越防护;包缺失/非法回退内置 miyako.char + 启动横幅 + 托盘提示
- 一键配置角色包 / 导入 SillyTavern 卡(v1/v2/v3,`.json` 或带卡面 `.png`),宏替换 `{{user}}/{{char}}`,卡面图九情绪统一映射
- 音色恒借激活包参考池:换卡不掉声音(无配套权重自动接线 `data/tts/` 默认权重)

**安全与隐私**

- server **仅监听 127.0.0.1**;首条 `Auth(token)` 鉴权,5s 未过 close(4401),未鉴权连接零业务消息
- 零遥测;外发面仅三类且全部用户显式开启
- 摄像头:默认关 + 首开同意框 + 帧与观察**永不落盘**(CI 零文件写执法)+ DND 源头停发
- BYOK:密钥只放 `.env`(不进 git、不进发布包),settings.yaml 禁止写密钥

**工程保障**

- 协议先行:`server/protocol.py` 是全部消息的唯一事实源,禁止其它文件硬编码消息类型
- import 约束 AST 扫描执法(platform 边界 / presenter 边界 / settings 单一入口 / character_id 期权列五重断言)
- SQL 全部 execute 调用点内联静态字面量 + 参数绑定;测试全部 hermetic(注入缝)+ 固定时钟

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 / UI | Python 3.10+ · PySide6 |
| 服务 | FastAPI + uvicorn(WebSocket)· Pydantic |
| 记忆 | SQLite(WAL)+ sqlite-vec(vec0 虚拟表,cosine) |
| 语音 | sounddevice(PyAudio 兜底)· faster-whisper(CPU int8)· pyaec(Rust Speex AEC)· webrtc-audio-processing(非 Windows)· pycaw · miniaudio |
| 摄像头 | QtMultimedia |
| LLM / VLM | OpenAI 兼容端点(任意厂商 / 本地 Ollama) |
| 本地 TTS | GPT-SoVITS api_v2(旁侧服务,9880) |
| 打包分发 | bat 薄壳 |

## 系统架构

```
┌─────────────── client(PySide6 GUI 进程,run_client.py)───────────────┐
│ 立绘 pet_window ← presenter(50ms 演出循环)← sprite_manager           │
│ 气泡 · 输入栏(文本+🎤)· 托盘 · 设置窗 · 历史窗 · 导入对话框            │
│ 音频链: audio_capture → AEC(speex/webrtc) → voice_vad/barge_in → playback │
│ 感知链: screen_capture+ocr_filter · camera_capture/watcher · state_reporter │
│ 平台隔离: platform/(base+win32+mock),ctypes/pycaw 只准出现在 win32.py   │
└───────────▲────────────────────────────────────┬──────────────────────┘
            │ WebSocket ws://127.0.0.1:9800/ws   │ 首条 Auth(token)
            │ 下行 Reply/Audio seq 全序           │ 5s 未过鉴权 close(4401)
┌───────────┴────────────────────────────────────▼──────────────────────┐
│ server(FastAPI 进程,run_server.py)                                    │
│ orchestrator: 检索→组装→流式→契约解析→合成→状态写回闭环                  │
│ persona · relationship · behavior(gate)· screen_router · vision_observer│
│ contracts/(输出契约)· protocol.py(唯一消息事实源)                       │
│ memory: store(WAL+持久写队列)· vector(sqlite-vec)· dedup · summarizer   │
│ providers: llm/asr/tts/vlm 全部 base+实现+工厂(lazy import)             │
│ session_stats · tokens · usage_store(Token 计量三层)                    │
└────────────────────────────────────────────────────────────────────────┘
```

**关键架构决策**

| 决策 | 内容 | 理由 |
|---|---|---|
| C/S 而非单体 | client/server 分进程,WebSocket 通信(`proto_ver: 1`) | GUI 崩溃不拖垮服务端,server 可无头运行 |
| 全 mock 可启动 | 出厂配置全 mock(零联网零密钥) | UI 与 AI 解耦,手感先行,模型逐个换真 |
| 模型层完全可插拔 | 全部 base 类 + 实现 + 配置驱动工厂 | 装不上不影响主功能,按矩阵降级 |
| 单实例 | 命名互斥量 `miyako_single_instance` | netstat 无法区分「已运行」与「端口被占」,可能误判的检查不如不做 |
| 角色包单活 | 多包共存,settings 激活,每角色一库 | 角色隔离只靠 db 文件粒度,备份/重置天然按角色 |

**降级矩阵(节选权威清单)**

| 失败点 | 行为 |
|---|---|
| LLM API 失败 | `tone_examples` 随机一句 + `Error{scope:"llm"}` 气泡兜底 |
| TTS 音色级探活失败 | reply 级锁定至 silent mock → 本轮纯气泡,托盘提示 |
| TTS 中途失效 | 剩余句纯气泡(句间不切换音色) |
| voice_postprocess 失败 | 原文直通(fail-open) |
| ASR 失败 | `Error{scope:"asr"}`,「没听清」 |
| AEC 缺失 | 直通 + 播放期静音门 + barge_in 强制 false |
| OCR 缺失 | 禁闲置嗅探,手动热键直走 VLM |
| VLM 超时 / 失败 | 跳过屏幕上下文继续对话 |
| 记忆检索超时 200ms | 空 context 无感知 |
| sqlite-vec 装载失败 | 纯关键词降级(同签名) |
| sprites manifest 全缺 | 内置 fallback manifest 占位图 |
| gate LLM 失败 / 无 state | **不搭话(fail-closed)** |
| WS 连接被拒 | 3 次且 >30s 豁免窗 → 气泡提示安全模式 → 停止重连 |
| WS 断线 | 上行缓冲补发、下行静默丢弃、指数退避 |

## 快速开始

> 前提:Windows 10/11。首次需联网下载组件(约几百 MB),之后完全离线。

```bash
# 1. 双击 install.bat:找 Python → .venv → 依赖三源自动切换 → setup_env.py → preflight 体检
# 2. 双击 启动miyako.bat:依赖校验 → 拉起 TTS 旁侧服务 → 无窗口化启动 server → 立绘 + 托盘出现
# 3. 排障用 启动miyako(安全模式).bat:前台 debug 控制台滚动显示日志
```

- 首次启动自动初始化 `data/`;**只能同时运行一份**(重复启动被拦截并气泡提示)
- 出厂 mock 模式即可体验完整 UI;接真模型见下
- 完整安装、运维命令、FAQ:[miyako使用手册.md](miyako使用手册.md)

**接入模型(两步)**

```yaml
# .env(从 config/.env.example 复制改名)
OPENAI_API_KEY=你的密钥            

# config/settings.yaml
llm:
  provider: api
  api:
    base_url: "https://api.deepseek.com/v1"
    model: "deepseek-chat"
```

本地大模型(Ollama):`base_url: http://127.0.0.1:11434/v1`,`.env` 留空即可。

**语音(可选)**

- TTS:`providers.tts: gptsovits`(本地自训,9880 端口,`启动miyako.bat` 已内置自动拉起)或 `cloud_clone`(云端克隆,密钥填 `.env` 的 `CLOUD_TTS_API_KEY`)
- ASR:`providers.asr: mock` 改为 `whisper`(本地识别);想快一点把 `asr.model_size` 改成 `base`

**角色**

- 托盘 ▸ **一键配置角色包…**:导入卡 → 接线本地语音权重 → 激活重启,一条龙
- 托盘 ▸ **导入酒馆卡…**:SillyTavern 卡(`.json` 或带卡面 `.png`)转包导入,音色恒借当前激活包
- 托盘 ▸ **切换角色**:重启式切换,reply 进行中先 interrupt 等落库(≤3s 超时强制)

## 配置速查

你的可编辑面只有**两个文件**:

| 文件 | 放什么 | 注意 |
|---|---|---|
| `config/settings.yaml` | 全部功能配置(带注释) | YAML 缩进敏感;单文件分节,读取唯一入口 `load_settings()`(fail-open) |
| `.env` | 密钥 | 绝不外传;绝不写进 settings.yaml |

主要配置节:`character`(激活角色包)/ `server`(仅本机回环,写别值强制覆盖)/ `llm`(四槽位路由,chat 槽默认禁 thinking——实测首字 8.8s→0.8s)/ `providers`(TTS/ASR/VLM 选型)/ `asr` / `audio`(AEC、打断)/ `voice` / `behavior`(主动搭话:问候/嗅探/冷却/静音名单/热键)/ `camera`+`vision` / `ui_theme` / `screen_triggers`(识屏规则 + 陪玩)/ `update`。

三条铁律:标注「实时」的键立即生效,其余需完全重启;任何键写错都不崩,回退默认值(先看 `logs/` 的 warning);角色包是资源数据,不用记事本改。

## 观测与运维

- **Token 计量三层**:跨重启累计(`usage_store`)→ 会话级(`session_stats`,轮 = ReplyBegin→ReplyEnd 漏斗)→ `/metrics` 暴露 `tokens_lifetime` / `tokens_session`;真实 usage 优先,厂商不支持回落估算并标 `estimated`(禁止计量静默归零)
- **成本观测**:`price_cny_per_1m` + `price_cny_per_1m_cached`(缓存命中约 5 折),设置窗 Token 页显示金额
- **延迟预算**:ASR 尾点 ≤800ms · 记忆检索 ≤200ms · LLM 首句 ≤1200ms · TTS 首块 ≤500ms · **合计 ≤2.5s**(`/metrics` p50/p95,`test_latency_budget` 守护埋点)
- **日志**:`logs/server.log` 与 `logs/client.log` 各 RotatingFileHandler(5MB×3,utf-8);TTS 旁侧服务输出 `logs/tts_console.log`
- **托盘状态指示**:TTS 正常 / TTS 降级:scope → fallback(不可点,只看)

## 隐私说明

miyako **零遥测**:不收集、不上传任何使用数据、崩溃数据或统计。

数据外发**只有**以下三种情形,全部由用户主动开启:

1. 填了 AI 服务密钥:对话文本、(可选)识屏画面 / 摄像头帧、(可选)云端语音合成文本,发往**你自己配置**的服务商;
2. 点了「检查更新」:向配置的地址发一次不含用户数据的 GET(默认关闭且未配置);
3. 启用了云端克隆音色:待合成文本发往你配置的 TTS 服务商。

本地路径(语音识别、本地 OCR、记忆库、本地合成)**永不外发**;摄像头画面与观察结果**永不落盘**。

## 卸载与备份

- 双击 **`卸载.bat`**:只删运行环境(`.venv`),`data/`、`config/settings.yaml`、`.env` **全部保留**;想彻底清除手动删这三项
- 备份 = 复制这三样即可完成全部备份

## 文档索引

| 文档 | 内容 |
|---|---|
| [miyako产品介绍.md](miyako产品介绍.md) | 架构权威、协议全文、模块细节、降级矩阵全文、工程纪律(开发者向) |
| [miyako使用手册.md](miyako使用手册.md) | 安装 / 启动 / 托盘菜单 / 配置速查 / FAQ / 卸载备份(用户向) |
| `config/settings.example.yaml` | 各配置键的完整注释 |

## 出问题了怎么办(精简 FAQ)

| 症状 | 解法 |
|---|---|
| 双击启动没反应、无托盘图标 | 大概率**已在运行**(单实例保护);真没运行就上安全模式看日志 |
| 提示找不到 Python | 装 Python 时勾选 `Add python.exe to PATH`,重跑 `install.bat` |
| 提示「依赖清单已变化」 | 重新双击 `install.bat`(`check_req_hash.py` 哈希校验) |
| 她会打字但从不说话 | 没配 TTS 即纯文字;已配则看托盘 TTS 指示是否降级,并确认语音服务已启动 |
| 语音服务开着但声音难听 / 机械音 | GPT-SoVITS 冷启动要几分钟或权重路径不对,看 `logs/tts_console.log` |
| 🎤 开着但她说「没听清」 | 未装本地识别组件(重跑 `install.bat`)或环境太吵被防幻听闸门拦 |
| 全屏打游戏她不理我 | 查三处:托盘 DND、游戏进程名是否进静音名单、陪玩名单是否含该游戏且类型写对 |
| 立绘表情不动 | 自己换的包没图会显示占位图(正常降级) |
| 怀疑她记错了 | 设置 ▸ 记忆 页签可校对 / 编辑 |
| 仍然没救 | `logs/server.log` 与 `logs/client.log` 最后 50 行打包发维护者 |


## 路线图

- [x] C/S 分进程 + 协议先行 + 全 mock 可启动 · 记忆子系统(SQLite WAL + 三因子检索 + 持久写队列)
- [x] Provider 可插拔体系 + 权威降级矩阵 + 每行单测 · Token 计量三层 · 延迟预算
- [x] 角色包系统(单活激活 / 路径防护 / 每角色一库)· 一键配置角色包 · 酒馆卡导入
- [x] 主动搭话五信号源 + 统一 gate(fail-closed)· 识屏双路 · 陪玩三类型 · 摄像头感知
- [x] 工程纪律:AST 扫描执法 / SQL 静态字面量 / 测试 hermetic + 固定时钟 / 发布三用例
- [ ] Silero-ONNX VAD 主选 / 能量兜底双层方案(选型实测已完成,待接线)
- [ ] summarizer 生产接线(`[reflect]` 自动触发,当前仅手动调档)
- [ ] Alertmanager 式外部告警通道(当前仅托盘/气泡提示)

---

*完整操作见 [miyako使用手册.md](miyako使用手册.md)。*
