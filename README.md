# 微信小程序 ASR 语音转文字系统

## 项目结构

```
F:\Robots\team66\asr\
├── backend\                # Python 后端桥接服务
│   ├── server.py           # Python HTTP 主服务（兼容实现）
│   ├── requirements.txt    # Python 依赖
│   └── start.bat           # Windows 一键启动脚本
├── miniprogram\            # 微信小程序源码
│   ├── app.js              # 全局配置
│   ├── app.json            # 页面路由
│   ├── app.wxss            # 全局样式
│   ├── project.config.json # 开发工具配置
│   ├── pages\
│   │   ├── index\          # 录音转写主页
│   │   └── history\        # 历史记录页
│   └── sitemap.json
└── output\                 # 转写结果存储目录（自动创建）
    └── asr20260608112030.txt  # 示例输出文件
```

## 整体架构

```
手机微信小程序
    │
    │  wx.uploadFile（音频文件）
    ▼
本地 Python HTTP 服务（端口 8765）
    │
    ├─► Ollama Whisper API → 返回转写文本
    ├─► whisper.cpp CLI（备选）
    └─► phi3 占位模式（降级）
    │
    ▼
F:\Robots\team66\asr\output\asr{时间戳}.txt
```

说明：当前版本后端已实现为 Python 标准库 HTTP 服务，接口保持不变（`/health`、`/transcribe`、`/records`）。

## 快速开始

### 第一步：安装 Ollama 和 Whisper 模型

```bash
# 1. 下载 Ollama：https://ollama.ai/download
# 2. 尝试拉取 whisper 模型（部分版本仓库可能不可用）
ollama pull whisper
# 3. 可选：拉取 phi3 备用模型
ollama pull phi3
```

如果 `ollama pull whisper` 返回 `file does not exist`，说明当前模型仓库无该标签。此时系统仍可运行并使用 `phi3-fallback` 路径返回占位结果。

### 第二步：启动后端服务

```bash
# 方式 A：双击运行
backend\start.bat

# 方式 B：命令行
cd backend
pip install -r requirements.txt
python server.py
```

建议（PowerShell + 虚拟环境）：

```powershell
f:/Robots/team66/asr/.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
f:/Robots/team66/asr/.venv/Scripts/python.exe backend/server.py
```

服务启动后访问：http://localhost:8765/health 检查状态

### 第三步：配置小程序

1. 打开微信开发者工具
2. 导入 `miniprogram\` 目录
3. 在 `app.js` 中修改服务器 IP：
   ```js
   serverBase: 'http://192.168.x.x:8765',  // 改为你的电脑 IP
   ```
4. 在小程序管理后台添加 request 合法域名（真机调试时需要）
5. 扫码预览或真机调试

### 第四步：获取本机 IP

```bash
# Windows
ipconfig | findstr IPv4
```

PowerShell 中建议使用以下方式检查后端健康状态：

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8765/health | ConvertTo-Json -Depth 6
```

## 转写引擎优先级

| 优先级 | 引擎 | 说明 |
|-------|------|------|
| 1 | Ollama Whisper | `ollama pull whisper` 后自动使用 |
| 2 | whisper.cpp CLI | 安装 whisper-cli 后自动使用 |
| 3 | phi3 占位 | 无 Whisper 时的降级提示 |

## 输出文件格式

文件名格式：`asr{年月日时分秒}.txt`，例如：`asr20260608112030.txt`

文件内容：
```
转写时间：2026-06-08 11:20:30
转写引擎：ollama-whisper
原始文件：record_1234567890.aac
────────────────────────────────────────
（转写的文字内容）
```

## API 接口文档

| 接口 | 方法 | 说明 |
|------|------|------|
| /health | GET | 健康检查，返回 Ollama 连接状态 |
| /transcribe | POST | 上传音频文件，返回转写结果 |
| /records | GET | 获取历史转写记录列表 |

## 常见问题

**Q: 小程序无法连接后端？**
- 手机和电脑需在同一 WiFi
- 关闭电脑防火墙或开放 8765 端口
- 使用局域网 IP，不要用 localhost

**Q: Whisper 模型转写准确率低？**
- 录音环境尽量安静
- 录音时靠近手机麦克风
- 可尝试更大的 whisper 模型：`ollama pull whisper:large`

**Q: 提示"phi3-fallback"？**
- 说明 Whisper 未安装，运行 `ollama pull whisper`
- phi3 是语言模型，**不能**处理音频，仅作状态提示

**Q: 启动后端时无输出直接退出？**
- 本机若使用 Python 3.14 alpha，部分依赖可能不兼容
- 当前仓库已采用兼容版 `backend/server.py`，不依赖 FastAPI 也可运行
- 优先建议使用稳定版 Python 3.11/3.12 进行长期开发

## 已验证记录（2026-06-08）

- 已执行并通过：`/health`、`/transcribe`、`/records`
- 本机 IPv4：`172.18.1.79`
- 运行日志：`output/execution-log-20260608.md`
- 示例输出：`output/asr20260608070742.txt`

**Q: 小程序审核时 request 域名被拒？**
- 在微信公众平台 → 开发 → 开发设置 → 服务器域名，添加你的后端域名（需 HTTPS）
- 开发调试阶段可在开发者工具勾选"不校验合法域名"
