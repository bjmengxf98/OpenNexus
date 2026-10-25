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
2. Set a purpose-specific client name such as `WorkBuddy`, choose an expiry, and explicitly select its permissions. New tokens default to read-only.
3. Copy the complete token immediately; its plaintext is shown only once.
4. Use a separate token for every user, client, and environment.

Authorization happens once at token creation. In-scope tool calls execute directly without a separate OpenNexus approval. Deletion requires `delete_data`; destructive structure operations also require `manage_structure`. Out-of-scope calls are rejected. Existing full-access tokens remain full-access without per-call approval; old limited tokens do not gain deletion permission automatically. Old approval records are history only, and old approval IDs cannot replay actions. Refresh the client's tool list after upgrading. WorkBuddy's own confirmation settings are independent of the server.

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

## Shared Business Execution

WorkBuddy owns reasoning and presentation; it does not invoke an additional OpenNexus chat-model loop. Equivalent record mutations share execution and readback contracts, not necessarily wording or reasoning.

- Generated documents locate the authenticated user's own `AI附件` from that user's configured files, independently of the source table's creator. Each user still needs at least one self-owned configured WPS file to locate their personal drive.
- `create_records`, `update_records` and `delete_records` perform one mutation and bounded readback. Only `verified=true` confirms observed field values or absence of target IDs; absence does not prove a record previously existed. `write_submitted=true` means accepted, while `null` means unknown. Unverified outcomes require reading before deciding, not automatic mutation retries.
- Each read phase is bounded to five pages and 20 seconds. A complete pre-create identity snapshot supports responses without created IDs; incomplete or ambiguous deltas remain unverified. This is not a transaction snapshot or cross-request idempotency key. Unsupported value representations and failed reads remain explicitly unconfirmed.
- Use `analyze_records`, necessary fields, filters and `next_row_offset` for compact large-table continuation; check `source_is_complete`. Chat-private `read_record_window` and `analyze_cached_records` are no longer advertised over MCP. Old calls explain the replacement. Refresh the client tool list after upgrading.
- Structural and ordinary-spreadsheet cell operations retain their existing receipts, not the new record-level verification guarantee. OpenNexus chat attachments and history are not implicitly transferred to WorkBuddy. These contract tests do not represent complete end-to-end acceptance of every instruction on both clients.

## Troubleshooting

- **Authentication required:** verify `Authorization`, the `Bearer` prefix, one space, and the complete token.
- **Zero tools:** check the trailing `/mcp/`, restart the service, and verify Streamable HTTP proxy support.
- **Works locally but not on mobile:** replace `127.0.0.1` with an accessible LAN or public HTTPS address.
- **Copied token does not work:** the abbreviated value in the token list cannot restore the original token; revoke it and create a new one.

## Security

- An MCP token is limited by both its owner's business access and its explicitly granted permissions.
- Revoke a suspected token immediately.
- Confirm targets before deletes, bulk updates, or outbound messages.
- MCP calls should be recorded in the audit log.
- Never publish real tokens, domains, business data, or private logs in issues or examples.
