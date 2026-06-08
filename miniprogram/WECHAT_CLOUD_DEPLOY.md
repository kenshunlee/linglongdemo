# 微信云开发 + 微信网关部署说明

本文用于将 `backend/` 服务部署到微信云开发（云托管），并通过微信网关提供 HTTPS 域名给小程序调用。

## 0. 前置条件

- 已在微信开发者工具打开 `miniprogram/` 项目
- 小程序 AppID: `wx84280c27da92b5a2`
- 已开通云开发环境（如未开通，按下文创建）

## 1. 解决 CLI 端口超时问题（你当前已遇到）

微信开发者工具中手动执行：

1. 打开 `设置 -> 安全设置`
2. 开启 `服务端口`
3. 重启微信开发者工具
4. 确认已登录对应小程序账号

可用以下命令验证（PowerShell）：

```powershell
& 'C:\Program Files (x86)\Tencent\微信web开发者工具\cli.bat' islogin
& 'C:\Program Files (x86)\Tencent\微信web开发者工具\cli.bat' cloud env list --project f:/Robots/team66/asr/miniprogram
```

## 2. 云开发环境创建

在微信开发者工具：

1. 点击 `云开发`
2. 点击 `开通`（若未开通）
3. 创建环境并记录 `环境 ID`（例如 `prod-xxxxxx`）

## 3. 部署后端到云托管

本项目已准备好云托管容器文件：

- `backend/Dockerfile`
- `backend/.dockerignore`

在微信云开发控制台（或开发者工具云托管页面）执行：

1. 新建 `云托管` 服务（如 `asr-backend`）
2. 代码源选择本地目录 `backend/`
3. 端口填写 `8765`
4. 环境变量建议：

```text
ASR_HOST=0.0.0.0
PORT=8765
ASR_OUTPUT_DIR=/tmp/output
OLLAMA_BASE_URL=http://127.0.0.1:11434
WHISPER_MODEL=whisper
FALLBACK_MODEL=phi3
```

说明：云托管里通常没有 Ollama，本服务会自动走降级路径（`phi3-fallback` 或返回错误提示）。如果要真实 ASR，请将转写引擎改为云端可用服务。

## 4. 配置微信网关路由

在云开发控制台的 `微信网关` 中：

1. 新建网关
2. 绑定后端服务 `asr-backend`
3. 配置路由前缀（建议 `/asr`）
4. 将以下接口映射到云托管服务：
   - `GET /asr/health`
   - `POST /asr/transcribe`
   - `GET /asr/records`

## 5. 配置服务器域名（HTTPS）

在微信网关中绑定可访问域名（例如 `https://api.yourdomain.com`）。

然后在小程序后台配置合法域名：

1. 登录微信公众平台
2. 进入 `开发 -> 开发设置 -> 服务器域名`
3. 将 `https://api.yourdomain.com` 添加到 `request 合法域名`

## 6. 更新小程序请求地址

修改 `miniprogram/app.js` 中的 `serverBase`：

```js
serverBase: 'https://api.yourdomain.com/asr'
```

## 7. 验证

部署完成后在本机执行：

```powershell
curl.exe -i https://api.yourdomain.com/asr/health
```

小程序真机侧验证：

1. 打开首页
2. 触发健康检查
3. 上传音频并查看 `history` 页面

## 8. 备注

- 当前 CLI 仅覆盖云环境/云函数能力，云托管与微信网关配置建议在控制台完成。
- 若后续你提供环境 ID 和网关域名，我可以继续把 `app.js` 与 README 全部替换成生产配置并做联调检查清单。
