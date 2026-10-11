# 贡献指南

谢谢你有兴趣改这个项目。它是照着「自己看直播真的用得上」做的，所以**实用性 > 优雅**：
任何能让字幕更准、更快、更少打扰的改动都欢迎。

## 环境准备

```bat
01_安装依赖.bat        :: 建 .venv 并装依赖
02_下载模型.bat        :: 下识别模型（约 1.6GB，唯一需要联网的一步）
05_自检.bat            :: 确认环境、音频设备、电平都正常
```

无显卡也能开发：`config.yaml` 里把 `asr.device` 改成 `cpu`、`asr.model` 改成 `medium`，
只是会慢很多。没有 API Key 就把 `translate.provider` 设成 `local` 或 `off`。

## 提交前请跑测试

```bat
rem 核心（不需要联网、不需要 API Key）
.venv\Scripts\python.exe -m backend.main --doctor
.venv\Scripts\python.exe -m scripts.test_llm_path
.venv\Scripts\python.exe -m scripts.test_multisentence --asr-text "クレヨンしんちゃんは何?あ、子供時代の思い出みたいな感じだった。"
.venv\Scripts\python.exe -m scripts.test_sse_replay
.venv\Scripts\python.exe -m scripts.acceptance_test

rem 需要声卡（会从扬声器放一段日语，注意别在开会时跑）
.venv\Scripts\python.exe -m scripts.test_loopback
.venv\Scripts\python.exe -m scripts.test_pipeline --provider local

rem Electron 侧
cd ui && npx electron . --ui-test    :: 断言字幕 DOM（不需要人工看窗口）
cd ui && npx electron . --smoke      :: 拉起真后端，打印 SMOKE_RESULT 后退出
```

跑测试前**先把正在运行的字幕程序关掉**。这些测试会真的播放音频、占用音频设备；
acceptance_test 已经改成用随机端口，不会和你的实例抢 8765。

## 代码约定

- **注释写「为什么」，不写「是什么」**。这个项目里踩过的坑（Silero VAD 的 64 采样上下文、
  CT2 转换导致解码退化、cmd 里 `set /p` 的括号陷阱…）都在注释里留了说明，改动时请保持。
- **不要吞异常**。PortAudio 有些失败会抛**空消息**的异常，早期版本直接吞掉，
  结果日志只有「采集失败：」这种没法排查的输出。抛的时候一定带上类型和消息。
- **批处理文件保持纯 ASCII**。`.bat` 里出现中文，在 `chcp 65001` 生效前 cmd 会按系统代码页
  解析导致行错位。文件名可以是中文，内容不行。
- Python 侧用 `from .x import y` 相对导入，脚本用 `python -m scripts.xxx` 运行。
- 新增可调参数时，同时加到 `config.yaml`（带中文注释）和 `readBackendDefaults()`（`ui/main.js`），
  否则界面里的默认值会反过来覆盖配置文件。

## 想改的地方（欢迎 PR）

- **更多语言对**：现在固定日语→中文。`asr.language` + 术语表 + 翻译 prompt 三处都写死了日语假设。
- **macOS / Linux 支持**：`backend/audio_in.py` 用的是 Windows WASAPI loopback。
  macOS 需要 BlackHole 之类的虚拟声卡，Linux 用 PulseAudio monitor。
- **字幕后处理**：把长句拆成更适合扫读的短行、按语速动态调整停留时间。
- **导出**：直播文字稿导出 srt/txt。
- **本地 LLM 翻译**：接 Ollama 已经能用（改 base_url），但还没做模型自动探测。
- **打包成 exe**：现在需要装 Python，用 PyInstaller + electron-builder 可以做成绿色版。

## 提交 PR

1. Fork，从 `main` 开分支
2. 一个 PR 做一件事，标题说清改了什么
3. 说明**你实际验证过什么**（跑了哪个测试、什么硬件、什么场景）——
   比"应该能用"有用得多
4. 如果改了行为，同步更新 README

## 别提交这些

`.gitignore` 已经挡住了，但再提醒一次：**`ui-settings.json` 里会存 API Key**，
`models/` 有 2GB，`.venv/` 和 `ui/node_modules/` 各几百 MB。这些都不该进仓库。

## 许可证

提交即表示你同意以 MIT 许可证发布你的贡献。
