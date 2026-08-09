/**
 * FastAPI 适配器 - 替代 Claude SDK，直接调用部门管理助手的 FastAPI 接口
 */
import { logger } from "../logger.js";

export interface QueryOptions {
  prompt: string;
  cwd: string;
  resume?: string;
  model?: string;
  systemPrompt?: string;
  permissionMode?: "default" | "acceptEdits" | "plan" | "bypassPermissions";
  images?: Array<{
    type: "image";
    source: { type: "base64"; media_type: string; data: string };
  }>;
  onPermissionRequest?: (toolName: string, toolInput: string) => Promise<boolean>;
  onText?: (text: string) => Promise<void> | void;
  onThinking?: (summary: string) => Promise<void> | void;
  abortController?: AbortController;
  // FastAPI 专用参数
  apiUrl?: string;
  apiToken?: string;
  weixinId?: string;
}

export interface QueryResult {
  text: string;
  sessionId: string;
  error?: string;
}

export async function claudeQuery(options: QueryOptions): Promise<QueryResult> {
  const {
    prompt,
    apiUrl = "http://localhost:8000",
    apiToken = "",
    weixinId = "",
    images,
    onText,
    abortController,
  } = options;

  if (!weixinId) {
    return {
      text: "",
      sessionId: "",
      error: "Missing weixinId",
    };
  }

  logger.info("Calling FastAPI", { apiUrl, weixinId, hasImages: !!images?.length });

  try {
    // 构建请求体，包含图片
    const body: any = {
      weixin_id: weixinId,
      text: prompt,
      token: apiToken,
    };

    // 如果有图片，转换为 FastAPI 接受的格式
    if (images && images.length > 0) {
      body.images = images.map(img => ({
        media_type: img.source.media_type,
        data: img.source.data,
      }));
    }

    const response = await fetch(`${apiUrl}/api/weixin/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: abortController?.signal,
    });

    if (!response.ok) {
      const errorText = await response.text();
      logger.error("FastAPI error", { status: response.status, error: errorText });
      return {
        text: "",
        sessionId: "",
        error: `HTTP ${response.status}: ${errorText}`,
      };
    }

    const data = await response.json();
    const reply = data.reply || "";

    // 流式输出（模拟）
    if (onText && reply) {
      await onText(reply);
    }

    logger.info("FastAPI query completed", { replyLength: reply.length });

    return {
      text: reply,
      sessionId: weixinId, // 用 weixinId 作为 sessionId
      error: undefined,
    };
  } catch (err: unknown) {
    const errorMessage = err instanceof Error ? err.message : String(err);
    logger.error("FastAPI query threw", { error: errorMessage });
    return {
      text: "",
      sessionId: "",
      error: errorMessage,
    };
  }
}
