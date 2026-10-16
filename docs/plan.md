# miyako 开发计划 plan.md（v1.9.9 → AI 工程师执行版 · 自足定稿）
> **文档性质**：本文件是写给 AI 编码工程师（GLM/zcode）的**完整执行计划**，同时是**自足文档**——所有实施所需的设计决策、降级语义、禁止事项已收录于 §0 与 §4，正常情况下无需阅读设计文档。
> **配套文档**：设计底稿 `miyako_v1.9.9.md` 由用户保管，**不进工作文件夹**；AI 仅在 §4「点播协议」定义的情形下接收用户粘贴的对应章节。
> **文档版本**：基于 miyako_v1.9.5（= v1.8 审评修复 + v1.9 角色插拔 + v1.9.2 四项外部借鉴 + **v1.9.3：Shinsekai 六点深度设计借鉴（§0.4 速查表新增行）+ M-CAM 摄像头感知 Tier C（独立里程碑）** + **v1.9.4：AEC Windows 平替定案（§0.4 速查表新增行，M+1.5.1 行同步）** + **v1.9.5：发布后验收修复与幻听防线/延迟批次①/查看历史对话归纳（§0.4 速查表新增 3 行）** + **v1.9.6：发布后第二批归纳（§0.4 速查表新增 6 行：陪玩模式/cloud_clone 档/Token 监控/立绘持久情绪与多轴利用/托盘退出两档化/settings 唯一入口；M-PLAY 实装标注、M-SPRITE 追认立项）** + **摄像头/麦克风开关状态持久化（2026-09-13：🎤 voice.autostart_mic 切换即写回+启动自动恢复；摄像头 consent 存档免重复同意框+启动失败可见化）**，均 2026-09 用户在对话中定案 + **v1.9.7：发布后第三批归纳（TTS 音色链收敛[只留 gptsovits/cloud_clone，失败落纯气泡]/隐私组进设置窗+托盘摄像头开关移除[首开同意框随迁]/立绘休眠唤醒回归修复/立绘高度上限取消[原 560]/陪玩名单全角标点容错；§0.4·§4.1 TTS 行已同步）** + **v1.9.8：发布后第四批归纳（§0.4 速查表新增 3 行：life 卡字段消费与 CONSUMED_CARD_FIELDS 契约/relationship dimensions 钳制消费与 gptsovits 候选池兜底/上下文容器 8192[life 全量卡实测根修]）** + **v1.9.9：发布后第五批归纳（§0.4 速查表新增 4 行：全量 logging 基础设施/DND 可见性三补与 idle_sniff.hours 接线/Token 会话级统计与陪玩·VLM 独立计量/启动器无窗口化与 uvicorn pythonw 兼容）**，均 2026-09 用户在对话中定案）。设计文档修订时，必须按 §3.2 同步规则更新本文件。
---
## §0 全局约定（每次会话必读）
### 0.1 项目是什么
miyako 是一个**单人可维护、跑在 Windows 桌面的本地 AI 陪伴角色**：Python 3.10+，PySide6 客户端 + FastAPI 服务端，WebSocket 通信（`proto_ver: 1`），全 mock 可启动，模型层完全可插拔。产品壳固定、**角色包可插拔（单活，v1.9）**：`data/characters/*.char` 多包共存，settings `character.package_id` 激活（缺省 miyako），每角色一库，重启式切换；不做多宠并存/同窗群聊/运行时热切换。
**AI Native 铁律**（最高优先级）：每一条 `reply_end` 之后必须有状态被写回（记忆/关系数值/话题钩子至少其一），且下一轮交互能感知到该状态。CI 有专门断言（`test_native_loop.py`）守护。
### 0.2 十二条设计原则（违反任何一条 = 返工）
1. **模型层完全可插拔**：LLM/ASR/TTS/VLM/AEC 全部是 base 类 + 实现 + 配置驱动工厂（`factory.py` 读 settings.yaml 实例化），provider 一律 lazy import，重依赖放 extras；
2. **协议先行**：`server/protocol.py` 是 client/server 所有消息的唯一事实源，禁止在其它文件硬编码消息类型；
3. **每层有 fallback**：任何 provider 挂掉都有降级路径，降级发 `error` 消息 + 托盘提示。**全部失败路径的权威清单见 §4.1 降级矩阵——实现任何 provider / 失败路径前，先对照该矩阵**；
4. **UI 与 AI 解耦**：M0~M1 全部用 mock provider 跑通 UI 手感，M2 起才逐个换真模型；
5. **状态沉淀闭环**：交互→落库→召回→影响演出与时机，循环闭合；
6. **不打扰用户**：主动行为必须过 gate（时机判定门），gate 的 LLM 判定失败按“不该说话”处理（fail-closed）；
7. **禁止 AI 调用轮询**：允许零成本本地信号巡检（像素 hash、输入空闲计时），任何云端上传必须事件触发 + 全量过 gate；
8. **分发友好**：server 端口与绑定地址在 settings.yaml 显式定义；bat 只做“找 Python → 校验依赖 → 调脚本”薄壳；初始化收敛在 setup_env.py（幂等）；
9. **进程边界最小化**：音频链路与 AI 链路全部驻留 Python 单进程，原始 PCM 不跨进程边界；
10. **角色包可插拔（单活）+ 禁止投机通用化**：角色资产收在 `data/characters/{package_id}.char/`，多包共存；**激活角色 = settings.yaml `character.package_id`（缺省 "miyako"）**，包路径经 `resolve_character_package()`（定义于 `server/constants.py`）解析——`CHARACTER_PACKAGE_ID` 常量已退役（v1.9）。**每角色一库 `data/memory/{package_id}.db`**：隔离只靠一库一角色，**禁止 character_id SQL 过滤/跨角色查询**（期权列仅作 forensic 标记，无索引无过滤）。不做的：多宠并存、同窗群聊、运行时热切换；
11. **单一职责验收**：任何模块必须能回答“删掉它会坏什么”；同一关注点不得拆两个模块（闭集校验只在契约、行缓冲只在 orchestrator、单实例判定只有命名互斥量）；
12. **系统调用隔离**：业务代码禁止直接 import ctypes/pycaw 等，全部经 `client/platform/` 的 PlatformProvider 接口（测试接缝，不是跨平台承诺）。
### 0.3 两条 import 约束（CI 执法，永不可违反）
- **约束 A（platform 边界）**：`client/` 下除 `platform/win32.py` 与 `platform/base.py` 外，任何文件禁止 `import ctypes / win32api / pycaw`；
- **约束 B（presenter 边界）**：`client/` 下除 `presenter.py` 与 `sprite_manager.py` 外，任何文件禁止 `import sprite_manager`。
两个约束各有一个测试文件执法（`test_platform_provider.py`、`test_stage_presenter.py`），实现方式：扫描源文件 AST/import 语句。
### 0.4 关键技术决策速查（实施时按此执行，勿自行更换）
| 项 | 决策 |
|---|---|
| TTS 合成源 | `sentence` 消息的 **text_ja**（与 ja 音色一致）；text_zh 是气泡显示源。两者都过 voice_postprocess（各自语言节） |
| TTS 四级链 | ①GPT-SoVITS(9880探活)→②edge-tts→③预录音频→④纯气泡；reply 级锁定，中途失败当场降级 |
| ASR | faster-whisper 本地 CPU int8，extras: asr |
| AEC | webrtc-audio-processing，10ms 帧对齐，`threading.Lock` 包 APM 入口；extras: aec |
| 音频采集 | sounddevice 默认（回调模式，16kHz，blocksize=512≈32ms，PCM16），PyAudio 兜底 |
| barge-in 双档 | AEC 就绪：残差>0.03 持续 400ms；无 AEC：非播放期能量>0.06 持续 600ms；**实机阈值=max(表值, 2×rms_floor)，3s 环境噪声基线标定（v1.8）**；AEC extras 缺失时 runtime 强制 false |
| 记忆 | SQLite + sqlite-vec，**每角色一库 data/memory/{package_id}.db（v1.9）**，三因子评分 `0.55*sim + 0.25*exp(-hours/(72*(0.5+importance))) + 0.20*importance`（v1.8 半衰按 importance 加权），检索超时 200ms 返回空 |
| 情感闭集 | 9 元：normal/happy/shy/pout/angry/sad/surprise/thinking/sleepy，唯一定义于 `server/emotion_defs.py` |
| LLM 输出格式 | 行式 `[e:情绪]中文|日本語`，由 Output Contract 解析（缺`\|`→整行当中文 tts_ok=False；未知表情→normal；失败不抛异常） |
| 单实例 | 命名互斥量 `CreateMutexW("miyako_single_instance")`，`ERROR_ALREADY_EXISTS=183` → 气泡提示退出。bat 不做端口探测。**互斥量名保持应用级不随角色变（v1.9 单活保证）** |
| 安全 | server 仅监听 127.0.0.1（读到其它值强制覆盖+warning）+ **首条 `auth(token)` 鉴权（v1.8：token 文件 data/server.token，5s 未过断开 4401）**，e2e 断言 |
| 下行音频（v1.8） | audio_* 经服务端**单发送队列按 seq 全序**，客户端到达即播；跨 reply 采样率变化重建播放流；interrupt = 客户端本地 flush + 服务端 cancel（乐观打断） |
| 角色激活（v1.9） | settings.yaml `character.package_id`（缺省 "miyako"）经 `resolve_character_package()` 解析；每角色一库；切换 = 托盘重启式（M-CHA）；产品级标识（日志目录/bat/互斥量/产品名）不随角色变 |
| 延迟 SLA | ASR尾点≤800ms / 记忆≤200ms / LLM首句≤1200ms / voice_postprocess≤5ms / TTS首块≤500ms / 合计≤2.5s，`/metrics` 暴露 p50/p95 |
| token 计量 | 流式请求带 `stream_options:{"include_usage":true}`，厂商不支持→`estimate_tokens()`（CJK 1 token/字、其余 len//4，**全项目唯一实现**，与上下文预算同源，v1.8）+`estimated:true`，禁止计量静默归零 |
| 应用层配置合一（2026-09 用户定案） | ui_theme / screen_triggers **并入 settings.yaml 分节**（用户可编辑面 = 1 配置文件 + 1 机密文件）；`.env` 只放机密（OPENAI_API_KEY 等，禁止写进 settings.yaml）；`.char` 角色包是资源数据包不属配置层；各节缺失/非法一律 fail-open 内置默认；不做独立主题/规则文件的兼容读取（单一事实源） |
| 摄像头感知（v1.9.3 M-CAM，Tier C） | 默认关+首开显式同意（告知画面将定期发往 VLM 云端/帧与观察不落盘/可随时关）；DND **源头停发**；节流采样 interval_s≥15 + mini-gate（冷却/每日上限/reply 空闲，M+2.3 后换全量 gate）；采集 QtMultimedia（零新增依赖）；VLM=`providers.vlm: openai`（OpenAI 兼容多模态，vision 节回落 llm.api）；观察输出 SKIP=静默；帧与观察**永不落 memory.db** |
| 记忆写队列（v1.9.3 借鉴①→M+1.1/1.3） | emotion 槽评估与后台写库经**持久化队列**（每角色库旁 pending 存储，JSON 落盘、重启重放、成功出队）——进程崩溃不丢 AI Native 状态写回；条目含 confidence（钳 0~1）/source/created_at |
| Prompt 分段组装树（v1.9.3 借鉴②→M+1.6） | persona/memory/history/user 段用 **Section 冻结树**表达（priority 排序+同级 id 唯一双重校验+空输出省略+enabled 跳子树+动态子节点钩子+render @final）；超预算**按 priority 从低砍整段**（非字符串拦腰截断）；用户文本节点永不参与 format 展开（注入防线入类型层） |
| token 估算信封化（v1.9.3 借鉴③→estimate_tokens 扩展） | 保持唯一实现不引入 tiktoken；新增**消息信封常数**（每条消息固定开销计数）与**多模态占位清洗**（图片等媒体换占位符再计，防撑爆计数）；保守回落偏大不偏小 |
| 阈值历史压缩（v1.9.3 借鉴④→M+1.6） | 历史达预算阈值触发→压缩到目标占比→保留最近 N 条→摘要自身限预算→历史留「历史对话总结」标记；阈值/目标占比经 clamp 强制不变式（目标<阈值拒绝）；与单次组装预算互补（预算管当场、压缩管跨场） |
| 后台槽修复重试（v1.9.3 借鉴⑤→M+1.3/1.5） | emotion 评估与日记等**后台槽**解析失败：先一次修复重试（原输出 append 回去+升级措辞指令+剥 tools+deepcopy 不污染持久历史+上限 2 次+每轮查取消）再静默放弃；**前台对话链保持 fail-open 不重试**（延迟红线） |
| 单一规范化视图（v1.9.3 借鉴⑥→M+1.6 audiofile） | 情绪→预录音频绑定建**唯一规范化结构**，prompt 侧（能力目录）与运行时（查表播放）共用同一视图，禁两处各自解析 yaml；对齐纪律：空标签行保留防错位（同 quote_guard 品味） |
| LLM 供应商参数透传与 thinking（v1.9.3 定案） | `llm.api.extra` 字典**浅合并**进请求体顶层（extra 键覆盖同名基础键；槽位 dict 可携带 extra 实现分槽差异化）。**chat 槽默认禁 thinking**（实测对话首字 8.8s→0.8s，质量 A/B 无实质差异；diary/correction 等后台槽不赶时间，可用槽位覆盖单独开启）|
| 外部项目借鉴红线（v1.9.3 定案） | Shinsekai 等源码可见项目：**设计思路可借鉴、代码一行不搬**（其许可证禁止再分发/衍生发布）；借鉴必须先按 §3.2 落档再实施 |
| **AEC Windows 平替（v1.9.4 定案→M+1.5.1）** | `webrtc-audio-processing` 在 Windows 不可用（PyPI 全版本仅源码包+Linux ARMv7 轮子，2020 停更；C++ 源码构建 Windows 失败——平台问题非网络问题，实锤）。**Windows 缺省 AEC = `pyaec`（Rust 版 Speex AEC，官方 win_amd64 轮子，合成信号实测 33.9dB）**；webrtc 绑定保留为 Linux/macOS 选项（extras 按 `sys_platform != 'win32'` 标记）。工厂 `audio.aec: speex|webrtc|none` 可插拔；系统混音峰值用 pycaw（新版 GetSpeakers 返回 AudioDevice 包装，取 `._dev` 兼容） |
| **dialog_v1 多段行假名判别（v1.9.5 定案→M+3 真机修复）** | 3 段及以上 `|` 分隔的行按假名归侧（含假名=日文/否则=中文，各按原顺序 `\n` 连接）——旧「第一个 | 右侧全归 text_ja」会把第 2+ 句中文并进 TTS 文本（「语音比气泡多几句」根因）；两段常规形态逐字不变。测试锁定于 `test_output_contract` |
| **检索池白名单（v1.9.5 定案→查看历史对话）** | dialogue 原文按组落库（单条=「[用户] …\n[miyako] …」，写队列+滚动裁剪 500 组）**只回放不召回**——`vector.RETRIEVAL_KINDS=(fact/diary/emotion_tone)`，向量同步与关键词候选两处读点同排除；数据面 = `MemoryAdmin.dialogue_rounds`（托盘「查看历史对话…」只读窗口） |
| **幻听防线（v1.9.5 定案→ASR）** | faster-whisper 非语音幻觉转写（「谢谢观看」类）= 幻听主源：`transcribe` 增 Silero VAD 预过滤 + condition_on_previous_text=False + 段级质量闸（`asr.no_speech_prob>0.6` / `asr.avg_logprob_min<-1.0`，可配置）+ 幻觉短语黑名单；空转写**静默丢弃**（零下行，区别于「ASR 失败→没听清」降级行） |
| **life 卡字段消费契约（v1.9.8 定案，pet 适应 life）** | persona 渲染 life 卡 behavior_rules（if-then「若…，…。」）/anti_patterns（【角色反模式】独立段，不并 taboos）；`CONSUMED_CARD_FIELDS` 固化消费清单=life interface.yaml 的配对源；honesty_boundary 有意不渲染（裁定，注释宣告）；旧包缺字段 fail-open 跳过 |
| **relationship dimensions 钳制消费 + gptsovits 候选池兜底（v1.9.8）** | relationship.yaml dimensions 的 initial/min/max/per_reply_delta 全消费（per-dimension 钳制，缺失回退 ±3/0~100，initial 钳进区间）；tts_gptsovits 段缺参考配置时 candidates.yaml rank#1 自动补位（**同源配对**：显式 ref 只配同名转写，宁缺毋错） |
| **上下文容器 8192（v1.9.8 定案）** | life 全量卡 persona 段实达 ~2463 tok（30%×4096 的 2 倍），4096 容器下 compose 砍 memory 段=记忆召回失效；升 8192 后 persona+memory 共存（回归用例锁），增量成本由上下文缓存（命中约 5 折）摊薄 |
| **全量 logging 基础设施（v1.9.9）** | `server/logsetup.py` 统一装配：`logs/server.log`、`client.log` 各 RotatingFileHandler（5MB×3 INFO，utf-8）；root 恒 INFO 防三方 DEBUG 刷屏，应用包 server/client 级别按 `--log-level`/`MIYAKO_LOG_LEVEL` 放开（安全模式 debug 真正传导到应用层）；幂等重挂+fail-open；此前全项目零 logging 配置=INFO 整层丢弃（ASR mock 事故土壤）；preflight 增「日志文件」项 |
| **DND 可见性三补 + idle_sniff.hours 接线（v1.9.9）** | `tick()` DND 短路改走 `_tick_dnd_paused`：节流 INFO（5min）+ 名单应用全屏被 DND 拦 → 气泡提示一次「陪玩吐槽已暂停」（episode 复位）；托盘切换 DND 即时气泡确认；`idle_sniff.hours` 经 `idle_threshold_from_cfg` 真实接线（此前硬编码 180s，非法回落 4h、地板 60s） |
| **Token 会话级统计 + 陪玩/VLM 独立计量（v1.9.9）** | `session_stats.py`（内存态，重启清零=会话边界）：轮=ReplyBegin→ReplyEnd 漏斗，llm_api 总量差值计量 总量/轮次/单轮最高最低；来源分桶 user/behavior/screen/**companion**（screen_router 陪玩分支全链透传）；VLM `record_usage("vision",…)`（真实 usage 优先，否则文本估算）+ `record_extra` 进总量/桶 `vlm_tokens`（**不进轮次 max/min**——轮=reply 漏斗口径不变；陪玩桶=轮次+画面理解=真实总账）；/metrics 增 `tokens_session`；Token 页「本次使用」区块（含陪玩/VLM 行，旧版 server 优雅占位） |
| **启动器无窗口化 + uvicorn pythonw 兼容（v1.9.9）** | 启动 bat server 行 `start "" pythonw`（黑窗口被误关=server 无声死亡根治；GUI 子进程不阻塞 bat、不随启动器窗口关闭而死）；run_server 全 print→logging（pythonw stdout=None 时 print 崩）+ `uvicorn.run log_config=None`（缺省 formatter 在 stdout=None 下 dictConfig 抛 ValueError=无窗口秒死根因）；uvicorn 日志传播到 root 落 server.log；安全模式 bat 保持前台控制台（调试） |
| **陪玩模式（v1.9.6 定案→M-PLAY，已实装）** | 识屏三触发域（闲置嗅探/陪玩/桌面 ambient[留设计]）；名单 `进程名:类型`（game=场景切换[大变化+稳定≥2s] / video=音频静默≥8s 防剧透 / web=按进程音频状态路由双语义[有声看番静默才说、无声看网页翻页可说]）；判据=前台窗口覆盖显示器**或工作区**（最大化亦计）；陪玩域接管挂起闲置嗅探；独立冷却+会话上限；VLM 一次调用两用（SKIP 静默，走 build_vlm 工厂=本地模型即插拔点）；gate 拒不吞额度；合规=事件触发（原则 7）；pycaw 仅枚举渲染会话（麦克风不进判别）；media_mode lively 留预项 |
| **cloud_clone 显式档（v1.9.6 定案→TTS）** | 音色云端克隆：OpenAI 兼容 `POST {base_url}/audio/speech`（response_format=wav 流式剥头，采样率按 fmt 段如实上报；BYOK 环境变量）；链 `[cloud_clone→audiofile→mock]` **刻意跳过 edge 防音色撕裂**；不进 ①~④ 自动链，用户显式指定；README 透明度声明扩为三类外发面 |
| **Token 监控（v1.9.6 定案→/metrics+设置窗）** | cached_tokens 归一采集（智谱 prompt_tokens_details / DeepSeek prompt_cache_hit_tokens；估算路径不造假）；跨重启累计 usage_store（data/memory/{角色}.usage.json，路径由 memory_db_path 派生）；/metrics 增 tokens_lifetime 与 cost 缓存差价（price_cny_per_1m_cached，命中约 5 折）；设置窗「Token 监控」页（轮询/隐藏即停） |
| **立绘持久情绪与多轴利用（v1.9.6 定案→M-SPRITE）** | 情绪=持久心境（IDLE 不再回退 normal——瞬态演出是频闪根因）；EMOTE 限真 motion frames；化妆死代码删除（breathe/blink/mouth 无素材且渲染端从未消费）；换装 5 选含裸装 + 朝向 auto 跟随气泡（阈值 150px）/手动锁定；全量合成器 compose_all_sprites（179 张归档/128 张入包，expressions_extra 闭集外差分）；饰品开关移除（无实际作用，manifest 数据留档） |
| **托盘退出两档化（2026-09-12 定案）** | 「完全退出（同时关闭 TTS）」/「仅关闭 miyako（保留 TTS 常驻）」；切角色重启复用后者；TTS 启动硬化：bat /D 钉 cwd（GPT_SoVITS sv.py 按 getcwd() 找依赖，错 cwd 静默崩溃→机械音降级真因）+ 控制台重定向 logs/tts_console.log |
| **settings 读取唯一入口（2026-09-12 定案）** | 全部 settings.yaml 解析收敛 server.constants.load_settings(path=None)（fail-open+注入缝）；源码扫描执法 test_settings_single_entry（safe_load 与 settings.yaml 引用同文件共存仅豁免该模块）；行级写入器（保注释）不属此列 |
| **酒馆卡适配（2026-09-18 定案→M-TAVERN，已实装）** | SillyTavern 卡（chara_card_v1/v2/v3，.json 或 .png 内嵌 chara/ccv3）→ **独立模式**转包导入（client/tavern_card.py + tavern_import_dialog.py + 托盘「导入酒馆卡…」）；**原卡链路零改动**（仅复用 card_importer 的 scan/import/wire）；音色=**恒借激活包参考池**（import_card 的 reference_donor，GptsovitsTts 候选池兜底接管→出声即 miyako 音色）+ **无配套权重自动接线 `data/tts/` 默认权重**（目标=settings providers.tts_service_yaml；条件缺失跳过、失败保卡）；宏替换 {{user}}→用户/{{char}}→角色名；立绘=卡面/自选图九情绪统一映射（无图落占位）；world book 不导入 |
| **链尾 mock 双形态（2026-09-18 补收敛漏网）** | 配置了音色级（gptsovits/cloud_clone）时，链尾 mock 构建为 **silent=纯气泡**（空流零音频块）——「听错误音色不如纯文本」/release notes「未启动=纯文本」的兑现（此前降级落 mock 发 1kHz 正弦滴声）；`providers.tts: mock` 出厂配置与未知值 fail-open 保持**有声**（CI/开发期口型依赖）；MockTts(silent=bool(前置音色级)) |
| **工具模式域隔离（2026-10-05 定案→M-TOOL，已实装）** | 番茄钟/闹钟[每日+一次性]/倒计时/喝水·久坐[人在座才计时、离开顺延]/整点报时[免打扰段跨午夜]/节日问候[公历+农历六大查表 2026~2035+自定义；元宵=春节+14 推导]；**定案九项**：迷你窗不做；播报=`llm.provider: api` 且在线时 ToolEvent 上行走 LLM 人格+TTS，mock/断线→客户端 LOCAL_FALLBACK_LINES 固定台词（**永不静默丢失**）；占线入队 cap2 超限丢最旧、reply 后经 on_reply_done→call_soon 补播；**全部无视 DND**（总闸 `tools.enabled` 补位总控权）；tool 轮**完全不写回**（不检索记忆/不进历史/对话不落库/后台槽全跳——AI Native 铁律对工具轮豁免，裁定文档化）；播报 system **只读 persona+tone_hint**（防记忆泄漏进播报词）；域隔离+总闸、不做模式状态机（「专注场景」未来=预设翻转既有开关）；**守卫式同漏斗**（否决独立小播报漏斗——编排双份是维护地雷）。已知取舍：播报占线期间用户输入被丢（与回复占线同款，Interrupt 可打断）。闹钟持久化 `tools.alarms`（flow 列表行级写回）；Session 统计 tool 桶自动入账 |
### 0.5 目录结构（v1.9 定版）
```text
miyako-pet/
├── docs/plan.md            # 本文件（进仓库；发布包排除 docs/）
├── install.bat / 启动miyako.bat / 启动miyako(安全模式).bat / 卸载.bat / README.txt / VERSION
├── config/ settings.example.yaml, .env.example   # 应用层配置单文件：settings / ui_theme / screen_triggers 全在 settings.yaml 分节（2026-09 合一）
├── run_client.py / run_server.py
├── scripts/ setup_env.py, preflight.py, check_req_hash.py, make_release.py
├── client/
│   ├── platform/ base.py, win32.py, mock.py
│   ├── presenter.py, pet_window.py, sprite_manager.py, bubble.py, input_bar.py
│   ├── aec_base.py, aec_webrtc.py, aec_none.py
│   ├── audio_capture.py, audio_playback.py
│   ├── screen_capture.py, ocr_filter.py
│   ├── tray.py（含「切换角色」子菜单，v1.9）, settings_window.py, ws_client.py
├── server/
│   ├── main.py, constants.py（含 resolve_character_package 角色解析）, protocol.py, orchestrator.py
│   ├── contracts/ base.py, dialog_v1.py
│   ├── emotion_defs.py, persona.py, relationship.py, behavior.py, screen_router.py
│   ├── memory/ store.py, vector.py, deduplicator.py, summarizer.py
│   └── providers/ llm_*.py, tts_*.py, asr_*.py, vlm_*.py,
│                  voice_postprocess.py, factory.py
├── data/
│   ├── characters/          # v1.9：多包共存；激活者 = settings character.package_id
│   │   └── miyako.char/ manifest.yaml（含 schema_version）, character_card.yaml,
│   │                        relationship.yaml, tts_config.yaml,
│   │                        sprites/manifest.json, reference_audio/, skill/
│   ├── fallback/            # 内置最小 sprites manifest（最终兜底）
│   ├── knowledge/
│   └── memory/              # v1.9：每角色一库 memory/{package_id}.db，运行时生成，不进发布包
├── tools/ gen_persona_derivatives.py, eval_persona.py, new_character.py
└── tests/ （见 §2 各任务中的测试清单 + 附录清单）
```
---
## §1 进度恢复规则（每次会话开工前执行）
1. 读仓库根目录 `PROGRESS.md`（不存在则视为从 M-1.1 开始）；
2. `pytest tests/ --tb=no -q` 确认现状（允许有失败，如实记录）；
3. 向用户复述：当前里程碑、本次要执行的 task 编号、对验收标准的理解；
4. 用户确认后再写代码。
**PROGRESS.md 格式**（跨会话的唯一记忆载体，每 task 结束必须更新）：
```markdown
# miyako 开发进度
- 当前里程碑：M1（假数据全链路）
- 已完成：M-1 全部、M0 全部、M1.1~M1.3
- 最近测试结果：pytest 42 passed, 1 skipped (win32 smoke on Linux)
- 遗留问题：无 / （列出）
- 下一步：M1.4
```
---
## §2 里程碑任务清单
### M-1 环境准备（0.5 周，6 tasks）
> **上下文**：建立“零外部依赖即可启动全 UI 链路”的地基。所有配置默认指向 mock provider。幂等是 setup_env 的生命线——用户中断后重跑必须从断点继续。
| Task | 内容与产出 | 验收 |
|---|---|---|
| **M-1.1** | 按 §0.5 一次性建齐目录与空文件；写 `.gitignore`（`.venv/ data/memory.db config/settings.yaml .env __pycache__/`）；`VERSION` 写 `0.7.0-dev` | 目录树与 §0.5 一致 |
| **M-1.2** | requirements.txt 分层（base: PySide6, fastapi, uvicorn[standard], websockets, httpx, pydantic, pydantic-settings, PyYAML, python-dotenv, numpy, sounddevice, pytest-asyncio, freezegun；extras: asr→faster-whisper>=1.0, aec→webrtc-audio-processing+pycaw, ocr→rapidocr-onnxruntime>=1.4, tts_edge→edge-tts>=6.1+miniaudio>=1.0, memory→sqlite-vec>=0.1, llm_ollama→ollama>=0.3）+ pyproject.toml 使 `pip install -e ".[asr]"` 可用 | 干净 venv 中 base 安装成功；extras 语法可用 |
| **M-1.3** | `scripts/setup_env.py`：①.venv 不存在则创建（sys.executable -m venv）；②settings.yaml/.env 已存在则跳过不覆盖，否则从 example 复制；③`--extras a,b` 参数；④生成的默认 settings.yaml 全 mock 且 `server: {host: "127.0.0.1", port: 9800}`；⑤每步打印 `[OK]/[SKIP]/[FAIL]` | 连跑两次第二次全 SKIP；中断重跑从断点继续 |
| **M-1.4** | `scripts/preflight.py`：14 项检查（Python≥3.10 失败退出码1 / GPU与显存 / API key / GPT-SoVITS 探活 127.0.0.1:9880 / Edge-TTS 20字 dry-run / ASR 模型缓存 / AEC import+50ms dry-run / OCR import / 音频输入设备试录200ms / sqlite-vec enable_load_extension 实际加载 / 角色包资产存在性 / mock 延迟基线 / 安装路径 ASCII / 磁盘<1GB）。**全部 lazy import，未装依赖只 ⚠️ 不崩溃，无外部服务全 ⚠️ 且退出码 0** | 干净环境：全 ⚠️ 退出码 0；每项输出白话修复指引 |
| **M-1.5** | 四个 bat（UTF-8 无 BOM + chcp 65001 + CRLF + 每个 exit 前 pause）+ README.txt。install.bat：找 Python（缺失→打开 python.org+提示勾选 PATH）→建 venv→pip install -r requirements.txt（清华镜像）→`pip install -e ".[asr,aec,ocr,tts_edge,memory]"`（失败不阻断）→setup_env→preflight→写依赖 hash→输出“✅ 安装完成！…”。启动 bat：检查 .venv → **执行 check_req_hash.py（校验 requirements sha256 与 .venv/.req_hash 一致，不符提示重装退出）** → `start /min` server → timeout 2 → pythonw client。**无端口探测**（重复启动由客户端互斥量拦截）。安全模式：--log-level debug 前台黑窗。卸载：taskkill→确认→只删 .venv，提示数据保留 | clean 虚拟机 install.bat 全流程走通；bat 校验 CRLF |
| **M-1.6** | 默认配置：settings.example.yaml 单文件（全 mock 组 + OpenAI 兼容 api 段；server 段 host/port；llm.roles 四槽位注释；**ui_theme / screen_triggers 分节**——2026-09 应用层配置合一，四条示例规则见 M+3.4）、.env.example（只放机密） | pydantic-settings 可正常解析 |
**里程碑出口**：新 clone → install.bat → preflight 报告 → 双端 mock 启动横幅。`git tag m1-env-ready`
---
### M0 协议骨架（1 周，5 tasks）
> **上下文**：protocol.py 是全项目消息唯一事实源。Output Contract 是核心设计：LLM 输出格式的解析与修复全部收在契约对象里，orchestrator 只做行缓冲。
| Task | 内容与产出 | 验收 |
|---|---|---|
| **M0.1** | `server/constants.py`：`CHARACTER_PACKAGE_ID="miyako"`、`SERVER_PORT_DEFAULT=9800`、`BIND_HOST="127.0.0.1"`。`server/emotion_defs.py`：`SENTIMENT_CLOSE_SET = ["normal","happy","shy","pout","angry","sad","surprise","thinking","sleepy"]`（后续所有闭集校验只引用此处） | 常量被后续模块引用，无第二份定义 |
| **M0.2** | `server/protocol.py`：全部消息定义为 Pydantic model，含 `type` 与 `proto_ver:1`。Client→Server：`UserText{text}`、`AudioStart{}`、`AudioChunk{data:base64}`、`AudioEnd{}`、`ScreenShot{image:base64 jpg≤1024px, source:"hotkey"\|"idle"}`、`Interrupt{}`、`State{foreground:{title,process}\|null, locked:bool}`、`Ping{}`。Server→Client：`ReplyBegin{trigger:"user"\|"behavior"\|"boot"}`、`Sentence{seq,text_zh,text_ja,emotion,motion?}`、`AudioBegin{seq,sample_rate}`、`AudioChunk{seq,data}`、`AudioEnd{seq}`、`ReplyEnd{}`、`State`（关系）、`Error{scope:"llm"\|"tts"\|"asr"\|"vlm", fallback}`、`Pong{}`。附 to_json/from_json 双向；未知 type 返回 None+warning 不抛异常 | — |
| **M0.3** | `server/contracts/base.py`：`format_prompt_reminder()` 返回格式指令文本；`parse(raw_line)` 单行解析返回 `{emotion, text_zh, text_ja, tts_ok}`，**不持有跨行状态**。`dialog_v1.py`（唯一实现）：解析 `[e:情绪]中文\|日本語`；脏行修复——缺`\|`→整行当中文+`tts_ok=False`；未知表情→normal（对照 emotion_defs）；缺`[e:]`→补 normal；失败不抛异常只 warning。`factory.build_contract()` **硬绑定 dialog_v1**，注释标注未来 ID 绑定预留位（现在不写 dialog_json_v1、不做 settings 绑定段） | `test_output_contract.py`：正常行/缺竖线/未知表情/缺标签/空行全用例；闭集引用 emotion_defs 断言 |
| **M0.4** | `server/main.py`：FastAPI + `/ws` + `/metrics`（先返回占位结构）。uvicorn host 从 settings 读取但**非 127.0.0.1 时强制覆盖 + warning**。启动打印自检横幅（生效 provider 配置表，不含 key）。优雅关闭：SIGTERM 断开全部 WS | e2e：netstat 断言 9800 仅 127.0.0.1 |
| **M0.5** | `client/ws_client.py`：QThread 管理 WebSocket。10s 心跳；指数退避重连 1s→30s 封顶；断线上行消息（user_text/audio_*）进无界缓冲队列，重连后按序补发；**下行不缓冲不补发**；**连接被拒诊断**：ConnectionRefused（即时拒绝，非超时）连续 3 次 → 气泡提示“miyako 服务未能启动，请双击安全模式查看原因” → 停止重连；超时类维持无限退避。连接状态信号 connected/disconnected | `test_protocol_roundtrip.py`（消息往返+未知 type 容错+闭集拒绝非法值）、`test_ws_reconnect.py`（断线→缓冲→重连→上行补发顺序/下行不补发）、`test_ws_diagnostic.py`（mock 3 次 refused → 诊断信号且停止重连） |
**里程碑出口**：双终端 WS 收发各消息往返成功；pytest 全绿。`git tag m0-protocol`
---
### M1 假数据全链路（3 周，12 tasks，可分三批投喂）
> **上下文**：全 mock provider 跑通客户端全部 UI 手感。本里程碑落成两大 CI 边界（platform 层、presenter 层）与 voice_postprocess。**立绘资产用程序生成的占位 PNG（不同纯色/简单图形），不依赖美术。**
**批次 a：平台层与窗口骨架**
| Task | 内容与产出 | 验收 |
|---|---|---|
| **M1.1** | `client/platform/base.py`：`ForegroundInfo` dataclass + `PlatformProvider(Protocol)` 七方法：`acquire_single_instance_lock(name)→bool`、`get_foreground_window()→ForegroundInfo\|None`、`is_session_locked()→bool`、`get_last_input_seconds()→float`、`get_system_audio_peak()→float`（0~1，不可用返回 **-1**）、`register_hotkey(combo, callback)→bool`、`show_error_dialog(title,text)`。`win32.py`：ctypes/pycaw 全部集中——CreateMutexW/GetLastError、GetForegroundWindow/GetWindowTextW/GetWindowThreadProcessId+QueryFullProcessImageName、GetLastInputInfo、OpenInputDesktop、pycaw IAudioMeterInformation、RegisterHotKey+消息泵、MessageBoxW；**每个调用含失败降级语义（返回安全值不抛异常）**。`mock.py`：内存态可编程实现。`get_platform()` 工厂：运行时返回 Win32，测试注入 Mock | `test_platform_provider.py`：Mock 与 Win32 同一组契约用例；Win32 冒烟非 Windows skip；**import 约束执法**（约束 A） |
| **M1.2** | `client/pet_window.py`：无边框/置顶/Qt.Tool/WA_TranslucentBackground；30ms QTimer 采样鼠标处像素 alpha，<8 开 `WA_TransparentForMouseEvents`（逐像素穿透），拖拽期间强制关穿透；**单实例经 `get_platform().acquire_single_instance_lock("miyako_single_instance")`，失败气泡提示"miyako已经在运行啦～"后 sys.exit(0)** | `test_single_instance.py`（mock platform 注入断言）；手动：双击启动第二次被拦截 |
| **M1.3** | `client/sprite_manager.py`：状态机 `IDLE(呼吸4s正弦缩放+随机眨眼2~6s)→EMOTE(300ms)→TALKING(口型120ms轮换)→IDLE`；30min 空闲→dormant 趴桌；表情×服装×姿态三轴查 manifest，缺项逐级 fallback 至 normal；**manifest 从 `data/characters/{CHARACTER_PACKAGE_ID}/sprites/manifest.json` 读取，整体缺失时回退 `data/fallback/` 内置最小 manifest（生成 9 元情绪单色占位图）** | manifest 缺失仍正常显示占位图；三轴 fallback 正确 |
**批次 b：音频链路（mock）与表现层**
| Task | 内容与产出 | 验收 |
|---|---|---|
| **M1.4** | `client/presenter.py`：`StagePresenter(Protocol)` 五方法 `set_emotion/set_talking/play_motion/show_bubble/idle` + `SpritePresenter` 实现（包装 sprite_manager） | `test_stage_presenter.py`：接口行为断言 + **import 约束执法**（约束 B） |
| **M1.5** | `client/bubble.py`：句级打字机气泡 40ms/字 QTimer；新句到达旧句上移，最多保留 3 句；样式读 settings.yaml 的 `ui_theme.bubble` 节（2026-09 合一） | 手动验收 |
| **M1.6** | `client/audio_capture.py`：sounddevice 回调采集（16kHz/blocksize=512/PCM16/numpy），PyAudio 兜底，再不可用降级“禁用语音输入”+托盘提示；`audio_playback.py`：流式播放 pcm16 块，**write 前通过 Qt signal 发 far_frame（tee）**，write 后按 stream latency 更新 AEC delay，计算 RMS 发能量信号；`aec_base.py`（process_far/process_mic/close）+ `aec_none.py`（直通）；`aec_webrtc.py` 留 TODO（M+1.5 实现）；AEC extras 缺失时工厂自动选 aec_none 且 barge_in 强制 false+托盘提示 | `test_aec.py`：aec_none 直通输出==输入；RMS 计算正确；降级工厂选择正确 |
| **M1.7** | `server/providers/`：`llm_base/llm_mock`（**流式输出行式格式，覆盖全部 9 元情绪**，含 few-shot prompt 模板）、`tts_base/tts_mock`（生成 1kHz 正弦波 pcm16）、`asr_base/asr_mock`（返回固定句子）、`vlm_base/vlm_mock`、`factory.py`（读 settings，`build_llm(role)` 按 roles 路由，全 lazy import） | mock 链路可跑 |
**批次 c：orchestrator 流水线与收口**
| Task | 内容与产出 | 验收 |
|---|---|---|
| **M1.8** | `server/providers/voice_postprocess.py`：读 `miyako.char/tts_config.yaml`，**管线顺序固定**：`strip_actions → glossary → quote_guard 掩蔽 → replace_map → verbal_tics`。**双语分节**：replace_map/glossary 按 zh/ja 节，verbal_tics 中日后缀成对（zh:"……哼"/ja:"……ふん"，probability 0.3，**RNG 可注入，单测固定种子**）；quote_guard 对引语区（「」/""）与专有名词命中区间做正则掩蔽，替换跳过掩蔽区；strip_actions 剥 `（...）(...)*...*`。配置缺失/非法 → **原文直通不阻断，托盘 ⚠️ 一次（防刷屏）**。tts_config.yaml 示例：`replace_map: {zh: {"我":"人家"}, ja: {"私":"あたし","わたし":"あたし"}}`，`glossary: {zh: {"AI":"人工智能"}}`，`edge_voice: "ja-JP-NanamiNeural"` | `test_voice_postprocess.py`：**中日双语用例各≥1**、语境守卫（引语内“我”不被替换）、动作剥离、口癖概率（固定种子）、fail-open |
| **M1.9** | `server/orchestrator.py`：句级流水线——user_text → (mock 记忆占位) → llm 流式 → **行缓冲（行未完整缓存、换行触发，完整行交契约）** → `dialog_v1.parse()` → 每完整行：①立即发 Sentence（闭集校验源 emotion_defs）②**文本过 voice_postprocess（text_zh/text_ja 双侧）** ③asyncio.create_task 异步送 TTS（合成源=处理后的 text_ja）④TTS task 发 audio_begin(真实 sample_rate)→audio_chunk×N→audio_end；全部完成后 ReplyEnd；`self._tasks` 集合管理；收到 Interrupt cancel 全部；**emotion 槽合并调用占位**（reply_end 后一次调用返回 `{affection,trust,importance,open_question,reason}`，mock 返回固定值，失败静默）；LLM 失败 fallback：角色卡 tone_examples 随机一句 + Error{scope:"llm"} | mock 链路全流程跑通；/metrics 显示脏行率 |
| **M1.10** | `client/input_bar.py`（文本输入+麦克风按钮+假音频采集发送）、`client/tray.py`（菜单：显示/隐藏、静音、DND、设置占位、TTS降级级别提示、**「完全退出」= 结束 client + kill server 子进程**，server pid 由启动时环境变量或 pid 文件记录）、`run_client.py` 顶层崩溃防护（traceback 写 `%LOCALAPPDATA%\miyako\logs\crash-YYYYMMDD.log` 滚动 3 份 + `get_platform().show_error_dialog`） | `test_crash_guard.py`：人为抛异常→日志落盘+弹窗调用 |
| **M1.11** | 立绘占位资产生成脚本（tools/ 内，生成 9 情绪 × 简单图形 PNG 入 .char/sprites/ 并写 manifest）+ `data/fallback/` 最小 manifest + miyako.char 其余初始 yaml（character_card.yaml 含 tone_examples 全 9 情绪双语各 2 条、relationship.yaml 四分支占位、tts_config.yaml、manifest.yaml 含 sprite/live2d 双资产节） | 资产可加载 |
| **M1.12** | `test_latency_budget.py`：对 mock 链路断言各环节埋点存在且数值合理（埋点在 M1.9 中插入：asr/memory/llm_first_line/voice_postprocess/tts_first_chunk） | `/metrics` 各环节 p50/p95 字段存在 |
**里程碑出口（M1 手动清单 8 项全过）**：①拖拽跟手+透明区穿透+立绘区可点 ②假对话打字机气泡逐句上移 ③表情随 mock 输出切换（9 种全覆盖）④口型随假音频 RMS 开合 ⑤杀 server→托盘灰→重启自动重连 ⑥双击第二次→互斥量拦截 ⑦托盘完全退出→任务管理器无 python 残留 ⑧人为抛异常→错误框+crash 日志。两个 import 约束测试全绿。`git tag m1-mock-loop`
---
### M2 真模型接入（2 周，5 tasks）
> **上下文**：替换 mock 为真实 provider。BYOK 模式，OpenAI 兼容格式。角色卡注入 persona。
| Task | 内容与产出 | 验收 |
|---|---|---|
| **M2.1** | `server/persona.py`：从 `.char` 包读 character_card（人设/价值观/禁忌/tone_examples），组装 system prompt，**尾部追加 dialog_v1.format_prompt_reminder()** | prompt 含格式约束段 |
| **M2.2** | `server/providers/llm_api.py`：httpx 异步流式，OpenAI 兼容；`stream_options:{"include_usage":true}`，厂商报错不支持→去掉参数重试+`estimate_tokens()`（CJK 1 token/字、其余 len//4，M2.6 统一实现）+`estimated:true`；**按 role 路由**（factory.build_llm(role) 读 settings.llm.roles）；失败 fallback：tone_examples 随机+Error{scope:"llm"} | 填真实 key 真对话跑通，双语+表情正确 |
| **M2.3** | `server/providers/asr_whisper.py`：faster-whisper int8 CPU lazy import；失败发 Error{scope:"asr"} 文本置空 | 断网→“没听清” |
| **M2.4** | `server/providers/tts_edge.py`：edge-tts ja-JP-NanamiNeural 流式 MP3 分块（miniaudio 解码为 pcm），**AudioBegin 如实上报实际采样率（24k）**；异常→Error{scope:"tts"} 降级。工厂实现**reply 级锁定**：reply 开始时探活链（gptsovits 跳过→edge 5min 缓存探活→audiofile 跳过→mock），句间不切换；reply 中途失败才当场降级+托盘提示 | 无 GPU 机器有动态语音；探活缓存生效 |
| **M2.5** | `/metrics` 完善：分 role token 累计、单次对话平均成本、各环节延迟 p50/p95；settings_example 增补 .env.example 说明 | `test_latency_budget` 真链路埋点齐全 |
| **M2.6** | 【v1.8 追加】① WS token 鉴权：server 闸门（连接后 5s 内未收到合法 auth → 断开 4401，期间零业务消息）+ client 连接后首条 auth（读 `data/server.token`）+ `test_ws_auth` ② orchestrator 下行**单发送队列按 seq 全序**（TTS 并发合成、发送串行化）+ audio_playback `flush()`（interrupt 乐观打断）与跨 reply 采样率重建 + `test_audio_seq_order` ③ `estimate_tokens()` 统一估算器（llm_api 计量与 orchestrator 预算共用，禁第二实现） ④ verbal_tics reply 级节流（每 reply ≤1 次，固定规则不加配置） ⑤ run_server 启动 pid 自检（读 data/server.pid 探活，活进程静默 exit 0） | 未鉴权连接零业务消息；并发 TTS 下播放零交错；打断后已播音频立即停；重复双击不拉 server 冲突副本 |
**里程碑出口**：真对话跑通；分角色消耗可见；断网→tone_examples 兜底；M2.6 五项验收全过。**此后满足 MVP 提前出口条件**（若时间紧张可跳过 M+1/M+2 直接 M+3.5；**但已确定多角色时，M-CHA 必须在 M+1 之前执行**——M+1 记忆模块按每角色一库设计，顺序不可倒）。`git tag m2-real-models`
---
### M-CHA 角色插拔（v1.9 新增，1.5 周，5 tasks；M2 之后、M+1 之前，顺序不可倒）
> **上下文**：产品壳与角色解耦——用户已确认多角色需求（设计文档 A.3 v1.9 定案）。**三条形态铁律（违反=返工）**：① **单活角色**——应用级互斥量名保持 `miyako_single_instance` 不随角色变，两个角色包也不许同时拉两个 pet；② **重启式切换**——禁止运行时热切换；③ **每角色一库**——禁止 character_id SQL 过滤/跨角色查询，隔离只靠 db 文件粒度。产品级标识（日志目录 `%LOCALAPPDATA%\miyako`、bat 文件名、互斥量名、产品名）保持 miyako 不变，只有"角色展示名"数据化。
| Task | 内容与产出 | 验收 |
|---|---|---|
| **M-CHA.1** | 激活点：settings.yaml 增 `character.package_id`（缺省 `"miyako"`）；`resolve_character_package()` 定义于 `server/constants.py`（读 settings → 校验 `data/characters/{id}.char/manifest.yaml` 存在且 `schema_version` 已知 → 返回包路径；**包缺失/manifest 非法 → 回退 miyako.char + 启动横幅 ⚠️**）；`CHARACTER_PACKAGE_ID` 常量退役（全仓 grep 零残留引用）；persona/voice_postprocess/sprite_manager/relationship 的包路径全部改经此函数（client 侧 display_name 读取同样经它） | `test_character_package`：改一行 settings 指向第二测试包即切换数据源；激活包缺失回退 miyako + ⚠️ |
| **M-CHA.2** | 品牌去硬编码：tray tooltip/设置标题、pet_window 单实例气泡、run_client 崩溃弹窗、ws_client 诊断文案——全部改读激活包 `character_card.display_name`；产品级标识不动（产品名与角色名解耦） | 第二角色运行时托盘/弹窗显示新角色名；互斥量仍拦第二实例 |
| **M-CHA.3** | 每角色一库：store/relationship 接收 db 路径参数，orchestrator 装配时按激活角色打开 `data/memory/{package_id}.db`；**首启迁移**：存在旧 `data/memory.db` 且 `data/memory/` 为空 → 移动为 `data/memory/miyako.db`（幂等，store 初始化时执行）；.gitignore 与 make_release 排除清单加 `data/memory/` | `test_character_isolation`（freezegun）：角色 A 写入 → 切 B 检索为空、关系独立 → 切回 A 完整恢复；旧库迁移零丢失 |
| **M-CHA.4** | 切换编排：tray「切换角色」子菜单（枚举 `data/characters/*.char` 读 display_name，当前激活者打勾）→ 确认框（提示将打断当前对话并重启）→ interrupt → 等后台评估任务落库（≤3s 超时强制）→ 写 settings → 复用「完全退出」进程管理重启双端；**目标包校验失败 → 拒绝切换（不写 settings）+ 托盘 ⚠️** | `test_character_switch`：对话中切换不丢最后一轮状态；坏包拒绝切换；双击第二实例仍被拦 |
| **M-CHA.5** | 角色包工具与契约：`tools/new_character.py`（交互式脚手架：character_card 9 情绪占位 tone_examples / tts_config / relationship 四分支 / 占位 sprites / manifest）；manifest.yaml 增 `schema_version: 1`（加载校验，未知版本 ⚠️ 不阻断）；preflight 角色包检查改**逐包枚举**报告；**音色快路径（v1.9.2）：`--quick-voice` 可选步骤——列 edge-tts ja 音色池试听样例，用户对照短录音（≥5s）人工选最近似者写入 tts_config.edge_voice——无语料/未训练时的低配动态语音，与 GPT-SoVITS 自训深路径互补** | 一条命令产出可启动的新角色包（占位资产）；preflight 多包体检输出；快路径产出的 edge_voice 可被 tts_edge 直接消费 |
**里程碑出口**：两个测试角色包往返切换，各自记忆/关系/口癖/立绘/音色完整无损（`test_character_isolation` 全绿）；miyako 旧 memory.db 首启自动迁移零丢失。`git tag m-cha`
---
### M-CAM 摄像头感知（Tier C，2026-09-10 用户定案插入项；位于 M-CHA 之后、M+1 之前）
> **上下文**：用户在 Tier A/B/C 分层中选定 Tier C（持续视觉理解+情绪感知）。**硬约束**：默认关 + 首次开启显式同意（明确告知：画面将定期发送至用户配置的 VLM 云端、帧与观察内存态不落盘、可随时关闭）；DND 时**源头停发**；节流采样（interval_s 下限 15s）+ mini-gate（冷却/每日上限/reply 空闲，M+2.3 落地后换全量 gate）；摄像头指示灯亮属诚实行为。采集用 PySide6 QtMultimedia（**零新增依赖**，已验证）；VLM 走 `providers.vlm: openai`（OpenAI 兼容多模态，实测 glm-5.3-flash 现有端点可吃图）。否决清单核对：摄像头不在否决清单、无新增重依赖。
| Task | 内容与产出 | 验收 |
|---|---|---|
| **VIS.1** | 同意与开关：settings `camera:{enabled:false,interval_s:45,max_side:1024}` + `vision:` 节（观察 prompt/冷却分钟/每日上限）；托盘 UI 子菜单增「摄像头感知」勾选项（默认不勾，首次勾选弹隐私说明确认框） | 默认关；首开有同意框（**【v1.9.6】同意一次存档 camera.consent，重启免重复弹**；**启动时按配置恢复开启，失败托盘可见化**）；关=连采集都不发生 |
| **VIS.2** | `client/camera_capture.py`：QtMultimedia 默认设备抓帧（≤max_side JPEG；无设备/权限 → unavailable 降级不崩溃） | 离屏降级用例；JPEG 过尺寸钳制 |
| **VIS.3** | 协议：`CameraFrame{image}`（client→server，base64 JPEG）；main.py 分发 | 往返 roundtrip；未知 type 容错不变 |
| **VIS.4** | `server/providers/vlm_openai.py` + factory 路由（`providers.vlm: openai`）：OpenAI 兼容多模态 chat（image_url base64），独立 `vision:` 配置（base_url/model/api_key_env） | MockTransport 用例：payload 结构/鉴权/HTTP 错误→VlmError |
| **VIS.5** | `server/vision_observer.py`：节流采样→VLM 观察（≤40 字，输出 SKIP=无话可说）→ mini-gate（冷却/每日上限/orchestrator 空闲）→ orchestrator 以 trigger:"behavior" 搭话；观察与帧不落 memory.db | 冷却内不重复搭话/SKIP 静默/VLM 失败静默/reply 忙丢弃 |
| **VIS.6** | `client/camera_watcher.py`：enabled 且 ws 连通才采样；DND 源头停发 | DND 停发；断连不发 |
**里程碑出口**：同意流完整（默认关/首开确认/随时关）；一帧→VLM 观察→gate→主动搭话全链路可演示；无摄像头/DND/冷却/失败四条降级路径全绿；隐私断言（帧不落盘）在测试中执法。`git tag m-cam`

---
### M-VOICE 连续对话（v1.9.3 用户需求，2026-09-10；阶段一半双工）
| Task | 内容与产出 | 验收 |
|---|---|---|
| **VOICE.1** | `client/voice_vad.py`：能量断句状态机（阈值/尾点 700ms/最短 250ms/最长 15s 强制截断；参数可配，纯逻辑时间可注入）。**VAD 选型（2026-09-10 实测定案）**：三方案 CPU 均 <0.5% 单核不构成差异；Silero-ONNX 为主选（onnxruntime 已在，模型 2.3MB），能量+双档为零依赖兜底——v1 先实现能量兜底层，误触实测超标再升级 Silero | 合成序列单测：开始/尾点判定/过短丢弃/强制截断/时间注入确定性 |
| **VOICE.2** | 🎤 改「对话开关」：开=麦克风常开，VAD 断句自动上行（每句独立 AudioStart/Chunk/End）；**半双工静音门**——reply/TTS 播放期间客户端丢帧（无 AEC 防自听回环）；托盘状态可视；**【v1.9.6】开关状态持久化**（voice.autostart_mic 切换即写回，启动自动恢复，采集失败自带回弹） | 收发闭环冒烟：假麦克风 E2E（语音→MockASR→气泡新回复句）；【v1.9.6】写回往返/缺节创建 |
**里程碑出口**：免按钮连续对话可演示；barge-in 全双工留 M+1.5.2（AEC extras 实装后）。`git tag m-voice`（与 M+1.5 合并验证）

---
### M+1 记忆生命化（3 周，6 tasks，可分三批）
> **上下文**：SQLite 单文件。**v1.9：每角色一库 `data/memory/{package_id}.db`（M-CHA.3 已铺装配）**——所有 store/vector/dedup/summarizer 模块自第一天起按 db 路径参数化设计，禁止 character_id 查询逻辑（期权列仅作 forensic 标记）。禁止重型向量库。
| Task | 内容与产出 | 验收 |
|---|---|---|
| **M+1.1** | `server/memory/store.py`：SQLite WAL 模式；四类条目 dialogue/fact/diary/emotion_tone，每条含 importance/ts/content；**db 路径参数化——按激活角色打开（M-CHA.3）**；建表带 character_id 期权列（forensic 标记，无索引无过滤）；线程安全；**持久化写队列（v1.9.3 借鉴①）：后台评估等写库经每角色库旁 pending 队列（JSON 落盘/重启重放/成功出队，条目含 confidence-source-created_at）——进程崩溃不丢 AI Native 状态写回** | 建表 SQL 断言含期权列；并发写无死锁；两角色库互不可见；**队列重启重放与崩溃恢复单测** |
| **M+1.2** | `server/memory/vector.py`：sqlite-vec 向量表 + **三因子检索**（粗召回 4 倍→截 top-k；`0.55*sim+0.25*exp(-hours/(72*(0.5+importance)))+0.20*importance`（v1.8 半衰按 importance 加权 36h~144h）；`asyncio.wait_for` 200ms 超时返回空 context）；**enable_load_extension 失败→降级纯关键词（BM25/LIKE），接口签名不变**；接口无 character_id 参数 | `test_vector_fallback.py` 降级路径；`test_memory_decay.py`（**freezegun 固定时钟**断言 24h 前记忆排序符合衰减）；`test_memory_timeout.py` |
| **M+1.3** | emotion 槽合并调用实装（orchestrator 后台任务组）：reply_end 后一次调用出 `{affection:-3~3, trust:-3~3, importance:0~1, open_question:str\|null, reason:"≤20字"}`；解析失败**先一次修复重试（v1.9.3 借鉴⑤：原输出 append+升级措辞指令+deepcopy 不污染历史+上限 2 次）再静默跳过**（关系不变、importance=0.5、无钩子）；结果经**持久化写队列（借鉴①）**写入 fact 的 importance 与 hooks 表 | 评估失败静默单测；**修复重试成功路径单测（坏输出→修复→落库）** |
| **M+1.4** | `server/memory/deduplicator.py`：**独立模块**。每日后台收集近 3 天 fact，喂 correction 槽 LLM 比对，输出 `{merge:[...], delete:[...]}` 更新 SQLite。**与 summarizer 共用 store 的同一个“收集近 N 天 fact”查询方法** | `test_deduplicator.py`：merge/delete 正确执行；LLM 失败静默 |
| **M+1.5** | `server/memory/summarizer.py`：角色视角日记（diary 槽 glm-4-plus）：第一人称、≤200字、按 .char 人设语气、**禁止出现“用户”一词**（含则重新生成一次，再含则截断弃用；**重试遵循 v1.9.3 借鉴⑤后台修复重试语义**）；每日校对委托 deduplicator；**顺带产出 0~2 条 `[reflect]` 行为反思条目（v1.9.2：emotion_tone 类承载、content 前缀约定、不动 schema——例"[reflect] 本周三次在用户专注时段搭话均被冷淡，主动频率应下调"，产出后供 behavior gate 信号强度消费，见 M+2.3）** | 日记格式校验单测；反思条目格式（前缀/条数上限）单测 |
| **M+1.6** | orchestrator 上下文预算：`budget={"persona":0.30,"memory":0.35,"history":0.25,"user":0.10}`（**预算单位 = estimate_tokens() 估算 token，M2.6 同源 + v1.9.3 借鉴③信封化**），memory 槽超预算按三因子综合分从低砍尾巴；**prompt 组装用 Section 冻结树（v1.9.3 借鉴②：priority 排序/同级 id 唯一/超预算按 priority 砍整段/用户文本节点永不 format 展开）**；检索结果带时间衰减标注（“三天前你说过…”）注入 prompt；**阈值历史压缩（v1.9.3 借鉴④：阈值触发→目标占比→保最近 N 条→摘要限预算→「历史对话总结」标记，参数 clamp 不变式）**；`server/providers/tts_audiofile.py`（③级：按情绪匹配 reference_audio/fallback/*.wav，文件缺失降级④；**情绪→音频绑定建单一规范化视图（v1.9.3 借鉴⑥）供 prompt 目录与运行时查表共用**） | `test_tts_fallback_chain.py`：mock 注入四级依次失败，断言①→②→③→④逐级切换+各级 Error+AudioBegin.sample_rate 正确+**每级前过 voice_postprocess**；**Section 树与压缩语义单测** |
**里程碑出口（AI Native 循环 CI 全绿）**：
```python
# tests/test_native_loop.py
async def test_every_dialogue_leaves_trace():
    with frozen_clock():
        await orch.handle_user_text("我最近特别想吃橘子")
        assert store.has_new_row() or relationship.changed() or hooks.pending()
        ctx2 = await memory.retrieve("我想吃水果")
        assert any("橘子" in m.content for m in ctx2)
```
`git tag m1-memory`
---
### M+1.5 AEC 与外放打断（1.5 周，3 tasks，可与 M+1 并行）
| Task | 内容与产出 | 验收 |
|---|---|---|
| **M+1.5.1** | `client/aec_webrtc.py`（webrtc 绑定，Linux/macOS）+ `client/aec_speex.py`（**v1.9.4 Windows 缺省：pyaec/Rust Speex AEC**，官方 win_amd64 轮子，合成信号实测 33.9dB）：统一契约——封装 APM（lazy import）；far/near 按 **10ms 帧对齐**（32k 下 320 样本）喂 APM，内部帧缓冲攒够才处理；`process_mic` 前 `set_stream_delay_ms`（初值 120ms，播放端更新）；`process_far`（播放线程）与 `process_mic`（采集线程）并发用**一把 threading.Lock 包住 APM 入口**；工厂 `audio.aec: speex|webrtc|none` 可插拔（extras 按 `sys_platform != 'win32'` 标记） | 合成信号：近端=远端+延时叠加 → 残差能量下降 **>10dB**（speex 实测 33.9dB） |
| **M+1.5.2** | barge-in 双档实装（audio_capture）：AEC 就绪——残差>0.03 持续 400ms 发 Interrupt；直通——仅非播放期，>0.06 持续 600ms；**实机阈值 = max(表值, 2×rms_floor)**：首启进入语音模式采集 3s 环境噪声基线 rms_floor，标定失败退表值（v1.8）；**发送 Interrupt 的同时客户端本地 flush 播放队列（乐观打断，v1.8）**；settings.yaml `barge_in` 开关，AEC extras 缺失 runtime 强制 false+托盘提示 | 外放假音频时正常说话触发打断（orchestrator cancel 全部挂起任务）；安静环境 60s 零误触发 |
| **M+1.5.3** | 系统音频防御：播放期经 `get_platform().get_system_audio_peak()` 监测系统混音峰值，减去自己输出流贡献后仍显著→判定他人在放音频→临时退回“播放期全丢弃”静音门；**返回 -1（不可用）同样退静音门** | `test_system_audio_defense.py`、`test_aec_fallback.py`（卸载 extras→直通+全丢弃+强制 false+托盘提示不崩溃） |
`git tag m1-aec`
---
### M+2 关系与分寸（2 周，4 tasks）
> **上下文**：behavior gate 是“不打扰用户”原则的执行体。**前台窗口信息由客户端经 state 消息上报，服务端零系统调用**；无上报数据 → gate 按不该说话处理（fail-closed）。
| Task | 内容与产出 | 验收 |
|---|---|---|
| **M+2.1** | `server/relationship.py`：SQLite 单表 `dimensions(character_id, affection, trust, updated_at)`（期权列无过滤；**表随角色库 data/memory/{package_id}.db 走，M-CHA.3**）；读 emotion 槽合并调用增量写库；**数值变化经下行消息 `rel_state{affection,trust}` 通知客户端（v1.8 改名，勿复用 state）**；映射 `.char` 包 relationship.yaml 分支（0-20/20-50/50-80/80+，各带 tone_hint）注入 persona；失败静默跳过 | 一周模拟互动后 tone_hint 在 prompt 可观测 |
| **M+2.2** | **客户端 state 上报**：定时（变更触发+最小间隔 5s 节流）经 `get_foreground_window()`/`is_session_locked()` 采集，发 `{type:"state", foreground, locked}`；服务端消费存内存态（仅本地回环、不落库）；foreground 为 null（获取失败）gate fail-closed | state 消息往返单测 |
| **M+2.3** | `server/behavior.py`：五类信号源（时段每日1次/记忆钩子8h冷却/手动截屏30s/闲置嗅探4h默认关/未回扣话题2h）；统一过 `_should_speak(signal)` gate：DND→全屏白名单（消费 state.foreground 匹配 config 白名单）→冷却→信号强度→emotion 槽 LLM 时机判定；**信号强度环节召回 `[reflect]` 反思条目调节档位（v1.9.2：近期"搭话被冷淡"的反思降低主动强度，反之调高，作为时机判定 LLM 的上下文）**；**gate LLM 失败按“不该说话”处理**；主动搭话发 reply_begin{trigger:"behavior"}；hooks 表带期权列 | `test_gate_fail_closed.py`（gate 抛异常→不搭话；无 state 数据→不搭话）；全屏白名单应用零搭话；未回扣话题冷却后追问一次，回扣后不再追问；反思条目降低信号强度的单测 |
| **M+2.4** | sprite_manager 增加 motion 扩展（Sentence.motion 触发 EMOTE）；settings_window.py 基础版（Providers 探活按钮+行为开关页+DND 设置） | dormant 趴桌姿态触发正常 |
`git tag m2-behavior`
---
### M+3 视觉与配置（2.5 周，5 tasks）
| Task | 内容与产出 | 验收 |
|---|---|---|
| **M+3.1** | `client/screen_capture.py` 嗅探状态机：`ACTIVE ──输入空闲≥3min（get_last_input_seconds()）──→ IDLE_WATCHING`（60s 一次 160px 灰度均值 hash，纯本地零上传）；hash 显著变化置 dirty；**锁屏（is_session_locked()）清 dirty**；回座瞬间截全帧；手动热键（register_hotkey，默认 Ctrl+Shift+M，失败提示+设置窗可改键）直接截全帧。默认 `behavior.idle_sniff.enabled: false` | 状态机迁移单测（mock platform） |
| **M+3.2** | `client/ocr_filter.py`：RapidOCR lazy import，numpy→[(text,conf)]；同 hash 缓存不重复跑；未装 extras 闲置嗅探自动禁用、手动热键直接走 VLM 路 | `test_ocr_fallback.py` |
| **M+3.3** | `server/screen_router.py` 双路路由：①hash 对比无实质变化→丢弃 ②OCR 文本匹配规则→命中且文本充分（置信度均值>0.8 且有效字符>50）→**纯文本送 chat 槽 VLM 跳过**；命中但需语境→图像送 VLM；未命中→静默丢弃 ③VLM 超时 2s/失败→跳过屏幕上下文 ④结果→话题钩子→behavior gate→搭话。**嗅探生成内容全部内存态用完即弃不写 memory.db；全量过 gate+独立 4h 冷却** | `test_screen_router.py`：文本路命中断言 VLM 未调用（mock 计数器）/图像路命中/未命中丢弃/VLM 超时 四用例 |
| **M+3.4** | settings.yaml `screen_triggers.rules` 规则引擎实装（配置驱动，2026-09 合一；原独立 screen_triggers.yaml）：`rules: [{name: error, pattern: '(错误\|Error\|エラー)[\s:]*0x[0-9A-Fa-f]+\|\d{4,6}', route: text, priority: high}, {name: victory, pattern: '(VICTORY\|通关\|勝利\|GAME CLEAR\|クリア)', route: image, priority: high}, {name: code, pattern: '(Traceback\|Exception\|FAILED\|エラー発生)', route: text, priority: mid}, {name: video, pattern: '(字幕\|第[一二三\d]+集\|EP\d+)', route: text, priority: low, cooldown: 8h}]` | 规则加载与匹配单测 |
| **M+3.5** | settings_window.py 完整版：Providers 探活页（逐个按钮+状态灯）、行为开关页（闲置嗅探白话说明）、关系数值页（只读+**一键重置记忆与关系**——二次确认，供用户大幅调教人设后清除旧人格锚定）、记忆管理页（表格列出 fact/diary，查看全文、删除单条、**编辑单条 fact（v1.9.2 用户校对——缓解模型复读与认知漂移，保存后立即生效于下轮检索）**、重置） | 删除一条记忆后下轮检索不到；**编辑一条 fact 后下轮检索到新内容**；重置后旧记忆全不可检索 |
**里程碑出口**：放置电脑 3 分钟留存报错窗口，返回后 AI 主动关心且走纯文本路（metrics 验证 VLM 未调用）；视频全屏不触发；`.char` pack/unpack（tools/gen_persona_derivatives.py）往返无损。`git tag m3-vision`
---
### M+3.5 发布准备（1 周，4 tasks）
| Task | 内容与产出 | 验收 |
|---|---|---|
| **M+3.5.1** | `scripts/make_release.py`：①生成 zip（剔除 tests/tools/**docs**；**排除清单强制校验：memory.db（含 `data/memory/` 整目录）/settings.yaml/.env/.venv/`data/server.pid`/`data/server.token` 任一出现在包内立即报错终止**）②bat 全部校验 CRLF+UTF-8 无 BOM ③**zip 全部条目 `zf.flag_bits \|= 0x800`**（UTF-8 语言标志位，中文文件名不乱码）④从 VERSION 命名 `miyako_v{ver}.zip` + release_notes.md 模板 | 三区域设置（zh-CN/en-US/ja-JP）解压文件名完好 |
| **M+3.5.2** | `scripts/check_req_hash.py`：requirements.txt+pyproject.toml 的 sha256 与 `.venv/.req_hash` 比对，不符退出码 1；install.bat 安装成功后写入 hash（M-1.5 已引用，此处实装闭环） | 改动 requirements 后启动被拦截 |
| **M+3.5.3** | 应用内检查更新：settings.yaml 配 UPDATE_URL；托盘“检查更新”→httpx 拉 VERSION 对比→有新版弹窗提示下载 zip+指引“覆盖解压（data 文件夹勿删）→重跑 install.bat”；**README 附“数据外发透明度声明”（v1.9.2：收集/不收集对照表——本体零遥测，唯一外发面 = 此处 GET VERSION 文本，不含任何用户数据；文档模式借鉴 N.E.K.O）** | mock UPDATE_URL 走通；透明度声明进 README 走查清单 |
| **M+3.5.4** | e2e 测试 `tests/e2e/test_release.py`：①解压 zip→检查无排除清单文件+中文名完好 ②模拟 install→启动→netstat 断言 9800 仅 127.0.0.1 ③升级模拟：预置旧 memory.db+settings.yaml→覆盖解压→**断言两文件未被改动**。人工 checklist 写 RELEASE_CHECKLIST.md：clean VM 三连击≥3 次；重复双击→互斥量拦截；**人为占用 9800→客户端诊断提示出现**；升级覆盖+重跑 install 依赖补齐数据保留；延迟基线≤2.5s；README 走查 | e2e 全绿；checklist 全勾 |
| **M+3.5.5** | 【v1.9】CI 流水线：GitHub Actions windows-latest，装 base 依赖跑 pytest 全量（含两条 import 约束执法、test_ws_auth、test_character_isolation）；extras 作业允许失败不阻断 | push/PR 触发全绿；README 徽章 |
**里程碑出口**：`git tag v0.7.0` + 发布。**MVP 提前出口路径**：若 M+1/M+2 被跳过，M+3.5 验收中记忆/关系相关项替换为”降级矩阵对应项验证”（检索占位、gate 仅 DND+冷却判定）。
---
### M-PLAY 陪玩吐槽（2026-09-12 用户需求立项；识屏三模式设计之模式2，模式3 ambient 仅留设计缓实施）

> 设计定案（2026-09-12 讨论）：识屏子系统升级为三触发域——①闲置嗅探（现状不动）②陪玩域（本里程碑）③桌面 ambient（留设计：本地攒工作记忆、搭对话 prompt 便车注入、零新增调用，实施时按 §2 本节后续 task 立项）。用户定案：本期只做模式2；token 成本敏感（否决翻译管线/中文直出，见 PROGRESS 2026-09-12「保持原案」条）。
>
> **状态：✅ 已实装（2026-09-12，47a176c + 真机反馈 8b35e1a）**——PLAY.1~5 全部完成。真机反馈补丁：判据放宽（最大化=覆盖工作区亦计，浏览器看视频免 F11）、名单非法条目/域接管托盘提示、缺类型改按非法处理（不再静默默认 game，防丢防剧透语义）。后续预项：media_mode lively（边刷边聊，冷却 5~10min+上限）。
>
> **后续预项（2026-09-12 用户定案缓做）**：`media_mode: quiet | lively`——quiet（现行默认）下有声媒体一律「播放中不打扰，静默窗口才说」；lively 下有声媒体（刷短视频等连续流）也允许**低频场景切换触发**（视频切换事件 + 冷却拉长 5~10min + 会话上限），实现「边刷边聊」。已知取舍：短视频内容不可预知，吐槽可能踩空；调用频率上升数倍。另注：音频判别枚举的是 pycaw **渲染（输出）会话**，麦克风输入属捕获会话不在此列——用户说话不会被判为「进程有声」；网页语音会议对方声音归属会议进程（判为播放中→安静），语义正确。

| Task | 内容与产出 | 验收 |
|---|---|---|
| **PLAY.1** | 协议 `ScreenShot.source` 增 `"companion"`（加法扩展，proto_ver 不变）；platform 层新增 `is_foreground_fullscreen()`（窗口矩形≈显示器矩形**或工作区矩形**[最大化亦计，2026-09-12 用户定案放宽]，容差数 px）与 `get_audio_active_processes()`（pycaw 音频会话枚举「正在出声的进程名」；**返回 None=不可判定**（pycaw 缺失/无设备），调用方按保守语义处理；mock 同步） | 三平台实现齐；mock 可注入；真实机器冒烟不炸 |
| **PLAY.2** | `client/screen_capture.py` 陪玩域：名单 `进程名:类型`（game/video/web）；接管优先级 陪玩域>闲置嗅探（挂起不取消）；game=场景切换触发（hash 大变化后稳定≥2s 才发，滤瞬时噪声）；video=音频静默≥8s 窗口触发（有声期间永不发，防剧透）；**web=按该进程音频状态运行时路由**（有声→video 语义/无声→game 语义；音频不可判定→保守按有声）；独立冷却+session 计数（离开名单超 session_gap 会话重置） | 名单外活跃期零上传（执法用例）；game/video/web 三语义各一用例；陪玩期间闲置嗅探零触发 |
| **PLAY.3** | `server/screen_router.py` 陪玩分支：source=="companion" → 独立冷却（默认 3min，**不动闲置识屏 4h 冷却**）→ hash 去重 → VLM **一次调用两用**（提示词「值得说就输出吐槽，否则 SKIP」，走 build_vlm 工厂——本地接口即插拔纪律，管线禁止直连 httpx）→ `（你们正一起看这个画面：…）请就此自然地吐槽或说一句话` → behavior 全量 gate 照过；**gate 拒不吞陪玩冷却额度**；观察与上下文不落盘（零 store 依赖既律） | SKIP 静默；冷却拦截；gate 拒后冷却未消耗；闲置识屏 4h 冷却不受影响 |
| **PLAY.4** | 配置+设置面：`screen_triggers.companion` 节（enabled/apps/sample_interval_s/scene_stable_s/cooldown_min/max_per_session/session_gap_min/video_silence_s）；行为页增「陪玩模式」组（总开关+名单编辑器[进程名:类型 逗号分隔]+冷却+上限）；README 透明度与同意措辞（web 类明示「全屏网页内容随截图发往 VLM」） | 参数表 vs example 互锁测试过；名单空=不工作 |
| **PLAY.5** | 测试全套：两触发器语义/web 双语义路由/音频不可判定保守/冷却与 session 上限/非名单零上传执法/DND 源头停/路由分支与 SKIP/gate 拒不吞额度 | 全绿 + 全量回归 |

**里程碑出口**：名单内全屏游戏场景切换 → miyako 吐槽一句（气泡+自训音色）；浏览器全屏看番只在暂停/ED 说话、全屏看网页在翻页时说话；非名单应用全程零识屏上行。`git tag m-play`
---
### M-SPRITE 立绘资源全量利用（2026-09-12 追认立项；✅ 已实装——并行会话产出，本节为文档同步）

> 背景：104 张三轴立绘（A/B/C 朝向 × 4 服装 × 9 情绪 + D 背影）实际只有 A_casual 服役——`image_key(emotion, outfit, pose)` 三轴接口早已预留但无调用方传参。

| Task | 内容 | 状态 |
|---|---|---|
| SPR.1 | `tools/compose_all_sprites.py` 全量合成器：原始差分层配方枚举（sinfo 服装 6 段含裸×2 排除出包），179 张归档/128 张入包（原 104 全覆盖+24 新差分）；闭集外差分 wry/puzzled/serious/flustered/smile 入 `expressions_extra`；sad-A/sleepy-B 原生缺层维持 fallback | ✅ 637b639 |
| SPR.2 | 状态机重构：情绪持久化（IDLE 不再回退 normal——瞬态演出是频闪根因）/EMOTE 限真 motion frames/化妆死代码删除（breathe/blink/mouth_index 无素材且渲染端从未消费）；换装 5 选含裸装 + 朝向 auto 跟随气泡（阈值 150px 纯函数）/手动锁定；托盘「立绘」菜单与 ui_theme 外观持久化 | ✅ 84c5963/d994ba1 |
| SPR.3 | 饰品开关移除（实测无实际作用；manifest accessories 数据段留档备将来重做） | ✅ 4716c34 |

**出口（已验）**：情绪驻留可见（不再闪回 normal）；托盘换装/朝向即点即换；全量素材入 manifest 且缺层 fallback 链无损。
---
### M-TAVERN 酒馆卡适配（2026-09-18 用户需求立项；✅ 已实装，本节为文档同步）

> 背景：SillyTavern 角色卡（chara_card_v1/v2/v3；.json 直读或 .png 内嵌文本块）导入 miyako 成为可激活角色包。三轮用户定案：①**不动原卡适配**——与「一键配置角色包」平行的独立模式，card_importer/factory 等共享链路零改动（仅复用 scan/import/wire 既有函数）；②**恒定借音色**——导入时无条件从当前激活包拷参考音频池（import_card 的 reference_donor），GptsovitsTts 候选池兜底接管，酒馆角色直接用 miyako 音色走既有 TTS 链路与降级矩阵；③**默认 TTS 接线**——卡无配套权重（酒馆卡恒如此）时自动把服务接线到 `data/tts/` 默认权重（SoVITS/GPT 各取字典序第一个；复用 wire_tts_service：custom 块改指向+权重拷入+当日首备份；目标=settings `providers.tts_service_yaml`；条件缺失跳过不阻塞、失败保卡走 wiring_error）。

| 项 | 内容 |
|---|---|
| 转换（client/tavern_card.py，纯逻辑） | .png 提取 tEXt/iTXt/zTXt 三种块、关键字 ccv3 优先 chara；v1 顶层/v2/v3 data 归一（data 优先）；宏替换 {{user}}→用户、{{char}}→角色名（大小写不敏感、双括号先于单括号）；转包五件套=character_card（persona←description+scenario+system_prompt+开场白+对话示例[子节标题钉格式契约]、behavior_rules←post_history_instructions 逐条剥序号、默认产品级 taboos 两条）+中性 relationship 四分支+fail-open tts_config（无 gptsovits 段，借池由导入方拷）+manifest（tavern 溯源；world book 不导入如实注明）+sprites 九情绪统一映射（取图优先序：art_path 自选→PNG 卡面→json 同名 .png 兄弟→data/fallback 占位九张；自选按原扩展名存 card.{ext}） |
| 导入（client/tavern_import_dialog.py） | 转换预览（scan 同一道门）→ import_card（回滚保护/激活校验/参考池补拷全继承）→ 默认 TTS 接线 → 可选激活写回 → 重启生效；立绘「选择…/用卡面」即改即重转换；拷贝走 QThread 不冻 UI；失败可见性纪律与原卡对话框一致 |
| 链尾 mock 双形态（server/providers/tts_mock.py + factory.py） | 见 §0.4 速查表行——配置音色级时降级落点静音纯气泡，出厂 mock 配置有声 |

**出口（已验）**：Vivian from zzz 实卡（v2 JSON+PNG 卡面）转换+导入+借池+默认权重接线全链通过；GptsovitsTts 补位 ref/prompt+真实合成 32kHz OK。测试：test_tavern_card 15 用例（读取三块型/ccv3 优先/归一/宏/转包映射/立绘覆盖/权重发现）+ test_tavern_import_dialog 7 用例（转换接管/解析失败上报/借池导入激活/立绘往返/关闭守卫/接线三态）。

### M-TOOL 工具模式（2026-10-05 用户需求立项；✅ 已实装，本节为文档同步）

> 背景：参考传统桌宠「实用工具融合」品类，增加番茄钟/闹钟/倒计时/喝水·久坐提醒/整点报时/节日问候（任务栏迷你窗[CPU/内存/时间/天气]经讨论后用户定案砍除——连带零新外发面、platform 零改动、零新依赖）。全部产品语义经对话定案（九项，见 §0.4 速查表行），核心关切是**与主陪伴模式零回归**：守卫式同漏斗 + 域隔离 + 总闸 + 既有测试阵三道防线。

| 项 | 内容 |
|---|---|
| 协议（server/protocol.py） | 上行 `ToolEvent{kind, detail}`——kind 8 元闭集（pomodoro_work_end/pomodoro_break_end/alarm/countdown/water/sedentary/hourly/festival，ToolKind Literal）+ REGISTRY 入册；`ReplyBegin.trigger` Literal 增 `"tool"`；wire type 17→18（test_protocol_roundtrip 计数钉子已同步） |
| 编排（server/tool_announcer.py） | kind→触发上下文模板表（触发文本作为 user_text 注入，台词由 LLM 按 persona 生成——与 behavior 固定台词同款机制）；`submit` 占线入队（cap 2 丢最旧）/闲时 `orch.submit_tool`；drain 挂 `on_reply_done` 钩子——ReplyEnd 后 call_soon 延迟补播（钩子在 reply 任务体内同步调用，此刻 is_busy 仍 True，须等任务返回）；main.py create_app 装配 + WS isinstance 分支 |
| orchestrator 守卫（server/orchestrator.py） | `trigger=="tool"` 四处守卫：①不检索记忆（省 200ms 预算）②不进 `_history`（防工具痕迹泄漏进后续闲聊 prompt）③对话原文不落库 ④后台槽（emotion/关系/写回）全跳；`_compose_system(include_memory=False)`=persona+tone_hint 两段（防记忆泄漏进播报词）；新增可选 `on_reply_done` 回调（异常不影响 reply）；`submit_tool` 忙碌返 False 交 ToolAnnouncer 排队 |
| 定时器域（client/toolbox.py，零 Qt） | PomodoroSession（work→break 自动循环，挂起恢复只补一声）/AlarmStore（"HH:MM"=每日[条目+日期去重]，"YYYY-MM-DD HH:MM"=一次性[触发即失效并写回、过期启动清理、迟到>120s 静默丢弃、错过不补触发]）/Countdown（一次性内存态）/PeriodicReminders（人在座才计时[get_last_input_seconds<active_idle_s，平台不可用按在座]，离开顺延回座计满整段）/HourlyChime（整点翻转+免打扰段[quiet_start~quiet_end 跨午夜]，免打扰整点同样消耗防复活连发）/FestivalChecker（公历 7 固定节日+农历六大查表 2026~2035[新华社/香港天文台对照/多源万年历交叉核对，2026-10 核实]+自定义 "MM-DD 名称"[全角标点容错]；表外年份优雅跳过）；`Toolbox.tick()` 每秒返回事件列表+emit 回调；时钟 `clock: Callable[[], datetime]` 注入可测 |
| 接线（run_client.py） | 播报路由定案实现：`llm.provider=="api"` 且 WS 在线→ToolEvent 上行；mock/断线→`LOCAL_FALLBACK_LINES` 本地气泡；闹钟写回=`_rewrite_key_at_path`+`_yaml_scalar`（flow 列表）；1s QTimer（QTimer(window) 父子随窗）；tray 注入 toolbox/on_tool_persist；晚绑定 on_countdown_request/on_alarms_dialog；comp 增 toolbox/tool_timer |
| 托盘+对话框+设置窗 | 托盘「工具」动态子菜单（aboutToShow 现读现建）：总闸 ✓/番茄钟 开始·停止[进行中显示剩余分钟]/倒计时…[QInputDialog 分钟]/闹钟管理…/喝水·久坐·报时·节日四开关[实时改 Toolbox 内存态+行级写回 settings]；client/alarm_dialog.py（列表增删+格式提示，挂 tray 保活防 GC 闪退）；settings_window「工具模式（重启生效）」参数组 12 项 |

**出口（已验）**：全量 **860 passed, 2 skipped**（含 GUI 冒烟真实装配）。测试：test_tool_announcer 16 用例（模板全 kind/占线排队补播[trigger 序 user→tool]/cap 丢最旧/**四跳过断言**[store 零新增/retriever 零调用/历史零增长/无 RelState]+user 轮对照组证明装配有效/on_reply_done 逐轮触发）+ test_toolbox 19 用例（相位循环/闹钟每日去重与一次性三态/非法条目/过期清理/倒计时/健康提醒间隔与顺延/平台不可用按在座/免打扰段/节日表抽查[含 2031 中秋国庆双节合并]/时段门/自定义解析/总闸短路/同 tick 多事件/闹钟写回回调）。
---

## §3 收尾流程（每 task / 每里程碑结束时执行）

> **v1.9.7 增量（2026-09-15）**：①情绪持久化的配套修复——休眠唤醒回归（set_emotion 移出 EMOTE 后回复路径失去隐式唤醒口+presenter.idle 死接线；修复=set_emotion 即唤醒+拖拽接线）；②**立绘显示高度上限取消**（base_height 原 hi=560 → 9999 等效无上限，用户定案）；③隐私组进设置窗（摄像头感知/麦克风自启实时开关，首开同意框随迁，托盘摄像头勾选移除；camera_watcher.set_enabled(False) 返回语义修复=「关了又开」根因）+ 陪玩名单全角标点容错与非法条目托盘提示。

### 3.1 每 task 收尾
1. `pytest tests/ -x -q` 相关单测全绿；
2. 更新 PROGRESS.md（task 编号移入“已完成”）；
3. 分组 commit：`feat:`/`fix:`/`test:` 分开，约定式格式；
4. **依赖清单收尾铁律**：本会话若动了 `requirements.txt` / `pyproject.toml`，提交前必须执行 `python scripts/check_req_hash.py --write` 刷新 `.venv/.req_hash`（或重跑 install.bat）——2026-09 教训：改清单的会话没刷 hash，两天后启动链被依赖校验拦截（PySide6 钉版事件）。
### 3.2 每里程碑收尾
1. `pytest tests/` **全量**跑一遍，结果贴出，有失败先修复；
2. 对照该里程碑“里程碑出口”逐项自检，标注 ✅/❌/未实现——❌ 和未实现项必须修复或明确说明原因后经用户确认才能过；
3. 检查两条 import 约束测试（test_platform_provider / test_stage_presenter）在通过列表中；
4. **文档同步检查**：确认本文件头部“基于 miyako_v1.9”与用户侧设计文档版本一致；设计文档若有修订，diff 变更点并更新本文件 §0.4 速查表、§4 摘录与对应 task，保证两文档不分叉；
5. `git tag {里程碑tag}`；
6. 更新 PROGRESS.md 里程碑状态 + 遗留 TODO + 下一步建议；
7. 向用户汇报，等待确认后进入下一里程碑。
### 3.3 遇到设计冲突时的处理顺序
① §0.4 速查表 → ② §0.2 原则 → ③ §4 摘录 → ④ 请求用户提供设计文档对应章节 → ⑤ 用户确认为准。**禁止自行引入本文件未规定的第三方重依赖**（新依赖必须说明理由并经用户确认）。
---
## §4 设计要点摘录（自足补丁：降级矩阵 / 设计理由 / 否决清单 / 点播协议）
### 4.1 完整降级矩阵（实现任何失败路径前必读，与 task 验收对照）
| 失败点 | 服务端行为 | 客户端表现 | 单测 | 落点 |
|---|---|---|---|---|
| LLM API 失败 | `tone_examples` 随机一句 + `Error{scope:"llm"}` | 气泡 fallback 台词 | `test_orchestrator_llm_fail` | M2.2 |
| TTS 音色级（gptsovits/cloud_clone）探活失败 | reply 级锁定至 mock（纯气泡） | 本轮文字照常，托盘提示 | `test_tts_auto_lockdown` | M2.4/2026-09 收敛 |
| TTS 音色级中途失效 | 当场降 mock：剩余句纯气泡 | Error{scope:tts} 提示 | `test_tts_fallback_chain` | M+1.6/收敛后 |
| voice_postprocess 失败/配置缺失 | **原文直通送 TTS，不阻断流水线（fail-open）** | 托盘 ⚠️ 一次（防刷屏） | `test_voice_postprocess_fail_open` | M1.8 |
| dialog_v1 单行解析异常 | 行缓冲续走（orchestrator）、脏行降级语义不变 | 无感知（脏行率升高，/metrics 可见） | `test_output_contract` | M0.3 |
| ASR 失败 | `Error{scope:"asr"}`，文本置空 | 前端「没听清」 | `test_asr_fail` | M2.3 |
| AEC 缺失/初始化失败 | —（客户端逻辑） | 直通 + 播放期全丢弃 + barge_in 强制 false + 托盘提示 | `test_aec_fallback` | M1.6/M+1.5 |
| 系统其他应用在放音频 | —（客户端逻辑） | 临时退回播放期静音门，barge-in 暂不可用 | `test_system_audio_defense` | M+1.5.3 |
| **音频峰值监测不可用（platform 返回 -1）** | —（客户端逻辑） | **同上（退静音门，保守处理）** | `test_platform_provider` | M1.1 |
| OCR 缺失/崩溃 | 禁用闲置嗅探 | 手动热键直接走 VLM 路 | `test_ocr_fallback` | M+3.2 |
| OCR 命中且文本充分 | **纯文本 LLM，VLM 不调用** | 搭话照常（近零成本） | `test_screen_router_text` | M+3.3 |
| OCR 命中但需画面语境 | 走 VLM 路 | 搭话照常 | `test_screen_router_image` | M+3.3 |
| VLM 超时>2s / 失败 | 跳过屏幕上下文，继续 LLM | 识屏静默忽略 | `test_vlm_timeout` | M+3.3 |
| 记忆检索超时>200ms | 返回空 context | 无感知 | `test_memory_timeout` | M+1.2 |
| sqlite-vec 装载失败 | 检索降级为纯关键词 | 无感知（排序质量下降） | `test_vector_fallback` | M+1.2 |
| tts_config.yaml 缺失/损坏 | voice_postprocess 原文直通 | 无感知（指纹兜底失效），preflight ⚠️ | `test_voice_postprocess_fail_open` | M1.8 |
| sprites manifest 全缺 | — | 回退内置最小 manifest（data/fallback/），立绘为占位图 | `test_sprite_fallback` | M1.3 |
| 关系评估失败 | 静默跳过，数值不变 | 无感知 | `test_relationship_fail` | M+2.1 |
| gate LLM 失败 / 无前台窗口上报 | 按“不该说话”处理（fail-closed） | 静默不打扰 | `test_gate_fail_closed` | M+2.3 |
| WS 连接被拒（服务未启动） | server 未运行 | **连续 3 次拒绝且距进程启动 > 30s（豁免窗内只退避不诊断，v1.8）→ 气泡提示安全模式排障 → 停止重连** | `test_ws_diagnostic` | M0.5/M2.6 |
| WS 未鉴权连接（无 token / 5s 超时） | 断开（close 4401），零业务消息 | 无感知（攻击者/误连进程静默失败） | `test_ws_auth` | M2.6 |
| WS 断线（超时类） | client 缓冲输入（仅上行补发，下行不补发） | 托盘灰图标，指数退避重连补发 | `test_ws_reconnect` | M0.5 |
| 脏行/未知表情 | 契约解析器降级不抛异常 | 立绘 fallback normal | `test_output_contract` | M0.3 |
| client 未捕获异常 | server 端口仍在，等待重连 | 错误弹窗（经 platform）+ crash 日志落盘 + 退出 | `test_crash_guard` | M1.10 |
| 全局热键注册失败 | — | 提示热键不可用，设置窗可改键；识屏退化为设置窗按钮触发 | `test_platform_provider` | M+3.1 |
| extras 安装失败（install.bat 期间） | 不阻断安装 | preflight 如实标 ⚠️，功能按降级矩阵走 | — | M-1.5 |
| 用户重复双击启动脚本 | server pid 自检：活进程静默 exit 0（M2.6，不拉冲突副本） | 命名互斥量拦截，气泡提示 | `test_single_instance` | M1.2/M2.6 |
| 激活角色包缺失/manifest 非法（启动时） | 回退内置 miyako.char + 启动横幅 ⚠️ | 托盘 ⚠️ 角色回退提示 | `test_character_package` | M-CHA.1 |
| 切换目标包缺失/校验失败 | 拒绝切换（不写 settings），当前角色不受影响 | 确认框报错 + 托盘 ⚠️ | `test_character_switch` | M-CHA.4 |
| 切换时 reply 进行中 | interrupt 后等后台评估落库（≤3s 超时强制）再重启 | 气泡「稍等，把这句记完…」 | `test_character_switch` | M-CHA.4 |
### 4.2 关键设计理由（一句话版——用于正确外推未写明的边缘情况）
| 设计 | 一句话理由（外推时以此为准） |
|---|---|
| 拒绝 Electron/Tauri/Web 栈 | 原始 PCM 与 AEC 残差必须在 Python 手里，浏览器音频管线是黑盒（原则 9） |
| far_frame tee 方案 | write() 阻塞返回时刻≈音频入硬件缓冲，天然对齐，避免环形缓冲时间对齐复杂度 |
| 命名互斥量而非端口探测 | netstat 无法区分“miyako 已运行”与“端口被占”，可能误判的检查不如不做 |
| 断线下行不补发 | 陪伴场景下静默丢弃优于错乱重放，协议不做 seq 级 ACK |
| 误打断防御优先 | AEC 参考信号只含 miyako 的 TTS，别人的声音它消不掉——误打断比少一次打断伤害大 |
| gate fail-closed | “不打扰”是不可逆体验承诺，宁可沉默不可误触 |
| character_id 仅期权列 | schema 自文档化锁住唯一低成本期权，多角色机制本身是投机通用化 |
| Contract 单实现硬绑定 | 收益是可测试性与 prompt 干净；多契约抽象是期权不是现在的目标 |
| voice_postprocess fail-open | 机械修正层挂了不应该影响核心对话链路，指纹兜底失效可接受 |
| 降级链句间不切换 | 音色撕裂比音质下降更破坏人格一致性 |
| 平台抽象是测试接缝 | 不是跨平台承诺；为无窗口单测提供 Mock 注入点 |
| 每角色一库（非 SQL 过滤） | 隔离靠 db 文件粒度：查询零改动、备份/重置天然按角色；character_id 过滤是白付的复杂度 |
| 切换=重启式（非热切换） | 90% 价值 30% 成本；热切换需拆 orchestrator/TTS 锁/句柄重建，边界情况不成比例 |
| 互斥量保持应用级命名 | 单活保证：两个角色包也不能同时拉起两个 pet 抢 9800 |
### 4.3 技术栈否决清单（AI 禁止自行建议引入，除非触发条件成立且用户确认）
| 被否决项 | 翻案触发条件 |
|---|---|
| Electron / Tauri / Web 前端栈 | 无（用户明确要求 Live2D 级表现力时重评） |
| Live2D | 用户反馈明确要求 → 经 StagePresenter 接口升级，届时单独立里程碑 |
| C# WPF | 项目重启为纯 Windows 深度集成形态 |
| Flutter / Unity / Godot | 无 |
| PyInstaller 打包 | bat 分发路线被用户数据证伪 |
| 多宠同屏并存 / 同窗群聊 / 运行时热切换 / character_id SQL 过滤 | 无（"单活插拔"形态已于 v1.9 M-CHA 落地，这些是其永久边界） |
| dialog_json_v1 弱模型契约 | 经 Ollama 接入的弱模型行式格式教不会时 |
| 识屏规则自动调优 | 规则数 >50 且误判数据可积累 |
| 重型向量库 / paddleocr / ffmpeg 依赖 | 无 |
### 4.4 设计文档点播协议
本文件已自足。**仅以下情形**，向用户请求设计文档 `miyako_v1.9.3.md` 的**对应章节**（只贴章节，不贴全文）：
1. 实现某失败路径时对 §4.1 某行语义有疑问 → 请求「§九 降级矩阵」原文；
2. 对 §4.2 理由外推拿不准 → 请求对应章节原文（§5.3 音频 / §6.x 服务端 / §7.x 客户端 / 附录 A 决议记录）；
3. 遇到本文件未覆盖的全新设计问题 → 按 §3.3 顺序处理，第④步请求用户粘贴相关章节。
**请求格式**：「请提供 miyako_v1.9.3.md 的 §X.X 章节，问题：{一句话描述}」。
---
## 附录：测试文件总清单（对照检查）
| 测试文件 | 所属里程碑 | 守护点 |
|---|---|---|
| test_protocol_roundtrip.py | M0 | 消息往返+容错+闭集 |
| test_output_contract.py | M0 | 契约解析+脏行修复+行缓冲 |
| test_platform_provider.py | M1 | 平台层契约+**import 约束 A** |
| test_stage_presenter.py | M1 | 接口行为+**import 约束 B** |
| test_single_instance.py | M1 | 互斥量（经 mock platform） |
| test_aec.py | M1/M+1.5 | 直通/RMS/10dB/降级 |
| test_voice_postprocess.py | M1 | 双语替换/守卫/种子/fail-open |
| test_latency_budget.py | M1/M2 | SLA 埋点 |
| test_crash_guard.py | M1 | 崩溃防护 |
| test_ws_reconnect.py | M0 | 断线缓冲语义 |
| test_ws_diagnostic.py | M0 | 连接被拒诊断（含 v1.8 豁免窗） |
| test_ws_auth.py | M2 | token 鉴权闸门（4401/零业务消息） |
| test_audio_seq_order.py | M2 | 下行 seq 全序 + 采样率重建 + interrupt flush |
| test_character_package.py | M-CHA | 激活点/包解析/缺失回退 |
| test_character_isolation.py | M-CHA | 无损切换 CI 断言（freezegun） |
| test_character_switch.py | M-CHA | 切换编排/坏包拒绝/旧库迁移 |
| test_tts_fallback_chain.py | M+1 | 四级链逐级切换 |
| test_vector_fallback.py | M+1 | sqlite-vec 降级 |
| test_memory_decay.py | M+1 | 固定时钟衰减排序 |
| test_memory_timeout.py | M+1 | 检索超时返回空 |
| test_deduplicator.py | M+1 | 矛盾清洗 |
| test_native_loop.py | M+1 | **AI Native 铁律 CI 断言** |
| test_system_audio_defense.py | M+1.5 | 混音峰值防御 |
| test_gate_fail_closed.py | M+2 | gate 失败不搭话 |
| test_screen_router.py | M+3 | 双路路由+VLM 计数 |
| test_ocr_fallback.py | M+3 | OCR 降级 |
| test_sprite_fallback.py | M1 | manifest 缺失→内置兜底 |
| tests/e2e/test_release.py | M+3.5 | 发布三用例 |
---
> **给 AI 的最后一句话**：本计划的核心价值在“降级矩阵永远可达”（§4.1）和“两条 import 边界永不被突破”（§0.3）。当你为图省事想直接 `import ctypes` 或在 UI 里操作 sprite 时，回头读一遍原则 9 和 §0.3——那两行约束就是这个项目区别于普通聊天前端的原因。开工吧。
