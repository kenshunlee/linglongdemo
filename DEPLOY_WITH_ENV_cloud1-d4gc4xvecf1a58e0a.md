# 部署到微信云开发（环境 cloud1-d4gc4xvecf1a58e0a）

已确认：CLI 登录可用，环境命令可带 env 参数执行。

## 1. 云托管部署后端

在微信开发者工具 -> 云开发 -> 云托管：

1. 选择环境：`cloud1-d4gc4xvecf1a58e0a`
2. 新建服务（建议名：`asr-backend`）
3. 代码目录选择：`backend/`
4. 端口：`8765`
5. 环境变量参考：`backend/cloud.env.example`

建议环境变量：

```text
ASR_HOST=0.0.0.0
PORT=8765
ASR_OUTPUT_DIR=/tmp/output
OLLAMA_BASE_URL=http://127.0.0.1:11434
WHISPER_MODEL=whisper
FALLBACK_MODEL=phi3
```

## 2. 微信网关配置

在同一环境的微信网关：

1. 绑定服务 `asr-backend`
2. 路由前缀：`/asr`
3. 路由：
   - `GET /asr/health`
   - `POST /asr/transcribe`
   - `GET /asr/records`
4. 绑定 HTTPS 域名（例如 `https://api.example.com`）

## 3. 小程序合法域名

微信公众平台 -> 开发 -> 开发设置 -> 服务器域名：

- 将 `https://api.example.com` 加入 request 合法域名

## 4. 小程序请求地址

修改 `miniprogram/app.js` 中 `serverBase`：

```js
serverBase: 'https://api.example.com/asr'
```

## 5. 验证

```powershell
curl.exe -i https://api.example.com/asr/health
```

如果返回 200 且 JSON 中 status=ok，则网关联通。
