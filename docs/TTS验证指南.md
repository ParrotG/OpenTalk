# Soniox TTS 文本流验证

## 已实现范围

`opentalk.tts.provider.create_tts()` 返回官方 `livekit.plugins.soniox.TTS` 实例，符合 LiveKit 原生 TTS 接口。使用已锁定的 1.8.5 插件，没有增加或升级依赖，没有添加上级 AgentSession、前端或 LLM → TTS 编排。

独立测试入口接受文本或 UTF-8 文本文件，按配置模拟 LLM 增量输出，调用 `stream.push_text()`，同时消费音频帧并写入 WAV。文本不是一次性传给 `synthesize()`，音频也不是通过 REST API 完整下载后处理。

## 配置与运行

凭据继续使用进程环境变量 `SONIOX_API_KEY` 或根目录 `.env.local`，进程环境变量优先。ASR 和 TTS 使用同一个变量，不需要 DeepSeek 密钥或 LiveKit Server。

公开配置位于 `config/tts.toml`：

| 配置 | 默认值与含义 |
| --- | --- |
| websocket_url | Soniox 官方实时 TTS WebSocket 端点 |
| model | `tts-rt-v2`，显式配置，避免依赖插件的旧模型默认值 |
| voice | `Maya` |
| language | `zh`，主要发音语言；可通过命令行覆盖 |
| sample_rate | 24000 Hz，输出单声道 PCM16 |
| speed | 1.0 |
| connection_timeout_seconds | 15 秒，框架 API 重试关闭 |
| stream_idle_timeout_seconds | 5 秒，由官方插件处理句子间停顿和供应商流轮换 |
| chunk_chars | 测试入口每次提交 12 个字符 |
| chunk_delay_ms | 测试入口分片之间等待 150 ms |
| timeout_seconds | 整个文本输入和合成最多 120 秒 |

从仓库根目录运行：

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.tts.smoke --text "会议室 A 明天上午十点可用。Please confirm the date and time before booking." --output recordings/tts-demo.wav --report logs/tts-demo.json
```

也可以使用自己的 UTF-8 文本文件：

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.tts.smoke --text-file recordings/tts-input.txt --language en --output recordings/tts-en.wav --report logs/tts-en.json
```

可选参数包括 `--config`、`--language`、`--voice`、`--output`、`--report` 和 `--cancel-after`；完整列表：

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.tts.smoke --help
```

命令会调用付费 Soniox API。合成成功后，用本机音频播放器打开 WAV 试听，不要求安装额外播放器或 ffmpeg。当前入口没有实时扬声器播放；流式音频消费和文件写入在合成期间发生，试听发生在文件保存之后。

省略输出选项时，音频位于 `recordings/tts-<session_id>.wav`，日志位于 `logs/tts-<session_id>.json`。指定同名输出时，成功结果采用临时文件加原子替换，不追加重复消息。API 失败或超时保留已有完整音频，报告标为 `failed`。

## Streaming 与语言语义

官方插件先缓冲增量文本至完整句子，再通过持久 WebSocket 发送，兼顾延迟和连贯性。测试入口的字符分片用于模拟 LLM 输出，不等于每片建立一个合成请求。没有手动对每个字符分片调用 `flush()`。

供应商要求 `language` 参数。它决定整体发音风格，不是自动检测结果，也不要求一句话只能包含一种语言。混合文本按原文提交，不拆成中英两条音轨；未来由回复策略将 `preferred_response_language` 投影为该参数。

原生 provider 支持 `update_options(language="en")`；之后开启的新流使用新参数。已验证离线场景中，中英两轮使用同一个 TTS 实例与连接、不同 stream_id、相同声音。当前 CLI 每次运行只合成一条回复，不在活动流中途改变主要发音语言。

短句或快速提交的文本可能在输入结束后才出现首音频。要观察输入输出重叠，建议使用包含数个完整句子的长文本，或在配置副本中增大 `chunk_delay_ms`；不要把单次未观察到重叠等同于不支持 Streaming。

## 取消与部分音频

手动按 Ctrl+C，或指定自动取消时刻：

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.tts.smoke --text-file recordings/tts-input.txt --output recordings/tts-cancel.wav --report logs/tts-cancel.json --cancel-after 2
```

取消时停止文本提交和音频消费，通过原生插件关闭流和连接，报告标为 `cancelled`，退出码为 130。已经收到音频时，单独保存可播放的 `tts-cancel.partial.wav`；没有音频时不产生部分文件。原有的 `tts-cancel.wav` 保留。

这验证组件取消与资源释放，不代表已实现用户声音触发的 barge-in、播放队列清空或完整会话的过期响应拦截。

## 日志与验收

终端即时输出 `first_audio`、`input_ended` 和 `stream_completed` 事件，合成完成后输出摘要。JSON 报告包含：

- `messages`：一条 `role=assistant`、`source=tts_test` 消息，记录完整待合成 `generated_text` 与实际已提交 `submitted_text`。取消时二者可能不同。
- `events`：唯一事件编号、UTC 时间、单调时钟耗时、文本分片、原生 request_id / segment_id、音频帧样本数及结束状态；不记录 PCM 原始数据或密钥。
- `first_audio_seconds`：从测试开始到首音频消费，包含连接、分句和模拟文本输入耗时，不等于供应商纯处理延迟。
- `audio_before_input_end`：是否在提交完全部文本前收到首音频。
- `text_chunks`、`audio_frames` 和 `audio_duration_seconds`：输入分片、收到帧数和累计合成音频时长。
- `audio_file`：本次实际保存的完整或部分文件；失败或未收到音频时为空。

没有自动播放或词级对齐，因此不记录声称精确播出的 `spoken_text`，不把整段生成文本当作完整播报。

建议分别试听中文、英文和句内混合文本，检查日期、时间、房间名称、外语短语及数字发音；再使用长文本观察 `audio_before_input_end`，最后验证取消和部分文件。

## 验证记录

2026-10-07：18 项 TTS 离线测试通过，使用真实 Soniox 插件与模拟 WebSocket，覆盖环境变量优先级、配置校验、文本与音频重叠、完整文本保留、WAV 二进制输出、结束握手、API 错误、无音频、异常终止、超时、取消及部分音频、资源释放、日志原子替换和两轮语言切换时的连接复用。全量离线回归为 48 项通过、3 项付费 LLM 测试默认跳过。

```bash
uv run --locked pytest tests/test_tts_offline.py -q
uv run --locked pytest -q
```

当前开发环境未检测到 Soniox 凭据，尚未执行真实 TTS API 验证；实际发音质量、真实首音频延迟和服务连通性需通过上述手动命令确认。用户已报告此前 ASR 测试用例通过，此处没有重复执行 ASR 付费测试。

## 官方参考

- [LiveKit Soniox TTS 插件](https://docs.livekit.io/agents/models/tts/soniox/)
- [Soniox 当前 TTS 模型](https://soniox.com/docs/tts/models)
- [Soniox WebSocket 协议](https://soniox.com/docs/api-reference/tts/websocket-api)
- [Soniox 语言混读](https://soniox.com/docs/tts/concepts/language-mixing)
- [Soniox 流结束与取消](https://soniox.com/docs/tts/rt/termination)
