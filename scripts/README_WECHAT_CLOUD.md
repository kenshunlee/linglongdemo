# 微信云开发自动化辅助脚本

## 1) 预检查（CLI 连通性）

```powershell
powershell -ExecutionPolicy Bypass -File scripts/wechat-cloud-precheck.ps1
```

若出现 `IDE service port disabled` 或 `wait IDE port timeout`：

1. 打开微信开发者工具
2. 进入 `设置 -> 安全设置`
3. 开启 `服务端口`
4. 重启开发者工具并重新登录
5. 重新执行上面的预检查脚本

## 2) 后续步骤提示

```powershell
powershell -ExecutionPolicy Bypass -File scripts/wechat-cloud-next-steps.ps1 -GatewayDomain "https://你的域名" -GatewayPrefix "/asr"
```

说明：当前 CLI 可以覆盖云环境/云函数，但云托管与微信网关绑定通常需要在控制台手动完成。
