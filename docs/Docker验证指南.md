# Docker 启动与分段验证

## 范围与结构

在项目根目录使用 Docker Compose v2。Windows／WSL 环境需要运行 Docker Desktop，并启用当前发行版的 WSL integration。此配置面向同一台电脑的浏览器，网页地址为 `http://localhost:3000`。

| 服务 | 用途 | 持久化与可见端口 |
| --- | --- | --- |
| `livekit` | 房间、信令和媒体传输 | 宿主机 localhost TCP 7880、TCP 7881、UDP 7882 |
| `api` | 独立 SQLite 会话管理／token API | `session-data`；8080 仅容器内部可见 |
| `worker` | 原生 AgentSession 和可替换业务 adapter | `session-data`、`booking-data`、`telemetry-data`；8081 仅容器内部可见 |
| `frontend` | Next.js standalone 网页及同源代理 | 宿主机 localhost TCP 3000 |
| `booking-init` | 幂等初始化业务测试对象 | `booking-data`；成功退出是正常状态 |

业务、会话和遥测是三个独立 SQLite 文件／数据卷；会话 API 不挂载业务库。worker 通过 adapter 选择业务。`booking-init` 仅提供默认预约用例的初始化，前端没有预约专用界面。`OPENTALK_AGENT=conversation` 可切换到通用 talkbot。

构建沿用 `uv.lock`、`pnpm-lock.yaml`，固定 Python 3.11.14、uv 0.9.21、Node.js 24.15.0、pnpm 10.13.1、LiveKit Server 1.13.8。Silero 模型随已锁定的插件安装，不需要麦克风设备映射或 PortAudio。应用服务以 UID 10001 运行；容器日志每个服务最多保留三个 10 MB 文件，原有 SQLite 遥测保留策略继续生效。

## 1. 准备与构建

根目录已有 `.env.local` 时继续使用，至少填写 `DEEPSEEK_API_KEY`、`SONIOX_API_KEY`；没有时从 `.env.example` 复制。该文件只在运行时传入 Python 服务，构建上下文排除凭据、宿主机数据库、录音和日志。Compose 统一设置本地 LiveKit 的 `devkey / secret` 与内部地址，因此不需要改写原生启动用的 `.env.local`。

```bash
docker version
docker compose version
docker compose config --quiet
docker compose build
```

默认读取 `.env.local`。若使用另一份凭据文件，可在所有 Compose 命令前设置 `OPENTALK_ENV_FILE=/absolute/path/to/provider.env`。构建过程不调用模型；镜像和锁定依赖下载需要网络。

## 2. 启动与健康检查

如果原生服务正在占用默认端口，先停止它们，或使用下一节的独立测试端口。

```bash
docker compose up -d --wait --wait-timeout 120
docker compose ps -a
docker compose logs --tail 100 worker api
curl --fail http://localhost:3000/api/control/health/services
```

LiveKit、API、worker 和前端应为 healthy，`booking-init` 为 exited (0)。Services 应显示 sessions/livekit/worker 为 ok、providers 为 configured。检查不调用模型；configured 只表示凭据已填写，不代表模型 API 已验收。

网页测试：输入文字应流式返回文本；点击 Start voice 应申请麦克风，显示波形和状态；Cancel voice 返回文字模式；刷新／关闭页面后应保留历史，下一次输入恢复正常结束的会话。异常结束的会话保持不可恢复。付费 provider 与真实麦克风的人工用例仍按 [网页语音验证指南](网页语音验证指南.md) 执行。

```bash
# Switch the agent without changing frontend code.
OPENTALK_AGENT=conversation docker compose up -d --wait
# Stop containers while retaining data.
docker compose down
```

服务的启动依赖使用健康检查和初始化成功状态；参见 [Docker Compose 启动顺序](https://docs.docker.com/compose/how-tos/startup-order/)。worker 使用 SDK start 模式，保留一个预热进程，并给予 30 秒容器关闭时间；正在运行的会话在停机后可能成为 failed，不能承诺重建会恢复尚未正常结束的对话。

## 3. 独立离线验证

先运行容器内完整后端回归；测试镜像不接收凭据，也不挂载演示数据卷：

```bash
docker compose -p opentalk-offline-tests --profile test run --build --rm --no-deps backend-tests
```

另开一个终端，从项目根目录设置独立项目／端口：

```bash
export COMPOSE_PROJECT_NAME=opentalk-verify
export COMPOSE_FILE=compose.yaml:compose.verify.yaml
export OPENTALK_FRONTEND_PORT=19400
export OPENTALK_LIVEKIT_PORT=19480
export OPENTALK_RTC_TCP_PORT=19481
export OPENTALK_RTC_UDP_PORT=19482
docker compose config --quiet
docker compose up -d --wait --wait-timeout 120
docker compose exec -T worker python -m opentalk admin check
```

`compose.verify.yaml` 仅替换验证 worker 的 LLM/STT/TTS 为确定性测试桩，并使用假凭据。它仍执行生产 worker 的 entrypoint、真实 LiveKit 调度、RoomIO、SQL 工具、RPC 和 SQLite 持久化，不调用付费模型。测试使用 `opentalk-verify_*` 数据卷，与默认 `opentalk_*` 及宿主机数据分开。

在这个终端继续执行：

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm exec playwright install chromium
OPENTALK_TEST_EXTERNAL_SERVER=1 \
OPENTALK_TEST_URL=http://127.0.0.1:19400 \
OPENTALK_TEST_DOCKER=1 \
pnpm exec playwright test tests/docker.spec.ts
cd ..
```

用例检查服务健康、真实 worker 调度、分片文本／SQL 工具、历史去重、正常关闭／自动恢复、语音输入和音频接收。它使用浏览器模拟麦克风，不评价真实模型理解或发音质量。默认普通 `pnpm test` 跳过这个 Docker 用例。已有浏览器缓存时可设置 `PLAYWRIGHT_BROWSERS_PATH`。

如需从 WSL 控制 Windows Chrome，可在 Windows PowerShell 启动独立测试浏览器，再给上述测试命令增加 `OPENTALK_TEST_CDP_URL=http://127.0.0.1:19522`：

```powershell
$openTalkChromeProfile = Join-Path $env:TEMP 'opentalk-docker-verify-cdp'
& 'C:\Program Files\Google\Chrome\Application\chrome.exe' --headless=new --no-first-run --no-default-browser-check --use-fake-ui-for-media-stream --use-fake-device-for-media-stream --remote-debugging-port=19522 --remote-allow-origins=http://localhost:19522 "--user-data-dir=$openTalkChromeProfile" about:blank
```

该浏览器使用独立 profile；验证后关闭它。保持调试端口仅供本机使用。

## 4. 数据持久化与管理员操作

默认 Compose 的管理员命令如下。在独立测试终端中，命令作用于测试项目的数据卷。

```bash
docker compose run --rm --no-deps booking-init python -m opentalk admin init
docker compose run --rm --no-deps booking-init python -m opentalk admin init --start-date 2030-01-02 --days 7
docker compose run --rm --no-deps booking-init python -m opentalk admin inspect
docker compose run --rm --no-deps booking-init python -m opentalk admin inspect --table users
docker compose run --rm --no-deps booking-init python -m opentalk admin inspect --table reservations
docker compose run --rm --no-deps booking-init python -m opentalk admin query "SELECT kind, status, count(*) AS n FROM operations GROUP BY kind, status"
docker compose run --rm --no-deps booking-init python -m opentalk admin check
```

初次启动默认初始化从业务时区明天开始的七天：4 users、4 resources、112 slots、49 slot_users、49 operations。重复初始化不重复插入，也不恢复已取消预约。宿主机原有数据库不会自动导入容器；已完成预约验证的宿主机库保持原状。

完成一轮对话并正常关闭网页，记录 `/api/control/sessions` 与 history 的 ID／条目数，同时记录业务库 reservations/operations 数量，然后重建容器：

```bash
docker compose down
docker compose up -d --wait --wait-timeout 120
docker compose exec -T worker python -m opentalk admin inspect
curl --fail http://localhost:19400/api/control/sessions
```

此处端口 19400 对应上面的独立测试环境；默认项目使用 3000。历史和业务状态应保留；正常结束的会话仍可恢复。卷内路径分别为 `/app/data/booking/opentalk.sqlite3`、`/app/data/sessions/sessions.sqlite3`、`/app/logs/telemetry.sqlite3`。备份时使用 SQLite backup API 或在全部写入者停止后复制，不只复制可能存在 WAL 的主文件。

测试完成运行 `docker compose down`。不带 `-v` 保留数据；`down -v` 会删除当前项目的全部三个数据卷，仅用于明确要清空的独立测试库。

## 5. 媒体配置与排错

Compose 使用 Docker bridge 网络。LiveKit 的 `node_ip=127.0.0.1` 提供宿主机浏览器候选，`advertise_internal_ip=true` 同时提供 worker 可达的容器候选；UDP socket 绑定容器网卡。不要将原生 `config/livekit-local.yaml` 的 `enable_loopback_candidate` 和仅回环 IP 过滤照搬到容器：宿主机转发到容器网卡的包可能无法匹配回环候选。

信令与媒体端口参数分别为 `OPENTALK_LIVEKIT_PORT`、`OPENTALK_RTC_TCP_PORT`、`OPENTALK_RTC_UDP_PORT`。媒体端口同时调整 LiveKit listener 与 Docker 映射，保持宿主机／容器端口一致；只改 published port 会导致 ICE 使用错误端口。修改端口后重新执行 `up -d --wait`。这些参数在所有后续 up/recreate 命令中保持一致。

若出现 could not establish pc connection，检查实际浏览器是否在同一台电脑、媒体端口是否冲突／被拦截、是否误用了原生配置，以及 LiveKit 日志中 user participant 是否 active。Services 为 ok 不代表媒体通道可达。容器 healthcheck 与 `restart: unless-stopped` 是不同机制：健康状态失败可供排错，Docker 不会仅因 unhealthy 自动重启仍在运行的进程。

远程浏览器／公网部署需另外配置 HTTPS/WSS、可达媒体 IP 与必要的 TURN；当前 Compose 不作为远程部署配置。相关原理见 [LiveKit 自部署说明](https://docs.livekit.io/transport/self-hosting/deployment/)，镜像锁定安装方式参见 [uv Docker 集成](https://docs.astral.sh/uv/guides/integration/docker/)。

## 本轮验证记录

2026-10-08 分段验证：

- 清理旧版接口后，宿主机后端离线回归 `109 passed, 4 skipped`；容器测试镜像也为 `109 passed, 4 skipped`。没有调用付费模型。
- 前端类型检查、Prettier、宿主机 standalone 构建与 Docker 镜像构建通过。
- 普通浏览器／原生 RTC 回归在独立开发服务器及 standalone 容器前端上均为 `10 passed, 3 skipped`；Docker 独立端到端用例在 WSL Chromium、Windows Chrome 中分别通过，使用真实 worker 调度与媒体轨道、模拟 provider 和麦克风。
- 初次容器媒体测试失败，定位到回环候选与容器网卡接收路径不匹配；改为容器网卡绑定并同时发布内部／宿主机候选后，文本、语音及恢复通过。没有只凭 healthcheck 判定媒体可用。
- 已在独立业务库创建一条固定请求键的预约，再 `down` / `up` 重建所有容器，改用真实生产 worker 的 start 命令。三个数据库全部表的记录指纹完全一致：业务库 50 reservations／50 operations，会话库 4 sessions／6 attempts／20 history items，遥测 62 条。业务完整性检查通过。会话数字包括排错时保留的失败尝试。
- 宿主机原有业务／会话数据没有被用于容器测试。实际付费 provider、真人麦克风质量和远程／公网部署未在本轮重复验收。

