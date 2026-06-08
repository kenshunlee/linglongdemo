# 微信小程序 ASR 本机 Demo（LAN/USB 可达）

本项目用于临时 Demo：后端运行在本机，优先使用本地 GPU 算力；安卓真机小程序通过 LAN 或 USB 网络共享访问本机服务。

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
```

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
