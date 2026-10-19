# 日语直播实时翻译字幕

看日语直播时，在屏幕底部悬浮一行**「日文原文 + 中文译文」**双语字幕。
识别在本机跑（Whisper large-v3-turbo + GPU），**看直播时不需要联网、不需要代理**；
中文翻译可以选在线大模型（质量最好）或本地 NMT（完全离线、免费）。

针对**主播语速接近日常日本人语速、比影视剧还快**这个场景专门做了优化，见下面「快速口语是怎么处理的」。

```
日  クレヨンしんちゃんは何?あ、子供時代の思い出みたいな感じだった。
中  蜡笔小新是什么？啊，就是那种童年回忆的感觉。
```

| | |
|---|---|
| **识别** | faster-whisper large-v3-turbo / CUDA，实测 7 秒音频 300ms（**x0.05 实时率**） |
| **字幕** | 边说边出灰色未定稿日文 → 句末静音 380ms 定稿 → 100~300ms 后出中文 |
| **翻译** | 在线 LLM（DeepSeek 实测 550~800ms）／本地 NMT（CPU 130ms/句，免费离线）可切换 |
| **平台** | Windows（依赖 WASAPI loopback 采集系统声音） |
| **不需要** | 不需要声卡回环线、不需要虚拟声卡、不需要联网、不需要代理 |

主要特性：

- **两段式字幕**：不等整句讲完，边讲边出未定稿日文，句末定稿后送翻译
- **五道幻觉闸门**：Whisper 在静音/音乐段会自信地吐出「ご視聴ありがとうございました」并无限循环，
  这里用 VAD 前置 + 三个置信度阈值 + 重复检测 + 口癖黑名单挡掉
- **越用越准**：鼠标移到字幕上点 ✏ 就能改译文，改过之后它会记住，
  以后遇到同类句子自动参考你的改法
- **术语表**：人名/梗强制使用你指定的译法（`ぺこら` → `佩克拉`）
- **滚动上下文**：翻每一句时带上最近 4 句对照，日语省略主语时指代才不乱
- **可回看历史**：滚轮往上翻看之前的翻译，不会自动跳回最新
- **熔断**：Key 失效/限流时停止翻译并提示一次，不会每句话刷一条错误
- **鼠标穿透**：字幕窗可以设成穿透，直接点下面的直播间

---

## 一、快速开始

**这台机器上依赖和模型都已经装好了**，直接跳到第 3 步：

```
双击  03_启动字幕.bat
```

第一次启动约等 10 秒（加载识别模型 + 定位音频设备），然后屏幕底部会出现一条半透明的字幕栏。
**把直播打开让它出声**，字幕就会自动出现。翻译当前用的是**本地模型**（免费、离线、不需要 Key）。

如果换了机器、或删过 `.venv` / `models`，按顺序补齐：

| 步骤 | 文件 | 说明 |
|---|---|---|
| 1 | `01_安装依赖.bat` | 建 Python 虚拟环境 + 装依赖（一次性，约 5 分钟） |
| 2 | `02_下载模型.bat` | 下载识别模型（一次性，约 1.6GB，**只有这步需要联网**） |
| 3 | `03_启动字幕.bat` | 启动字幕窗，之后每次看直播都点这个 |
| 4 | `05_自检.bat` | 出问题时先跑这个，能定位 90% 的毛病 |

还有两个辅助脚本：

- `04_终端调试.bat` —— 纯文字模式，字幕直接打在命令行里，用来确认采集+识别有没有正常工作
- `06_回环测试.bat` —— 从扬声器放一段日语示例，验证「系统声音→识别」整条链路

**字幕窗的用法**：窗口默认贴屏幕底部、置顶、半透明。鼠标移上去顶部会出现一条控制栏。

| 快捷键 | 作用 |
|---|---|
| `Ctrl+Shift+H` | 显示 / 隐藏窗口 |
| `Ctrl+Shift+P` | 暂停 / 恢复识别（接电话时用） |
| `Ctrl+Shift+M` | **鼠标穿透**：开启后鼠标点击会穿透字幕窗，能直接点下面的直播间 |
| `Ctrl+Shift+↑ / ↓` | 字号增大 / 减小 |

**回看之前的翻译**：鼠标移到字幕上，用滚轮往上翻就能看历史（默认保留最近 300 条，
可在设置里改）。往上翻的时候字幕**不会**自动跳到最新，右下角会出现「↓ 最新」按钮，
点它或者滚回底部就恢复实时跟随。想看更多历史就把窗口拉大（拖右下角边缘）。

---

## 二、配置中文翻译

日文识别是纯本地的，中文翻译需要你选一个方式。

### 方式 A：在线大模型（推荐，质量最好）

编辑 `config.yaml`：

```yaml
translate:
  provider: llm
  llm:
    base_url: https://api.deepseek.com   # 任何 OpenAI 兼容接口
    model: deepseek-chat
```

然后提供 API Key（**推荐用环境变量，不要写进文件**）：

```bat
setx JP_LIVE_TRANSLATOR_API_KEY "sk-你的key"
```

也可以直接在字幕窗的 ⚙ 设置里填。Key 只存在本机的 `ui-settings.json`。

常见的几组填法：

| 服务 | base_url | model |
|---|---|---|
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5:7b` |

Ollama 这行意味着：**连翻译也可以完全离线**，只要你有能跑 7B 的显存。

### 方式 B：本地 NMT（免费、离线）

```bat
.venv\Scripts\python.exe -m scripts.setup_local_mt
```

然后把 `config.yaml` 里的 `translate.provider` 改成 `local`。
模型 78M 参数，约 130ms/句，**跑在 CPU 上所以不跟识别抢显存**。
质量不如大模型（`初見さん` 可能被翻成「第一次见面」），但胜在免费、断网可用、零延迟抖动。

> 实现说明：走的是 `transformers` 加载 MarianMT，**不是** CTranslate2。
> 因为实测社区微调的 OPUS-MT 转成 CT2 之后解码器会退化（输出「红红红红…」），
> 而同一份权重用 transformers 原样加载就没问题。只有当你手上有官方 CT2 模型时，
> 才把 `translate.local.backend` 改成 `ct2`。

### 方式 C：只看日文

`provider: off`。

---

## 三、术语表：让人名和梗翻译对

`glossary.yaml` 里的词会被**强制**使用你指定的中文写法。这是提升观感最省力的一招：

```yaml
entries:
  - ja: ぺこら
    zh: 佩克拉
    note: 主播名，不要意译
  - ja: スパチャ
    zh: SC（打赏）
```

- 用 LLM 翻译时，术语表作为 prompt 约束注入（最有效）
- 用本地 NMT 时，作为译文后处理替换
- 平假名/片假名写法差异会自动兼容（`ぺこら` 和 `ペコラ` 都认）

建议把你常看的主播名字、组合名、口头禅都加进去，效果立竿见影。

---

## 四、快速口语是怎么处理的

主播用日常语速说话时，常见的三个失败模式是：**字幕等半天不出来**、**出来一堆重复的鬼话**、**句子被切得七零八落**。对应的处理：

**1. 边说边出字幕（两段式）**
不是等一句话讲完才显示。VAD 检测到开始说话后，每隔 `550ms` 就把「这句到目前为止的音频」重新识别一次，先显示出**灰色未定稿**的日文；等这句话真正结束（静音超过 `min_silence_ms`）再用 beam search 出**定稿**结果，然后送翻译。
长句不会被无限拖下去：连续讲话超过 `max_speech_s`（默认 11 秒）就强制在自然停顿处切一段先出字幕。

**2. 幻觉抑制（Whisper 最大的坑）**
Whisper 在静音或纯音乐段会自信地吐出「ご視聴ありがとうございました」并无限循环。这里用五道闸门挡：
- `condition_on_previous_text=False` —— 从根上断掉自回归重复
- Silero VAD 先确认**真的有语音**才送识别
- `no_speech_prob` / `avg_logprob` / `compression_ratio` 三个置信度阈值
- 重复模式检测（同一小段重复 3 次以上直接丢）
- 口癖黑名单（可在配置里自己加）

被丢掉的片段会以 `asr_drop` 事件上报，`log.level: debug` 时能看到丢的原因。

**3. 句子不切碎**
Silero VAD 判断语音边界，`min_silence_ms` 默认 380ms —— 比一般语音助手的 700ms+ 激进得多，因为直播不能等。如果主播语速特别快、句中换气很短，把 `min_silence_ms` 调到 `250~320` 会更跟手；如果发现长句老是被腰斩，调到 `450~600`。

**4. 翻译带上下文**
翻译不是孤立地翻一句。会把最近 `context_sentences`（默认 4）句「日→中」对照一起给模型，让它知道「刚才在聊什么、这个人指的是谁」。日语省略主语是常态，这一步对代词和主语的正确性影响最大。

**5. 识别提示词**
`asr.initial_prompt` 里注入了直播场景的说明（口語、フィラー、句読点を補う），能让标点和用词稳定不少。

---

## 五、让它越用越准

字幕难免有翻错的时候。鼠标移到那条字幕上，点右上角的 **✏**，把中文改成对的，点「记住这条」。
之后遇到同类句子，翻译会自动参考你的改法。

**如果是人名、专有名词、固定叫法**，勾上「这是专有名词」—— 程序会**直接把它写进
`glossary.yaml`**（术语表是每次翻译都强制带上的对照，比整句示例可靠得多），
并自动备份一份 `glossary.yaml.bak`。

攒到一定数量后，设置面板里点「查看已记住多少条」能看统计；
点「把候选写进术语表」可以把程序判定「反复出现」的短短语批量写入术语表。

### 两个文件的分工（别搞混）

| | `data/corrections.jsonl` | `glossary.yaml` |
|---|---|---|
| 谁写 | **程序自动写**（点「记住这条」时） | 程序只在勾「专有名词」时写；平时你手工维护 |
| 内容 | 日语原文 + 原译 + 你改成什么 | 只有「词 → 译法」 |
| 怎么生效 | 按相关度挑几条塞进 prompt（少样本示例） | **每次翻译全量强制带上** |
| 适合 | 整句的译法偏好 | 人名、梗、固定叫法 |

**人名一律走术语表。** 示例注入只对「相似句子」有效，而术语表是硬约束 ——
同一个名字不管出现在哪句话里都会被强制翻译成你指定的写法。

修正记录一行一条，可以直接用文本编辑器改，也可以删掉重来：

```json
{"ja": "ぺこら", "wrong": "佩科拉", "right": "佩克拉", "note": "", "scope": "term"}
```

同一句日语改了多次时，**只保留最后一次**（否则会积累互相矛盾的版本，塞进 prompt 只会让模型更糊涂）。

### 它到底是怎么"学"的（重要）

**它不是训练模型权重，而是把修正变成翻译时的上下文。** 这是刻意的选择：

- 微调模型需要**正确的训练数据**。系统自己跑出来的字幕不是正确答案 ——
  拿它去训练等于「用自己错误的输出去训练自己」，只会把错误固化成习惯。
- 而"给它看几个例子"成本为零、立刻生效，对翻译质量的提升反而最直接。

两种翻译方式拿到修正的路径不一样：

| 翻译方式 | 修正怎么生效 |
|---|---|
| 在线大模型 | 修正作为少样本示例塞进 prompt（`【用户以前的修正】（必须遵守）`） |
| 本地 NMT | 本地模型读不了 prompt，改成对译文做替换（只替换你明确改过的错译） |

而术语表对**两种方式都生效**，所以人名优先加术语表。

### 什么时候才值得考虑真正微调

当你攒到**几百条人工修正**之后，微调才有意义 —— 那时 `corrections.jsonl`
就是一份现成的、带"正确答案"的训练数据（日语输入 + 你的中文输出）。

路线大概是：用这份数据微调一个小翻译模型（MarianMT 或 Qwen 小尺寸），
导出成 CTranslate2 后替换 `models/` 里的模型 —— 正好是 `translate.local.backend: ct2`
那条通路。**但现在不建议做**：几十条数据微调必然过拟合，效果还不如现在这套示例注入。

---

## 六、调参速查

改 `config.yaml` 后重启软件生效（外观类可以在 ⚙ 设置里实时改）。

### 让程序自己调（推荐先看这个）

程序会边看边记：每句话的**语速（字/秒）**、**音频电平**、**识别耗时**，
只存数值不存字幕内容，写在 `data/stream_stats.jsonl`。

攒够 20 段之后，下次启动会**自动**把参数调到适合她语速的位置：

| 指标 | 自动调什么 | 为什么 |
|---|---|---|
| 语速 ≥6.5 字/秒 | `vad.min_silence_ms` → 300 | 语速快、停顿短，阈值大了会把句子切碎 |
| 语速 ≤3.5 字/秒 | `vad.min_silence_ms` → 460 | 语速慢、停顿长，阈值小了会切出碎句 |
| 电平 <0.008 | `audio.gain` → 3.0 | **声音小是识别准确度的最大杀手**，这条最有效 |
| 语速 ≥6.0 | `asr.partial_interval_ms` → 450 | 局部字幕刷新更快，跟手感更好 |

查看它算出了什么：

```bat
.venv\Scripts\python.exe -m backend.main --stats-report
```

会打印语速中位数、P90、说话占比、电平、以及建议值和理由。
不想让它自动改，把 `config.yaml` 里 `tuning.auto_apply` 设成 `false`（仍然记录）。
清空数据：`--stats-clear`。

**注意**：这只能把「能调的旋钮」拧到合适位置，
识别准确度的上限还是由 `asr.model` 决定的（large-v3-turbo）。

### 手动调参

| 现象 | 调整 |
|---|---|
| 字幕出得太慢 | `vad.min_silence_ms` 调小到 250~320；`asr.partial_interval_ms` 调小到 350 |
| 句子被切得太碎 | `vad.min_silence_ms` 调大到 450~600 |
| 偶尔冒出莫名其妙的句子 | `asr.no_speech_threshold` 调**小**（更严格），或在黑名单里加词 |
| 该出的字幕没出 | `asr.no_speech_threshold` 调大到 0.7；`vad.threshold` 调低到 0.35 |
| 直播声音小、采不到 | `audio.gain` 调到 2~4 |
| 识别跟不上语速（队列报警） | 换 `distil-large-v3` 不合适；改小 `asr.model` 到 `medium`，或降低 `partial_interval_ms` 的频率 |
| 显存不够 | `asr.compute_type` 改 `int8_float16` |
| 没显卡 | `asr.device: cpu`，`asr.model: medium`，`compute_type: int8` |

---

## 七、定时自动守直播（起床不用管）

如果主播固定某个时间开播（比如日本时间早 9 点 = 北京早 8 点），
可以用计划任务让她开播时自动把直播页打开，字幕程序顺手就开始翻。

本仓库带了一个 `scripts/auto_open_stream.ps1`，它做三件事：

1. 如果字幕程序没在跑，先启动它
2. **每分钟查一次主播的 YouTube RSS**，发现新视频（= 开播）就继续
3. 等 30 秒让直播稳定，再用 Chrome 打开直播页（带自动播放参数）

**为什么不能提前一晚挂着页面**：YouTube 上如果主播不提前创建「预约直播」，
`/live` 页面在未开播时是「此频道目前没有直播」，等她开播后那个页面**不会自动变成直播**。
必须先检测到开播、再打开，才会自动播放。

安装（改成你要的主播）：

```powershell
$script = 'G:\projects\jp-live-translator\scripts\auto_open_stream.ps1'
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $script)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday, Saturday, Sunday -At '07:55'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'HinomeLiveAutoOpen' -Action $action `
  -Trigger $trigger -Settings $settings -Force
```

删除：`Unregister-ScheduledTask -TaskName 'HinomeLiveAutoOpen' -Confirm:$false`

**必须满足的条件**：

- **电脑不能关机/睡眠** —— 休眠会停掉音频输出和采集，字幕就没了
  （电源设置里把「睡眠」设为「从不」，屏幕可以关）
- 保持**已登录**状态（计划任务是 Interactive 模式）
- Chrome 和字幕程序都会出现在任务栏，别手动关掉

日志：`logs/auto_open.log`（什么时候启动、第几次检测到开播、打开了哪个页面）
记住已见的视频 ID：`logs/last_video_id.txt`（删掉它会重新以当前最新视频为基准）

---

## 八、故障排查

先跑 `05_自检.bat`，它会逐项检查环境、设备、电平、翻译配置。

**「找不到 Python」**
装 Python 3.10~3.12 并勾选 Add to PATH；或者设置环境变量 `JP_LIVE_PYTHON` 指向 python.exe。

**采不到声音（电平一直是 0）**
1. 确认确实在放声音（不是静音、耳机没拔）
2. `05_自检.bat` 第 2 步看设备列表，把 `pa:xx` 这个 id 填到 `config.yaml` 的 `audio.device`
3. 如果你用 Voicemeeter 之类的虚拟声卡，采集目标要选**你实际听的那个输出设备**（名字里带 `[Loopback]`）
4. 部分蓝牙耳机独占模式会导致回环采集失败，换成扬声器试试

> 关于采集实现：优先用 `PyAudioWPatch`，因为它自带 WASAPI loopback 补丁。
> `sounddevice` 只有在它的 PortAudio 编译时带了 loopback 支持时才可用 ——
> 很多发行版没有，此时构造 `WasapiSettings(loopback=True)` 会直接抛 TypeError，
> 程序会自动探测并切到 PyAudioWPatch，不需要你手动处理。

**识别出来的日文是错的/字幕断断续续**
先看 `logs/jp-live-translator.log`，把 `log.level` 改成 `debug` 能看到被过滤掉的片段和 VAD 概率。
如果整段整段没有字幕，多半是 VAD 没检测到语音：`vad.threshold` 调低到 0.3，
并确认 `05_自检.bat` 里 VAD 后端不是 `energy`（energy 只是兜底，快速口语下断句很差）。

**字幕总是缺最后一两个字**
VAD 提前收尾了。确认跑的不是 energy VAD，然后把 `vad.min_silence_ms` 调大到 450~550。

**GPU 没被用上（自动退回 CPU，速度慢很多）**
faster-whisper 依赖 CUDA 12 的 `cublas64_12.dll` / `cudnn`，GPU 版 torch **不提供**这些文件。装一下：

```bat
.venv\Scripts\python.exe -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

约 700MB。程序会自动把这些 DLL 加进搜索路径（见 `backend/cuda_setup.py`），不用手动配 PATH。

**翻译报 401 / 403**
API Key 不对或没余额。注意 `base_url` 结尾不用写 `/chat/completions`，程序会补。
连续失败 4 次后翻译会**自动熔断**（避免每句话刷一条错误），字幕窗顶部提示一次原因；
修好 Key 后重启后端即可恢复。想确认这条链路本身没问题，跑 `python -m scripts.test_llm_path`
——它用本地 mock 服务把 prompt 注入、流式、错误处理、熔断全断言一遍，不需要真 Key。

**装 Electron 卡在下载**
`node_modules` 装完后 Electron 会再下约 110MB 的运行时。GitHub 在国内常只有几十 KB/s（要 40 分钟），
所以 `03_启动字幕.bat` 里已经把 `ELECTRON_MIRROR` 指向 npmmirror（约 1.9MB/s，一分钟左右）。
如果镜像失效，把那一行注释掉就会回退到 GitHub。

**字幕窗一片空白 / 不出现**
窗口可能是透明的但位置在屏幕外。删掉 `ui-settings.json` 重置窗口位置。或者先用 `04_终端调试.bat` 确认后端本身是好的。

**日志在哪**
`logs/jp-live-translator.log`，或点设置面板里的「打开日志」。

---

## 九、目录结构

```
jp-live-translator/
├── 01_安装依赖.bat ~ 06_回环测试.bat  一键脚本
├── config.yaml                     主配置
├── glossary.yaml                   术语表（人名对照）
├── requirements.txt
├── .venv/                          Python 虚拟环境（脚本自动创建）
├── backend/
│   ├── main.py           命令行入口 / --doctor / --probe-audio / --test-asr
│   ├── pipeline.py       采集→切句→识别→翻译 的线程编排
│   ├── audio_in.py       WASAPI loopback 系统声音采集（PyAudioWPatch 优先）
│   ├── vad.py            Silero VAD（onnx/torch）+ 能量兜底 + 分段状态机
│   ├── asr_engine.py     faster-whisper 封装 + 幻觉过滤
│   ├── translator.py     LLM / 本地 MarianMT / CT2 三个后端
│   ├── mt_context.py     滚动上下文 + 译文清理
│   ├── glossary.py       术语表加载与替换
│   ├── cuda_setup.py     CUDA DLL 自动加载（解决 cublas64_12.dll 找不到）
│   ├── server.py         本地 SSE 事件流（给字幕窗或 OBS 用）
│   └── bridge.py         JSONL 事件协议
├── ui/                   Electron 透明置顶字幕窗
├── scripts/
│   ├── setup_whisper.py  下载识别模型
│   ├── setup_vad.py      下载 Silero VAD 模型
│   ├── setup_local_mt.py 下载本地翻译模型（含自检）
│   ├── mock_llm_server.py OpenAI 兼容的 mock 服务（测试用）
│   ├── check_config.py   启动前确认 provider / 翻译后端是否正常
│   ├── test_loopback.py  回环采集→VAD→识别 测试
│   ├── test_pipeline.py  完整流水线测试
│   ├── test_llm_path.py  LLM 翻译链路测试（prompt/流式/错误/熔断）
│   ├── test_real_llm.py  用真实 API Key 试翻两句
│   ├── test_multisentence.py 多句译文累积测试
│   ├── test_sse_replay.py    SSE 粘性状态补发测试
│   └── acceptance_test.py 整机验收（后端进程 + SSE + 控制命令）
├── testdata/             测试音频
└── models/               模型缓存（约 1.9GB，别放 C 盘）
```

---

## 十、进阶：不装 Electron 也能用

核心功能全在 Python 侧，Electron 只是个显示层。

```bat
rem 终端里直接看双语字幕
.venv\Scripts\python.exe -m backend.main --pretty

rem 起一个本地 SSE 事件流，自己写个网页/OBS 浏览器源接
.venv\Scripts\python.exe -m backend.main --serve
rem 然后浏览器打开 http://127.0.0.1:8765/events
```

事件是 JSON Lines，字段：

```json
{"kind":"partial","id":12,"text":"それでですね"}                  // 未定稿日文
{"kind":"final","id":12,"text":"それでですね、","rtf":0.21}       // 定稿日文
{"kind":"translation","id":12,"text":"然后呢，","source":"それでですね、","latency_ms":420}
{"kind":"stats","finals":88,"translations":88,"dropped":3}       // 每 5 秒一次
```

用别的语言重写显示层时，照着 `ui/renderer/renderer.js` 里的 `onEvent` 实现即可。

---

## 十一、开发者：怎么验证改动没搞坏

五个层次，从快到慢：

```bat
rem 1. 环境 + 设备 + 电平 + 翻译配置
.venv\Scripts\python.exe -m backend.main --doctor

rem 2. 回环采集 -> VAD -> 识别（会从扬声器放一段日语，别静音）
.venv\Scripts\python.exe -m scripts.test_loopback

rem 3. 完整流水线（含 partial 推测和翻译）
.venv\Scripts\python.exe -m scripts.test_pipeline --provider local

rem 4. LLM 翻译链路：用 mock 服务断言 prompt 注入、流式、错误处理、熔断（不需要真 Key）
.venv\Scripts\python.exe -m scripts.test_llm_path

rem 5. 多句译文累积（一句话被切成多句翻译时不能互相覆盖）
.venv\Scripts\python.exe -m scripts.test_multisentence --asr-text "クレヨンしんちゃんは何?あ、子供時代の思い出みたいな感じだった。"

rem 6. SSE 晚连接也要能收到 ready（粘性状态补发）
.venv\Scripts\python.exe -m scripts.test_sse_replay

rem 7. 整机验收：真的拉起后端进程 + SSE + 控制命令
.venv\Scripts\python.exe -m scripts.acceptance_test
```

> 跑这些测试前最好先关掉正在运行的字幕程序。验收测试用的是**随机端口**，
> 如果你的实例占着 8765 也不会互相干扰（这个坑真踩过：测试连到了旧实例上，
> 结果 `ready` 永远收不到，白查了半天）。

`test_pipeline` 的输出会打印每条字幕的相对时间戳，用来判断延迟是否可接受；
正常情况应该是：讲话开始约 2.5~3 秒后出现第一行灰色 partial，句末静音约 0.4 秒后定稿，
定稿后 100~300 毫秒出中文。

Electron 侧有两个不需要人工盯窗口的测试：

```bat
cd ui
npx electron . --ui-test    rem 在真实 Chromium 里断言字幕 DOM（25 项）
npx electron . --smoke      rem 拉起窗口 + 真后端，打印 SMOKE_RESULT 后自动退出
```

> 注：`electron . --ui-test` 和 `--smoke` 都要求环境里**没有** `ELECTRON_RUN_AS_NODE=1`，
> 否则 Electron 会退化成纯 Node 跑不起来。程序检测到这种情况会直接报清楚的错。

---

## 十二、已知限制

- **只有 Windows**：依赖 WASAPI loopback 采集系统声音。macOS 需要改 `audio_in.py`（用 BlackHole 之类的虚拟声卡），Linux 用 PulseAudio monitor。
- **只翻日语**：识别模型固定 `language="ja"`。翻别的语言要改 `asr.language` 和 `asr.initial_prompt`。
- **不翻译弹幕**：只处理直播音轨里的声音。
- **partial 会闪**：未定稿的灰字在主播改口时可能跳动，这是流式识别的固有特性。介意的话把 `partial_interval_ms` 设成 0 关掉，只用定稿字幕。
- **本地 NMT 质量有限**：免费离线的代价，网络梗和省略句容易翻飞。追求观感就配 LLM。
