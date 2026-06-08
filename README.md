# 微信小程序 ASR 语音转文字系统

## 项目结构

```
F:/Robots/team66/asr/
├── backend/                # Python 后端桥接服务
│   ├── server.py           # Python HTTP 主服务（stdlib 实现）
│   ├── requirements.txt    # Python 依赖
│   ├── start.bat           # Windows 一键启动脚本
│   ├── cloud.env           # 云托管环境变量（本地私有）
│   └── cloud.env.example   # 云托管环境变量模板
├── miniprogram/            # 微信小程序源码
│   ├── app.js              # 全局配置
│   ├── app.json            # 页面路由
│   ├── app.wxss            # 全局样式
│   ├── project.config.json # 开发工具配置
│   ├── pages/
│   │   ├── index/          # 录音转写主页
│   │   └── history/        # 历史记录页
│   └── sitemap.json
└── output/                 # 转写结果存储目录（自动创建）
```

## 整体架构

```
手机微信小程序
    │
    │  wx.uploadFile（音频文件）
    ▼
本地 Python HTTP 服务（端口 8765）
    │
    ├─► 智谱 GLM-ASR-2512（主引擎）
    ├─► whisper.cpp CLI（备选）
    └─► phi3 占位模式（降级）
    │
    ▼
output/asr{时间戳}.txt
```

后端接口保持不变：`/health`、`/transcribe`、`/records`。

## 快速开始

### 第一步：安装依赖

```powershell
f:/Robots/team66/asr/.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
```

### 第二步：配置智谱 ASR

在 `backend/cloud.env` 或系统环境变量中配置：

```env
ZHIPU_API_KEY=your_zhipu_api_key
ZHIPU_BASE_URL=https://open.bigmodel.cn/api/paas/v4
ZHIPU_ASR_MODEL=glm-asr-2512
```

### 第三步：启动后端服务

```powershell
f:/Robots/team66/asr/.venv/Scripts/python.exe backend/server.py
```

或直接双击：`backend/start.bat`

服务启动后访问：`http://127.0.0.1:8765/health`

### 第四步：配置小程序

1. 打开微信开发者工具。
2. 导入 `miniprogram/` 目录。
3. 在 `miniprogram/app.js` 设置后端地址：

```js
serverBase: 'http://你的电脑局域网IP:8765'
```

4. 真机调试时，确保手机和电脑在同一 WiFi。
5. 发布前请配置合法 HTTPS 域名。

## 健康检查示例

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8765/health | ConvertTo-Json -Depth 6
```

期望关键字段：

- `asr_provider`: `zhipu`
- `asr_model`: `glm-asr-2512`
- `zhipu_configured`: `true`

## 转写引擎优先级

| 优先级 | 引擎 | 说明 |
|-------|------|------|
| 1 | GLM-ASR-2512 | 已配置 `ZHIPU_API_KEY` 时使用 |
| 2 | whisper.cpp CLI | 本地安装 whisper-cli 时自动尝试 |
| 3 | phi3 占位 | 上述不可用时返回降级提示 |

## 输出文件格式

文件名格式：`asr{年月日时分秒}.txt`

文件内容示例：

```
转写时间：2026-06-08 11:20:30
转写引擎：glm-asr-2512
原始文件：record_1234567890.aac
────────────────────────────────────────
（转写的文字内容）
```

## API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查，返回引擎状态 |
| `/transcribe` | POST | 上传音频文件，返回转写结果 |
| `/records` | GET | 获取历史转写记录列表 |

## 常见问题

**Q: 小程序无法连接后端？**

- 手机和电脑需在同一 WiFi。
- 检查小程序中 `serverBase` 是否使用电脑局域网 IP。
- 检查 8765 端口是否可访问。

**Q: 返回 `phi3-fallback` 或 `none`？**

- 通常是 `ZHIPU_API_KEY` 未配置或网络不可达。
- 先检查 `/health` 中 `zhipu_configured` 是否为 `true`。
- 再检查服务器访问智谱 API 的网络权限。

**Q: 启动后端时无输出直接退出？**

- Python 3.14 alpha 可能出现部分包兼容性问题。
- 当前仓库后端采用 stdlib HTTP 实现，兼容性较好。
- 生产建议使用 Python 3.11/3.12。

## 安全提示

- `backend/cloud.env` 含敏感密钥，不要提交到公开仓库。
- 若密钥曾暴露，请立即在智谱控制台更换。
