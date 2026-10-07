# Soniox ASR 文件回放验证

## 已实现范围

`opentalk.asr.provider.create_stt()` 返回官方 `livekit.plugins.soniox.STT` 实例，符合 LiveKit 原生 STT 接口，现已接入项目的 AgentSession 和网页语音链路。本指南专门验证独立文件回放，不启动 LiveKit 房间、前端、VAD、LLM 或 TTS；完整交互见 [网页语音验证指南](网页语音验证指南.md)。

锁定的 LiveKit Agents 与 Soniox 插件版本均为 1.8.5，PyAV 为 18.1.0。安装沿用仓库锁文件。

## 配置与运行

公开配置集中在 `config/asr.toml`：

| 设置 | 当前值与含义 |
| --- | --- |
| model | `stt-rt-v5` |
| base_url | Soniox 官方实时 WebSocket 端点 |
| api_key_env | `SONIOX_API_KEY` |
| sample_rate | 16000 Hz，单声道 PCM16 |
| language_hints | `zh`、`en`，非严格限制 |
| max_endpoint_delay_ms | 1500 ms |
| connection_timeout_seconds | 15 秒；显式关闭框架 API 错误重试 |
| frame_ms | 每片 20 ms，按原音频时长回放 |
| tail_silence_ms | EOF 后继续发送 3000 ms 静音 |
| drain_timeout_seconds | 静音发送完成后，等待剩余结果最多 15 秒 |

在根目录 `.env.local` 中设置 `SONIOX_API_KEY`，或通过进程环境变量提供。已有环境变量优先于文件。ASR 不需要 DeepSeek 密钥。`.env.example` 只包含占位符。

从项目根目录运行：

```bash
uv sync --locked --python 3.11
PYTHONPATH=backend uv run --locked python -m opentalk.asr.replay recordings/demo.wav --output logs/asr-demo.json
```

将 `recordings/demo.wav` 替换为自己的录音路径，可以是绝对路径。WAV、MP3、M4A 等格式使用已安装 PyAV 支持的解码器处理，不要求安装外部 ffmpeg 命令。读取本地文件，增量解码、混为单声道并重采样；不将完整录音一次性上传，也不调用异步文件转录 API。

命令会调用真实付费 API，尾部静音也发送给服务。缺少密钥会明确报错。空文件或不能解码的输入在建立付费连接前拒绝；后续解码错误同样作为失败处理。

配置和输出可以显式指定：

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.asr.replay recordings/demo.mp3 --config config/asr.toml --output logs/asr-demo.json
PYTHONPATH=backend uv run --locked python -m opentalk.asr.replay --help
```

省略 `--output` 时保存到 `logs/asr-<session_id>.json`。重复指定同一输出路径时采用临时文件加原子替换，不追加重复消息。输出不能覆盖输入录音。录音和日志目录均已加入忽略规则。

## 建议手动录音用例

建议录制一段约 15～30 秒的音频，包含下列内容，并在完整句子之间停顿 2～3 秒：

| 用例 | 示例录音内容 | 检查重点 |
| --- | --- | --- |
| 中文 | 请帮我查询明天上午会议室 A 的可用时间。 | 中文原文、时间与房间名称 |
| 英文 | Please check Room A tomorrow morning. | 不翻译成中文 |
| 句内混合 | 帮我 book Room A，明天 ten o'clock，先不要确认。 | 同一句中保留中英原文 |
| 跨句切换 | 先说一句中文，再说一句完整英文。 | 在同一连接内识别，不重建会话 |
| 改口 | 预约九点，不对，改成 ten o'clock。 | 保留改口内容；ASR 本身不执行预约 |
| 多个停顿 | 多次说话，句间停顿 2～3 秒。 | 最终片段和消息编号可区分 |
| 无尾部停顿 | 最后一个词说完即结束录音。 | 回放追加静音后，末尾内容是否最终化 |

短录音可能只出现最终结果，不能据此认定没有 Streaming。较长录音更容易观察临时字幕持续更新。识别文本无需严格匹配标点，但应检查语言、日期、时间及改口内容是否正确。

回放时按 Ctrl+C 可以检查取消：输入发送和后台任务停止，连接关闭，日志标为 `cancelled`。这验证组件取消能力；真实用户 barge-in、VAD 触发及停止机器人播报需要完整会话与 TTS 阶段验证。

## 输出与日志语义

终端即时输出带文本的 JSON 事件，最后输出摘要。日志中包含：

- `events`：唯一 `event_id`、UTC 接收时间、从回放开始计算的单调时钟耗时、是否仍在回放原文件、事件类型及相关消息编号。
- `messages`：按 `message_id` 更新的用户消息，包含 `role=user`、`source=asr`、原文、`revision`、`is_final` 和供应商提供的音频时间。
- `interim_transcript` 与 `preflight_transcript` 都是临时状态。它们可以被修订，不提交业务操作，也不作为最终消息另行追加。
- `final_transcript` 将对应消息更新为最终状态；下一段话使用新的消息编号。日志保留临时事件修订历史，重复相同临时文本不会新增消息或增加消息修订编号。
- `processed_audio_seconds` 是插件报告的累计处理时长；`audio_duration_seconds` 是原文件解码后的时长，另有尾部静音时长。
- `first_interim_seconds`、`first_final_seconds` 包含解码、连接和录音回放时间，不等于用户说话结束后的模型纯处理延迟。
- `status` 区分 `passed`、`failed` 和 `cancelled`。失败仅保存错误类型，不保存供应商原始认证相关异常载荷。

当前文件输入来源固定为用户，角色不依赖声纹识别。应用不保存单一 `detected_language`；翻译、语言标注和说话人识别均关闭。`language_hints` 仅提示识别，不决定回复语言，也不限制只能识别中英文。

## EOF 与结束条件

当前锁定的官方插件不会把 LiveKit `end_input()` 转换为 Soniox 的空文本结束帧，也不会因此自行结束接收与保活任务。文件测试入口采用麦克风式的尾部静音，让最后一句通过服务端 endpoint detection 最终化。

回放入口等待：输入已结束、服务报告处理时长覆盖原录音和尾部静音、没有尚未最终化的临时消息。达到条件后主动关闭流及 HTTP session。有已最终化的非空语音文本才标为通过；没有处理进度、临时文本未最终化或未识别到语音都会失败。

该策略用于有限文件验证，并不等于收到 Soniox `finished` 协议回执，也不证明每个词识别准确。未修改官方插件内部逻辑。未来完整会话使用持续音频、VAD 与轮次控制时，需单独验证断连恢复及供应商事件顺序。

## 已完成验证

2026-10-07：18 项 ASR 离线测试通过，使用真实官方插件并模拟 WebSocket，不访问外网、不产生 API 费用。覆盖配置与凭据优先级、WAV/MP3 解码、立体声混音、重采样、PCM 分片、混合语言临时修订、preflight 状态、最终文本、EOF 等待、API 错误、超时、无语音、取消及资源关闭、消息去重和日志原子替换。全量离线回归为 30 项通过、3 项付费 LLM 测试默认跳过。

```bash
uv run --locked pytest tests/test_asr_offline.py -q
uv run --locked pytest -q
```

当前环境未提供 `SONIOX_API_KEY`，没有用于真实测试的用户录音，因此尚未验证 Soniox 连接、实际识别准确率或真实服务延迟。上面的手动入口用于完成这部分验证。

## 官方参考

- [LiveKit Soniox STT 插件](https://docs.livekit.io/agents/models/stt/soniox/)
- [Soniox 实时转录](https://soniox.com/docs/stt/rt/real-time-transcription)
- [Soniox WebSocket 协议](https://soniox.com/docs/api-reference/stt/websocket-api)
- [Soniox endpoint detection](https://soniox.com/docs/stt/rt/endpoint-detection)
