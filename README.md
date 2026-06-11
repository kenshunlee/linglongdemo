# 微信小程序 ASR 本机 Demo（LAN/USB 可达）

本项目用于临时 Demo：后端运行在本机，优先使用本地 GPU 算力；安卓真机小程序通过 LAN 或 USB 网络共享访问本机服务。

Demo 场景统一单端口：业务服务与调试都使用 `8765`（调试采用 launch 模式，不再额外占用 debug 监听端口），机器人 SDK 仍然使用 `3336/3333/4141`。

## 架构

```
安卓微信小程序
    │
    │ wx.uploadFile(audio)
    ▼
本机 Python ASR 服务（0.0.0.0:8765）
    │
    ├─► faster-whisper（本地优先，GPU 优先，失败降级 CPU）
    ├─► GLM-ASR-2512（配置 API Key 时兜底）
    ├─► whisper.cpp CLI（可选兜底）
    └─► phi3-fallback（最终降级提示）
    │
    ▼
output/asr{时间戳}.txt
```

后端接口保持不变：/health、/transcribe、/records。

已新增回流联调接口（用于赛星实训平台）：

- GET /reflow/health
- GET /reflow/session/current
- GET /reflow/session/status?session_id=...
- POST /reflow/auth/login
- POST /reflow/auth/me
- POST /reflow/bootstrap
- POST /reflow/session/start
- POST /reflow/session/finish
- POST /reflow/task/report
- POST /reflow/embodied/batch
- POST /reflow/trajectory/batch
- POST /reflow/events/batch

已新增市集导购任务编排接口：

- GET /mission/status
- GET /mission/history?limit=10
- POST /mission/start
- POST /mission/start_and_wait
- POST /mission/stop

## 快速开始

### 1) 安装依赖

```powershell
f:/Robots/team66/asr/.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
```

### 2) 启动后端

方式 A：双击 [backend/start.bat](backend/start.bat)

方式 B：命令行

```powershell
f:/Robots/team66/asr/.venv/Scripts/python.exe backend/server.py
```

建议可选环境变量：

```env
LOCAL_ASR_ENABLED=1
LOCAL_ASR_MODEL_SIZE=small
LOCAL_ASR_DEVICE=auto
LOCAL_ASR_LANGUAGE=zh
ASR_HOST=0.0.0.0
ASR_PORT=8765

REFLOW_ENABLED=1
REFLOW_BASE_URL=https://fuxingdao.sh-aia.com
REFLOW_LOGIN_NAME=team66
REFLOW_PASSWORD=你的平台密码
REFLOW_TEAM_ID=team66
REFLOW_ROBOT_ID=R-team66-01
REFLOW_SCENE_ID=market
REFLOW_BATCH_MAX=200
REFLOW_VALIDATE_TEAM_BINDING=1

MISSION_ENABLED=1
MISSION_DRY_RUN=1
MISSION_DEFAULT_SPEED_MPS=0.18
MISSION_AUTO_REFLOW=1
```

说明：

- /reflow/bootstrap 会执行登录、team_id 绑定校验（auth/me）并可选直接创建 session。
- /reflow/trajectory/batch、/reflow/embodied/batch、/reflow/events/batch 支持超长数组自动分包（每包最多 REFLOW_BATCH_MAX 条）。
- /mission/start 接收 command_text，按市集导购流程驱动状态机；MISSION_DRY_RUN=1 时只记录动作不下发机器人控制。
- 开启 MISSION_AUTO_REFLOW=1 后，mission 的状态和动作会自动触发会话创建、任务上报、轨迹/具身/事件上报与会话收口。

### 3) 检查服务状态

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8765/health | ConvertTo-Json -Depth 6
```

重点字段：

- asr_provider
- asr_model
- active_engine
- device
- gpu_available
- local_asr_ready

### 4) 调试后端

1. 打开 VS Code 的“运行和调试”。
2. 选择 `ASR Backend: One-click Debug`。
3. 该配置会先执行 `backend/start-debug-attach.bat` 拉起后端服务，再自动 attach 到 `127.0.0.1:5679`。
4. 断点打在 `backend/server.py`、`robot_control.py` 或其他后端文件里都可以直接命中。

### 5) Release 运行

非调试场景直接运行 `backend/start.bat` 即可，脚本会使用业务端口启动服务（默认 `8765`）。

## 小程序联调

### 方案 A：同 Wi-Fi（推荐）

1. 手机与电脑连接同一局域网。
2. 小程序首页点击服务器地址，填写 http://电脑LAN-IP:8765。
3. 点击保存后应显示“已连接”。

### 方案 B：USB 网络共享（无公网/无同网时）

1. 安卓手机连接 USB，开启 USB 网络共享。
2. 查询电脑在 USB 网段的 IPv4（通常类似 192.168.137.x）。
3. 小程序服务器地址填写 http://该IPv4:8765。
4. 注意不能使用 127.0.0.1。

### 小程序页面内置地址预设

在首页“修改服务器地址”弹窗可直接选择：

- 局域网 IP
- USB 共享网络 IP
- 临时隧道地址

保存后会写入本地缓存，后续调试自动复用。

## 转写引擎优先级

| 优先级 | 引擎 | 说明 |
|-------|------|------|
| 1 | faster-whisper | 本地优先；device=auto 时优先尝试 CUDA，失败降 CPU |
| 2 | GLM-ASR-2512 | 配置 ZHIPU_API_KEY 后可兜底 |
| 3 | whisper.cpp CLI | 本地安装 whisper-cli 时自动尝试 |
| 4 | phi3-fallback | 仅返回调试提示 |

## 常见问题

Q: 手机显示未连接？

- 确认后端监听 0.0.0.0:8765。
- 检查 Windows 防火墙是否放行 8765 入站。
- 检查小程序地址是否为电脑可达 IPv4（非 localhost/127.0.0.1）。
- 微信开发者工具调试时关闭域名校验。

Q: health 返回 gpu_available=false？

- 本地 GPU 初始化失败后会自动降级 CPU。
- 可先用 CPU 路径演示，或调整 LOCAL_ASR_MODEL_SIZE 进一步降低负载。

Q: 仍然走远程引擎？

- 检查 local_asr_ready 字段是否为 true。
- 若 false，查看 local_asr_error 字段定位依赖/驱动问题。
