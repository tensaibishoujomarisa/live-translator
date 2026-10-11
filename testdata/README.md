# 测试音频放这里

仓库**不包含**测试音频（跑测试时会自动下载一段约 440KB 的日语样本）。

- `python -m scripts.test_loopback` 第一次运行会自动下载并转成 `ja_sample16k.wav`
- 也可以自己放一段 **16kHz 单声道 wav** 命名为 `ja_sample16k.wav`
- 想用别的文件：`python -m scripts.test_loopback --wav 你的文件.wav`
- 完全不想联网：随便录一段日语/或放任何语音，识别结果对错不影响"链路是否通"的判断

自动下载源是 HuggingFace 上的 Common Voice 日语样本
（[japanese-asr/ja_asr.common_voice_8_0](https://huggingface.co/datasets/japanese-asr/ja_asr.common_voice_8_0)）。
如果下载不通，设一下代理：`set HTTPS_PROXY=http://127.0.0.1:7890`。
