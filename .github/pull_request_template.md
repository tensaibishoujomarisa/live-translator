## 改了什么

<!-- 一两句话说明。如果是修 bug，说清「之前会怎样、现在会怎样」 -->

## 为什么这么改

<!-- 根因是什么？有踩坑背景最好写上 -->

## 验证

- [ ] `python -m backend.main --doctor` 通过
- [ ] `python -m scripts.test_llm_path` 通过
- [ ] `python -m scripts.acceptance_test` 通过
- [ ] `cd ui && npx electron . --ui-test` 通过
- [ ] 改了音频/VAD/识别相关：跑过 `scripts.test_loopback`
- [ ] 改了 `.bat`：确认过文件是**纯 ASCII**（中文会让 cmd 解析错位）

## 环境

- 显卡 / 驱动：
- 翻译方式（llm / local / off）：

## 备注

<!-- 截图、日志、已知限制、后续想做的事 -->
