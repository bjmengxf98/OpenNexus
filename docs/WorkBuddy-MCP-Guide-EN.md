# Connecting WorkBuddy to OpenNexus MCP

[简体中文](WorkBuddy-MCP接入指南.md) · [English](WorkBuddy-MCP-Guide-EN.md)

MCP lets an external client invoke OpenNexus business capabilities. It does not turn WorkBuddy into the OpenNexus UI, and OpenNexus cannot use this connection to control WorkBuddy proactively.

## Server Deployment

1. Deploy the complete OpenNexus repository. Never upload a local `data/app.db`, `.env`, or logs.
2. Install `requirements.txt` in the production virtual environment.
3. Fully restart the Python service so the MCP token and audit tables can be initialized.
4. Forward `/mcp/` from the HTTPS reverse proxy to the FastAPI service.
5. Increase the reverse-proxy upload limit when large remote attachments are required.

## Create a Token

1. Sign in to OpenNexus and open **Settings → MCP Access**.
2. Create a purpose-specific token such as `WorkBuddy`.
3. Copy the complete token immediately; its plaintext is shown only once.
4. Use a separate token for every user, client, and environment.

## WorkBuddy Configuration

Add a Streamable HTTP MCP server:

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

For local testing, use `http://127.0.0.1:8000/mcp/`. A phone or another computer must use an HTTPS address that can reach the server; `localhost` refers to that device itself.

The tool count may change between releases and should not be used as a fixed connectivity check.

## Troubleshooting

- **Authentication required:** verify `Authorization`, the `Bearer` prefix, one space, and the complete token.
- **Zero tools:** check the trailing `/mcp/`, restart the service, and verify Streamable HTTP proxy support.
- **Works locally but not on mobile:** replace `127.0.0.1` with an accessible LAN or public HTTPS address.
- **Copied token does not work:** the abbreviated value in the token list cannot restore the original token; revoke it and create a new one.

## Security

- An MCP token carries the business permissions of its OpenNexus user.
- Revoke a suspected token immediately.
- Confirm targets before deletes, bulk updates, or outbound messages.
- MCP calls should be recorded in the audit log.
- Never publish real tokens, domains, business data, or private logs in issues or examples.
