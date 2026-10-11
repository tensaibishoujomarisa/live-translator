# 变更日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.3] - 2026-10-12

这一版修的都是**打开发行版才会碰到、而且一碰就废**的问题。

### 修复

- **发行版一启动就崩（最严重）**：`device: auto` 先把 device 设成 `cuda`
  算出 `compute_type = float16`，接着发现没有 CUDA 运行库又把 device 改成
  `cpu`，但 `compute_type` **没跟着改** —— 于是 CPU + float16，
  ctranslate2 直接拒绝：

  ```
  ValueError: Requested float16 compute type, but the target device
              or backend do not support efficient float16 computation.
  ```

  崩在 `ensure_loaded` 里，用户看到的现象是「软件起来了但没有字幕」。
  现在 device 每次变化都重新算 compute_type（`_pick_compute_type`），
  并且 CPU 上会拒绝纯 float16 / float32（`int8_float16` 合法，保留）。

- **Electron 硬编码默认模型，导致离线包偷偷下载 1.5GB**：
  `ui/main.js` 里写着 `settings.asrModel = 'large-v3-turbo'`，
  而 `buildArgs()` 把它作为 `--model` 传下去 —— **命令行参数会覆盖包内
  config.yaml**。后果：发行版 config 写的是 `small`、包里也只带了 `small`，
  后端却去 HuggingFace 下载 1.5GB（实测下到 613MB 中断，在包里留下一个
  `.incomplete` 垃圾文件，用户看到的是「启动卡住」）。
  现在默认不传 `--model` / `--device`，以 config.yaml 为准
  （发行版是 small，开发机是 large-v3-turbo，两边都对）。

- **`--list-devices` 那条 spawn 自己拼了一份 env**，漏掉 `HF_HUB_OFFLINE`，
  于是打开设置面板拉设备列表时照样联网找模型。统一走 `childEnv()`。

- **`HF_HUB_CACHE` 语义用错了**（原 `HUGGINGFACE_HUB_CACHE`）：
  缓存根目录应该由 `HF_HOME`（→ `$HF_HOME/hub`）表达。

- 模型已在本地缓存时自动设 `HF_HUB_OFFLINE`；但**只在目录内容完整时**才认
  （`models--*/snapshots/*/model.bin` 存在），只有 `blobs/*.incomplete`
  不算。而且万一离线标记导致加载失败（缓存目录看着有其实不全），
  会撤销标记重试一次，不会把「本来能下载」变成硬失败。

### 新增

- `--force-cpu` 参数：强制 `device=cpu`，用来在**有显卡的机器上**复现
  无显卡机器的行为（这次的崩溃就是因为本机有 GPU 一直测不出来）
- `scripts/test_compute_type.py`（15 项）：锁死 device/compute_type 组合规则
- `scripts/test_offline_guard.py`（11 项）：缓存识别、不完整下载不误判、
  离线标记的置位与回退

## [0.2.2] - 2026-10-12

### 新增

- **术语批量导入**：`python -m backend.main --import-terms terms_xxx.draft.yaml`
  - 给某个特定主播加术语时，手工往 `glossary.yaml` 里贴很容易把 YAML 缩进写坏，
    现在一条命令搞定
  - **只追加、不重写**：`Glossary.append_entries()` 只往文件末尾加行，
    用户手写的注释、分组、`alt` 列表一个字节都不动
    （原来的 `write_entries` 会整份重写，注释全丢）
  - 已存在的同名条目会被跳过而不是覆盖，并自动备份 `.bak`
- `scripts/check_draft_terms.py`：导入前检查草案格式、报冲突、在副本上演练效果
- `terms_hinome.draft.yaml`：火羽ひのめ（B站日V）的候选术语（姓名 + 直播用语）。
  她的个人梗没有收录 —— 没有可靠来源，不编。

## [0.2.1] - 2026-10-12

### 修复

- **术语表写入根本不生效**（上一版只是个假承诺）：界面提示说「会建议加进术语表」，
  但程序从来不写 `glossary.yaml`，那个提示永远不会变成实际效果。
  现在勾「这是专有名词」会**直接写入**，并自动备份 `glossary.yaml.bak`。
- **`promote_terms` 依赖实例上的 `glossary.path`**，某些构造路径下它是 `None`，
  导致功能静默失效（报「术语表未启用或没有路径」）。改为以配置为准。
- **`NullTranslator` 没接住 `learning` / `glossary`**：没配 API Key 时工厂返回它，
  于是「只显示日文」模式下点 ✏ 记修正会全部失败。
- **`learn` / `status` 命令把结果 print 到 stdout**，污染 JSONL 事件协议 ——
  按行解析的客户端（字幕窗、SSE 转发）会直接解析失败。现在只走事件流。
- **`_handle_command` 引用了不存在的 `bridge`**，`learn` 抛 `NameError` 被
  `except` 吞掉，表现为「点了没反应」，日志里只有一行 warning。
- Electron 未传 `--learning-path` / `--glossary-path`，发行版会把用户数据写到别处。

### 性能

- **术语表 prompt 每次翻译都重算**：37 条术语约 600 字符，每句都拼一遍，
  一小时直播白烧几十万 token。现在预计算并缓存（术语表更新时失效）。

### 新增

- `--glossary-path` 参数（测试必须传，否则会改到用户真实的术语表）
- 设置面板「把候选写进术语表」按钮
- `scripts/test_glossary_write.py`（35 项）：写入/幂等/覆盖/YAML 特殊字符/
  自动提升/过长跳过/缓存失效
- UI 测试补 5 项（术语表按钮与结果展示），共 69 项

### 测试隔离

端到端测试发的是 `scope=term`，会触发自动写术语表 ——
之前没隔离路径，**测试真的往用户的 `glossary.yaml` 里写过条目**。
现在传临时路径，并断言真实文件的字节内容未被改动。

## [0.2.0] - 2026-10-12

### 新增：越用越准

字幕翻错时，鼠标移到那条字幕上点 **✏**，直接改中文并「记住这条」。
之后遇到同类句子，翻译会自动参考你的改法。

- **不是训练模型权重，而是把修正变成翻译时的上下文**（`backend/learning.py`）。
  这是刻意的取舍：微调需要**正确的**训练数据，而系统自己跑出来的字幕不是正确答案 ——
  拿它训练等于「用自己错误的输出去训练自己」，只会固化错误。
- 在线 LLM：修正作为少样本示例注入 prompt（`【用户以前的修正】（必须遵守）`）
- 本地 NMT：读不了 prompt，改为对译文做替换（只替换你明确改过的错译）
- 专有名词/人名建议提升为术语表条目（术语表是强制对照，优先级最高）
- 相关度挑选：按「包含关系」或「连续 4 字以上重合」筛选，
  不相关的修正不会被硬塞进 prompt
- 同一句日语改多次只保留最后一次，避免积累互相矛盾的版本
- 修正存在 `data/corrections.jsonl`，可直接编辑或删除

### 修复

- **`learn` / `status` 命令把结果 print 到 stdout**，而 stdout 是 JSONL 事件协议的通道，
  混进非 JSON 行会让按行解析的客户端（字幕窗、SSE 转发）直接解析失败。
  现在只走事件流。
- **`_handle_command` 里引用了未定义的 `bridge`**，导致 `learn` 抛 NameError
  被 except 吞掉、事件从未发出（表现为「点了没反应」）。
- Electron 未传 `--learning-path`，发行版会把修正记录写到项目之外的位置。
- `CorrectionStore` 的去重键原本是「原文+修正」，会让同一句的多个版本共存；
  改成只按原文去重。

### 验证

`scripts/test_learning.py`（25 项，库层：存储/相关度/prompt 注入/后处理替换/术语建议）
+ `scripts/test_learn_e2e.py`（15 项，协议层：真起后端进程走 stdin 命令，
并断言 stdout 每一行都是合法 JSON）。

## [0.1.2] - 2026-10-12

### 新增

- **便携发行版**：解压双击 `_start.bat` 即可用，不需要装 Python、不需要联网下载模型。
  CPU 版约 1.2GB（zip），自带识别模型（`small`）、本地翻译模型、VAD 模型。
- `scripts/build_env.py`：建干净自包含的依赖环境（用 **CPU 版 torch** 502MB
  替代 CUDA 版 4.2GB —— 本地翻译本来就跑 CPU）
- `scripts/build_release.py`：组装发行版，支持 `--slim` / `--bundle-asr` / `--with-whisper`
- `scripts/verify_bundle.py`：用发行版自带的 Python 验证识别/翻译/VAD/音频真的能用
- `scripts/bench_cpu_asr.py`：CPU 识别基准测试，用来选发行版该带哪个模型
- `scripts/test_bundle_env.py`：验证依赖环境（不只 import 成功，而是真跑一句翻译）
- 设置里可配「可见行数」「回看行数」；`asr.device` 支持 `auto`

### 修复

- **`asr.device` 为 cuda 但缺 CUDA 运行库时，模型能加载却在第一次推理时崩**
  （`Library cublas64_12.dll is not found`），表现为完全没有字幕。
  现在：加载前先探测 CUDA 可用性（不可用直接选 CPU），
  加载后还会用一小段静音试推理兜底，确保不把故障留到音频线程里。
- **Electron 没把 `HF_HOME` 传给后端子进程**，导致「离线版」仍去 HuggingFace 解析模型，
  启动从 1.5 秒变成 61.5 秒。现在 Electron 自己设置模型缓存路径，
  且模型已在包内时设 `HF_HUB_OFFLINE=1`。
- **`Config` 里的路径是绝对路径**，打包后 config.yaml 带着打包机的路径，
  换台机器就找不到模型。新增 `Config.get_path()`，相对路径按项目根解析
  （顺带修好了「从别的目录启动找不到模型」）。
- **`create_vad(models_dir="...")` 传字符串会抛 TypeError**（`str / str`），
  被内部 except 吞掉后**静默降级成 energy VAD**，断句明显变碎却不报错。
- **`silero-vad` 从未进 requirements.txt**（开发环境偶然装了），
  干净环境下 VAD 必然降级。现在列为运行期依赖，降级时用 ERROR + 界面提示。
- 便携版运行时组装：只拷 `python.exe` + site-packages 会报 `No pyvenv.cfg file`；
  `python._pth` 存在时 `DLLs` 不再自动加入搜索路径，导致 `_socket`/`_ctypes` 全部 import 失败。
  现在拷完整基础解释器目录，并把 `DLLs` 显式写进 `._pth`。
- 生成的 `_start.bat` 里 Python 健全性检查被放在 `exit /b 0` 之后，成了死代码。

### 实测数据

CPU 识别基准（i5-13600KF，20 线程，int8）：

| 模型 | 实时率 | 结论 |
|---|---|---|
| `base` | x0.102 | 可实时，但日语错误较多 |
| `small` | **x0.311** | **发行版默认**（能实时且识别基本正确） |
| `medium` | x0.878 | 勉强，连续讲话会积压 |
| `large-v3-turbo` | x1.237 | **比说话还慢，CPU 上不可用** |

GPU（RTX 4070 Ti）上 `large-v3-turbo` 是 x0.05，比 CPU 快约 25 倍 ——
有显卡就用显卡版 / `device: auto`。

## [0.1.1] - 2026-10-12

### 新增

- **字幕可滚动回看历史**：鼠标移到字幕上用滚轮往上翻就能看之前的翻译，
  默认保留最近 300 条（`subtitle.history_lines` 可配）。
  往上翻时字幕不会自动跳到最新，右下角出现「↓ 最新」按钮，点它或滚回底部恢复跟随；
  窗口可拉大以便一次看更多。
- 设置面板新增「可见行数」与「回看行数」两个滑块
- 窗口高度按所见行数自动计算，不再固定 240px

### 修复

- **多句译文互相覆盖**：一段识别结果被切成多句分别翻译时，每条都带同一个 segment id，
  前端按 id 找行是「覆盖」而不是「追加」，导致观众只能看到最后一句
  （日文完整、中文只剩后半截）。现在后端累积同一段的多句译文并逐句发出，
  前端也改成追加语义，两种来源都能正确拼接。
- **重复定稿**：同一段音频偶尔会被送进识别两次，屏幕上同一句话连着出两条
  （日文和中文各自重复一遍）。现在用「文本相同 + 音频时长精确到 50ms 一致 +
  2.5 秒内」判为重复并丢弃。不用「文本相同」单独判定，是因为主播真的
  重复说同一句话时，两次的音频时长不可能精确到毫秒级相同。
- **滚动容器用了 `justify-content: flex-end`**：这个组合会让超出顶部的内容
  滚不到（`scrollHeight` 甚至等于 `clientHeight`）。改用 `margin-top: auto` 推底。
- **字幕区的 `-webkit-app-region: drag` 吃掉了滚轮事件**，导致无法滚动回看。
  拖窗口改成只抓顶部控制栏。

## [0.1.0] - 2026-10-11

首个可用版本。目标场景：看日语直播（主播语速接近日常日本人语速、比影视剧略快）时，
屏幕底部实时显示「日语原文 + 中文译文」双语字幕。

### 识别（本地，完全离线）

- faster-whisper + large-v3-turbo，GPU float16。实测 7 秒音频约 300ms，实时率 **x0.05**
- **两段式字幕**：讲话过程中每 550ms 用贪心解码重跑一次当前句，先显示灰色未定稿日文；
  句末静音 380ms 后用 beam search 定稿
- **VAD 切句**用 Silero VAD，不是能量检测。注意 Silero 要求每帧前拼上一帧末尾 64 个采样当
  上下文（模型实际吃 576 点），不拼的话输出恒为 ~0.001，VAD 等于完全失效
- **五道幻觉闸门**：`condition_on_previous_text=False`、VAD 前置确认、三个置信度阈值
  （`no_speech_prob` / `avg_logprob` / `compression_ratio`）、重复模式检测、口癖黑名单
- 连续讲话超过 `max_speech_s`（默认 11s）强制在自然停顿处切段，避免字幕无限等
- 无 GPU 时自动回退 CPU/int8

### 翻译（可切换）

- `llm`：任何 OpenAI 兼容接口（DeepSeek / OpenAI / 硅基流动 / 本地 Ollama）。
  实测真实 DeepSeek 延迟 550~800ms
- `local`：MarianMT 日→中，CPU 约 130ms/句，免费离线，**不占显存**
- `off`：只看日文
- **滚动上下文**：把最近 4 句「日→中」对照一起给模型，日语省略主语时指代才不乱
- **术语表**：`glossary.yaml` 里的词强制使用指定译法。LLM 走 prompt 约束，本地 NMT 走
  后处理替换（含平假名/片假名互换写法、以及 `alt` 字段列的音译变体）
- **译文清理**：剥代码块、剥「翻译：」前缀、丢弃日文原文回显
- **熔断**：连续失败 4 次后停止翻译并提示一次可读原因（Key 无效 / 限流 / 服务故障），
  不会每句话刷一条错误

### 字幕窗（Electron）

- 透明、无边框、可置顶、可鼠标穿透（`Ctrl+Shift+M`），贴屏幕底部
- 双语显示，中文实心、未定稿日文半透明
- 设置面板：采集设备、音量增益、静音断句、语音判定阈值、识别模型、翻译方式与 Key、
  上下文字句数、字号/透明度/双语开关，面板顶部显示**当前实际在跑什么**
- 关掉不需重启（断句/阈值/上下文走热更新），换模型/换翻译后端才重启

### 音频采集

- WASAPI loopback 采集系统声音。优先 PyAudioWPatch（自带 loopback 补丁），
  sounddevice 仅在其 PortAudio 支持 `WasapiSettings(loopback=True)` 时可用
- 默认设备按**名字**匹配（默认输出的索引属于 MME 空间，loopback 端点属于 WASAPI 空间，
  拿数字比必然错位）

### 工程

- 本地 SSE 事件流（`/events`），供第三方或 OBS 使用；新连接会**补发粘性状态**
  （ready/status），断线重连也不会卡在「正在启动」
- CLI：`--doctor` / `--list-devices` / `--probe-audio` / `--test-asr` / `--pretty` / `--serve`
- 测试：mock LLM 链路（44 项）、多句译文累积、SSE 补发、整机验收、UI DOM 渲染（36 项）、
  Electron 冒烟、发布前洁净室检查

### 已知限制

- 只有 Windows（依赖 WASAPI loopback）
- 只翻日语（`asr.language` 固定 ja）
- 不翻译弹幕，只处理直播音轨
- UI 设置里的 API Key 是明文存在 `ui-settings.json`，也会出现在进程命令行里；
  建议改用环境变量 `JP_LIVE_TRANSLATOR_API_KEY`
- 本地 NMT 质量有限（网络梗和省略句容易翻飞），追求观感请用 LLM

### 踩过的坑（留档，避免重复踩）

- **CT2 转换会毁模型**：社区微调的 OPUS-MT 转成 CTranslate2 后解码器不收敛，
  输出「红红红红…」。同一份权重用 transformers 原样加载正常，所以本地 NMT 默认走 transformers
- **cublas64_12.dll 找不到**：GPU 版 torch 不提供 CTranslate2 需要的 CUDA 12 运行库，
  需要单独装 `nvidia-cublas-cu12` / `nvidia-cudnn-cu12`，并在导入前把 DLL 目录加进搜索路径
- **`.bat` 里不能有中文**：`chcp 65001` 生效前 cmd 按系统代码页解析，字节错位会把行拼起来；
  `set /p` 的提示文本里不能有 `[]`、`,`、`?`；`echo` 行里 `"%VAR%"` 的引号会被当命令；
  UTF-8 BOM 会让 `@echo off` 解析失败
- **测试要避开真实实例**：用户可能正开着字幕程序占着 8765，测试若用固定端口会连到旧进程上，
  现象是「ready 永远收不到」，极难排查。现在测试用随机端口并校验 `/debug/sticky` 存在
