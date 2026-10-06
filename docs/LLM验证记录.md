# LLM 与文本工具验证记录

验证日期：2026-10-07。

## 实现范围

- 远端端点：https://api.deepseek.com。
- 当前模型：deepseek-flash；配置可替换模型和兼容端点。
- 密钥：读取 DEEPSEEK_API_KEY，环境变量优先于根目录 .env.local。
- 接口：原生 LiveKit OpenAI 插件 LLM，通过 OpenAI SDK 使用流式 Chat Completions。
- 业务工具：LiveKit function_tool，复用 SQLite 预约服务。
- 当前不创建上级 AgentSession、Worker、语音管线、前端或 LiveKit 房间。

锁文件中的相关版本：livekit-agents 1.8.5、livekit-plugins-openai 1.8.5、openai 2.54.0、python-dotenv 1.2.4。保留已有 pytest 8.4.2，不升级原有测试依赖。

## 真实流式冒烟

使用现有环境变量执行真实 API 请求，没有将密钥写入文件或打印。结果：

| 指标 | 首次成功运行 |
| --- | --- |
| 非空内容片段数 | 39 |
| 首内容延迟 | 约 1.93 秒 |
| 总耗时 | 约 2.34 秒 |
| 输入 token | 23 |
| 输出 token | 39 |

测试实际遍历 LLM.chat() 流，而不是将完整回复事后拆分。以上是单次请求数据，不是性能 SLA。

## 文本工具场景

真实模型测试使用临时 SQLite 数据库，不改变日常 demo 数据。

1. 查询空闲时段 → 中英混合请求准备九点预约 → 改为十点并要求英文回复 → 显式确认 → 查询预约状态 → 取消。
   - 观察到 list_available_slots、prepare_booking、confirm_booking、get_booking、cancel_booking 调用。
   - 改口后的旧操作失效，版本增加，确认前没有正式预约。
   - 确认后预约占用正确时段，取消后时段释放。
   - 六轮用户输入，五条业务审计事件。
2. 请求仅准备、不确认 → 放弃。
   - 观察到查询、准备和 invalidate_operation 调用。
   - 未产生正式预约，待确认操作被置为失效。
   - 两轮用户输入，两条业务审计事件。

两项场景共约 15.23 秒通过；真实流式测试此前独立通过。断言以工具调用与数据库结果为主，不依赖固定回复措辞，也没有增加一个付费评审模型。

写入授权由测试宿主在显式用户请求后授予具体 operation_id/version 或 booking_id，不通过模型提供的布尔值授权。当前没有实现任意自然语言确认的自动判断，不能将这些测试解释为已经完成上级对话状态机。

本地完整报告位于 logs/llm-booking-lifecycle.json 和 logs/llm-no-implicit-confirmation.json。报告包含原生角色消息、函数调用与返回、延迟和审计，通过原子替换避免重复追加；该目录被忽略。

## 离线回归

默认测试：12 项通过，3 项真实 API 测试跳过，耗时约 2.11 秒。

覆盖原有预约后端、配置验证、环境变量优先级、缺少凭据、工具白名单和参数验证、宿主授权、改口使旧授权失效、重复确认与取消，以及分片工具参数的组装和角色记录。

分片协议测试采用离线 SSE 响应。它验证适配器行为，不替代真实 API 流式冒烟。

## 运行命令与环境限制

```bash
uv sync --locked --python 3.11
uv run --locked pytest -q
PYTHONPATH=backend uv run --locked python -m opentalk.llm.smoke
uv run --locked pytest tests/test_llm_live.py --live-llm -q
```

后两条会调用真实服务并消耗 token。默认 pytest 即使检测到密钥也不运行真实 API 测试。

当前受限沙箱中，asyncio.run() 在线程池退出阶段可能停住。最小复现仅运行 asyncio.to_thread，也出现工作完成后进程未退出的情况。离线完整测试在获准的沙箱外环境通过；没有为此改变业务异步实现。沙箱外真实调用也已成功。uv 缓存继续使用 UV_CACHE_DIR=/tmp/opentalk-uv-cache。

## 官方接口资料

- [DeepSeek 工具调用](https://api-docs.deepseek.com/guides/tool_calls/)
- [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
- [OpenAI 流式输出](https://developers.openai.com/api/docs/guides/streaming-responses)
- [LiveKit 兼容 LLM 接口](https://docs.livekit.io/agents/models/llm/openai-compatible-llms/)
