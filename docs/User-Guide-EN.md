# OpenNexus User Guide

[简体中文](用户帮助.md) · [English](User-Guide-EN.md)

OpenNexus is a self-hosted AI workspace for WPS multidimensional spreadsheets, team operations, knowledge, reminders, messaging, and MCP access.

## 1. Five-Minute Start

### Register and sign in

Open the deployed address, create an account if registration is enabled, and wait for administrator approval when required. Browser password managers may store the password; OpenNexus only remembers the email address in local browser storage.

### Connect WPS

Open **Settings → WPS multidimensional spreadsheets**, configure one or more files, and complete WPS OAuth authorization. The green WPS indicator confirms a usable connection. A disconnected WPS account does not prevent general AI conversation, but spreadsheet operations will be unavailable.

### Work in natural language

Examples:

- `Summarize today's progress and list missing submissions.`
- `Create a P1 task for Zhang San, due August 15.`
- `Which projects are overdue?`
- `Remind me tomorrow morning to bring the meeting material.`

Always review the returned record, event time, reminder time, recipient, and write result.

## 2. Main Interface

The left sidebar organizes conversations by time and lets you create, rename, switch, or delete a topic. Keeping separate topics for projects, reminders, and policy research improves context quality.

The `+` menu supports file upload, attachment mode, conversation clearing, and uploaded-file removal. Images can also be pasted directly into the input box.

The top bar provides the current WPS file selector, connection status, dashboard, help, settings, administration, and sign-out actions. On mobile, less frequent actions move into the drawer.

## 3. WPS Data Operations

OpenNexus can query, summarize, create, update, and delete records; manage sheets and fields; and work with supported WPS structures. It reads the schema before complex operations and translates record links into business names when possible.

Examples:

- `Summarize P0 and P1 tasks due this week.`
- `Create a task named Complete quarterly report, owner Zhang San, due August 15.`
- `Change the deadline of the airport archive task to August 20.`

For destructive or bulk operations, state the target precisely and verify the preview/result.

## 4. Daily Progress, Tasks, Projects, and Leave

The assistant understands business concepts instead of exposing only raw record IDs. It can analyze daily submissions, task priorities and deadlines, project owners and status, and leave periods.

Examples:

- `Who has not submitted progress today?`
- `Show overdue tasks grouped by owner.`
- `What is the current status of Project A?`
- `Who is on leave today?`

## 5. Business Intelligence Dashboard

The overview works with any selected WPS multidimensional spreadsheet. It first shows a model-free basic overview. After the user clicks **Redesign and Analyze** and confirms, the configured model selects suitable indicators, charts, and detail tables from the real worksheet names and field schema. The server accepts only existing fields and allowlisted aggregations; every displayed number is calculated locally from cached WPS records. A validated design is cached per user, file, model, and schema and is regenerated after a schema change or an explicit redesign.

The overview is a management summary rather than a field inventory: it keeps at most four KPIs and six indicators in total, removes duplicates, and rejects IDs, account numbers, employee numbers, phone numbers, numeric-looking text, selections, and configuration switches as numeric metrics. WPS record-link IDs are resolved to business labels from the same cached file where possible; cascade fields show their business hierarchy without internal markers such as `Common`.

Dashboard design sends worksheet and field metadata plus fetched counts, not record bodies, to the configured model. Narrative generation for the current page still uses the summarized page data described in the confirmation dialog. Built-in dashboards, freeform paper, instruction pages, and other non-record pages are safely skipped and reported. Daily progress, task, and project tabs remain specialized views that appear only for matching data worksheets; other business files use the model-designed overview. **Refresh Data** updates the local cache from WPS.

Switching files returns to the overview. When opening Daily Progress without a manually chosen date, the dashboard selects the latest cached date for that file. A first read of a new file still requires WPS synchronization; later switches reuse local data where possible. Previously empty daily snapshots are rechecked after cache updates or expiry, without substituting records from another date. Refreshing a specialized view reads only its relevant worksheet(s). If a switched file fails to load, the previous file's dashboard remains hidden so it cannot be mistaken for current data.

A worksheet is paged up to 5,000 records. If WPS cannot provide a continuation token or the limit is reached, the dashboard labels the results as partial; fetched counts must not be interpreted as full-table totals.

## 6. Smart Reminders

OpenNexus distinguishes the event time from the notification time.

- An explicit reminder such as `Remind me at 8:00 tomorrow` uses the specified time.
- A meeting time without a reminder time normally gets a reasonable advance reminder.
- Travel-related events consider route, transport, preparation, and buffer time; missing departure details should trigger a clarification.

After creation, verify both times. You can list, adjust, or cancel reminders in natural language. Delivery failures remain pending for retry and must not be reported as successful.

## 7. Notifications and Personal WeChat

Personal-WeChat binding is available under **Settings → Personal WeChat**. One OpenNexus account should bind one personal-WeChat account. This bridge is experimental and may be affected by platform rules or token expiry.

WeCom and WPS messaging can be configured independently. Always confirm recipients before sending external messages.

## 8. Files, Images, and Documents

OpenNexus supports common document, spreadsheet, text, PDF, and image formats. An uploaded image is marked for image recognition; Word/PDF and other documents are marked for document processing. Files may be analyzed by the AI or uploaded as WPS record attachments, depending on the selected mode.

Generated documents should be reviewed before external distribution.

## 9. Knowledge and Memory

Administrators can upload policies, templates, and workflows to the shared knowledge base. With embedding configured, documents are chunked and retrieved semantically during relevant conversations.

Memory is isolated by user and purpose: personal preferences and stable facts can apply across topics; topic memory applies only to its conversation; and business rules can be bound to one connected WPS data source. Relevant history retrieval uses only the current user's own statements and never treats previous AI replies as facts.

WPS remains the source of truth for mutable business status such as tasks, projects, progress, and leave records. OpenNexus re-queries WPS for current status instead of storing those values as long-term memory. Do not use memory for passwords, tokens, or unnecessary sensitive personal data.

## 10. Settings

Settings cover WPS files and account ID, primary and image models, display theme and font size, WeCom, personal WeChat, MCP access, account security, profile, and feedback.

Keep **Smart tool selection and Token saving** enabled for the primary model unless compatibility troubleshooting requires the legacy full-tool mode. Large-table analysis reads every required page, performs filtering and aggregation locally, and returns compact row windows for semantic review. Each new assistant reply shows Token usage, model rounds, tool calls, and tool-schema savings. If the provider omits exact usage, the value is marked as estimated. Context-limit errors trigger automatic compaction and retry without claiming that incomplete data is complete.

API keys are deployment secrets. Never include them in screenshots or public issues.

## 11. WorkBuddy and MCP

Create a token under **Settings → MCP Access**, copy it once, and configure a Streamable HTTP connection in WorkBuddy. See [WorkBuddy-MCP-Guide-EN.md](WorkBuddy-MCP-Guide-EN.md).

MCP is a one-way business interface from the client to OpenNexus. Revoke a token immediately if it may have leaked.

## 12. Administration

Administrators can manage users, roles, feedback, the shared knowledge base, spreadsheet change logs, WeCom, and WPS application settings. Secret fields must not be returned in plaintext by the administration API.

## 13. Common Problems

- **WPS disconnected or expired:** reconnect WPS and confirm the OAuth callback URL.
- **Wrong file:** check the top file selector and the default file setting.
- **AI says complete but data did not change:** verify the tool result and refresh WPS; a textual claim is not proof of a successful write.
- **Reminder not received:** check reminder status, channel binding, bridge process, and retry logs.
- **Personal WeChat failed:** verify that the bound account matches the running bridge process and rebind if the session expired.
- **Image/PDF not recognized:** configure a supported vision model and ensure the upload type is correct.
- **Dashboard is stale:** use Refresh Data and inspect WPS authorization/cache logs.
- **MCP needs authentication:** use the complete one-time token with the `Bearer` prefix.
- **Model timeout:** try a faster model and verify network/base URL configuration.

## 14. Security

- Never share `.env`, databases, backups, logs, cookies, QR codes, or tokens.
- Use HTTPS and a unique `SESSION_SECRET` in production.
- Give every MCP client its own revocable token.
- Remove real names and business data before sharing screenshots or issues.
- Back up runtime data privately and test restoration.

Use the in-app feedback form or a sanitized GitHub issue for general feedback. Security vulnerabilities must follow [SECURITY_EN.md](../SECURITY_EN.md).
