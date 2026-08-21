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

## 5. Department Dashboard

The dashboard contains department overview, daily progress, task, and project views. Daily snapshots can be browsed by date. **Refresh Data** updates the local cache from WPS; **Regenerate Intelligent Analysis** asks the configured model to rebuild the narrative analysis.

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
