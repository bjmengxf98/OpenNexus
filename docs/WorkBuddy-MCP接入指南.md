# WorkBuddy 接入 OpenNexus MCP

[简体中文](WorkBuddy-MCP接入指南.md) · [English](WorkBuddy-MCP-Guide-EN.md)

MCP 是外部客户端调用 OpenNexus 业务能力的接口。它不会把 WorkBuddy 变成 OpenNexus 前端，也不会让 OpenNexus 主动控制 WorkBuddy。

## 服务器部署

1. 按完整仓库部署 OpenNexus，不要上传本地 `data/app.db`、`.env` 或日志。
2. 在生产虚拟环境安装 `requirements.txt`。
3. 完整重启 Python 服务；系统会自动创建 MCP 令牌与审计所需的数据表。
4. 确认反向代理将 `/mcp/` 转发到 FastAPI 服务，并使用 HTTPS。
5. 如需处理较大远程附件，应同步调整反向代理上传限制，例如 Nginx 的 `client_max_body_size`。

## 创建访问令牌

1. 登录 OpenNexus，进入“设置 → MCP 接入”。
2. 创建一个用途明确的令牌，例如 `WorkBuddy`。
3. 立即复制完整令牌；明文只显示一次。
4. 每个用户、客户端和环境应使用不同令牌，不要多人共用。

## WorkBuddy 配置

在 WorkBuddy 打开“专家 · 技能 · 连接器 → MCP 服务管理 → 配置 MCP”，新增 Streamable HTTP 服务：

```json
{
  "mcpServers": {
    "opennexus": {
      "type": "http",
      "url": "https://your-domain.example/mcp/",
      "headers": {
        "Authorization": "Bearer onx_mcp_REPLACE_WITH_YOUR_TOKEN"
      },
      "disabled": false
    }
  }
}
```

本机测试可将 URL 改为 `http://127.0.0.1:8000/mcp/`。手机端或另一台电脑必须使用能够访问服务器的 HTTPS 地址，不能填写 `localhost`。

保存后刷新连接，客户端应能列出 OpenNexus 工具。具体工具数量会随版本变化，不应以固定数字判断连接是否正常。

## 故障排查

- “需要认证”：检查 `Authorization` 是否为 `Bearer`、空格和完整令牌的组合。
- 工具数量为零：确认末尾 `/mcp/`、服务已重启，并检查反向代理是否支持流式 HTTP。
- 本机可用但手机不可用：`127.0.0.1` 只代表当前设备，应改用公网或局域网 HTTPS 地址。
- 令牌复制后无效：令牌明文只在创建时保存；列表中的缩略值不能还原完整令牌，应撤销后重新创建。

## 安全说明

- MCP 令牌代表对应 OpenNexus 用户的业务权限。
- 怀疑泄露时立即在设置页撤销，旧令牌会失效。
- 删除、批量更新和外发消息等操作执行前应核对目标。
- 所有 MCP 调用均应进入审计日志。
- 不要在 Issue、截图、聊天记录或示例配置中提交真实令牌、域名和业务数据。
