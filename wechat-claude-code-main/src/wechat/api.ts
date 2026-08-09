import type {
  GetUpdatesResp,
  SendMessageReq,
  GetUploadUrlResp,
} from './types.js';
import { logger } from '../logger.js';

/** Generate a random uint32 and return its base64 representation. */
function generateUin(): string {
  const buf = new Uint8Array(4);
  crypto.getRandomValues(buf);
  const view = new DataView(buf.buffer);
  const uint32 = view.getUint32(0, false); // big-endian
  return Buffer.from(buf).toString('base64');
}

export class TokenExpiredError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'TokenExpiredError';
  }
}

export class WeChatApi {
  private readonly token: string;
  private readonly baseUrl: string;
  private readonly uin: string;

  constructor(token: string, baseUrl: string = 'https://ilinkai.weixin.qq.com') {
    if (baseUrl) {
      try {
        const url = new URL(baseUrl);
        const allowedHosts = ['weixin.qq.com', 'wechat.com'];
        const isAllowed = allowedHosts.some(h => url.hostname === h || url.hostname.endsWith('.' + h));
        if (url.protocol !== 'https:' || !isAllowed) {
          logger.warn('Untrusted baseUrl, using default', { baseUrl });
          baseUrl = 'https://ilinkai.weixin.qq.com';
        }
      } catch {
        logger.warn('Invalid baseUrl, using default', { baseUrl });
        baseUrl = 'https://ilinkai.weixin.qq.com';
      }
    }
    this.token = token;
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.uin = generateUin();
  }

  private headers(): Record<string, string> {
    return {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${this.token}`,
      'AuthorizationType': 'ilink_bot_token',
      'X-WECHAT-UIN': this.uin,
    };
  }

  private async request<T = Record<string, unknown>>(
    path: string,
    body: unknown,
    timeoutMs: number = 15_000,
  ): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    const url = `${this.baseUrl}/${path}`;

    logger.debug('API request', { url, body });

    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: this.headers(),
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (!res.ok) {
        const text = await res.text();
        throw new Error(`HTTP ${res.status}: ${text}`);
      }

      const json = (await res.json()) as T;
      logger.debug('API response', json);
      return json;
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        throw new Error(`Request to ${url} timed out after ${timeoutMs}ms`);
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  /** Long-poll for new messages. Timeout 35s for long-polling. */
  async getUpdates(buf?: string): Promise<GetUpdatesResp> {
    return this.request<GetUpdatesResp>(
      'ilink/bot/getupdates',
      buf ? { get_updates_buf: buf } : {},
      35_000,
    );
  }

  /** Send a message to a user. Supports both old `ret` and current responses. */
  async sendMessage(req: SendMessageReq): Promise<void> {
    const MAX_RETRIES = 3;
    const TOKEN_EXPIRED_CODES = [11, -13, -14];
    let delay = 10_000;
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      const res = await this.request<{
        ret?: number;
        retmsg?: string;
        errcode?: number;
        errmsg?: string;
        message_id?: number | string;
      }>('ilink/bot/sendmessage', req);
      const code = res.errcode ?? res.ret;
      const errorText = res.errmsg ?? res.retmsg ?? '';
      if (code !== undefined && TOKEN_EXPIRED_CODES.includes(code)) {
        throw new TokenExpiredError(`sendMessage token expired: code=${code} ${errorText}`);
      }
      if (code === -2) {
        if (attempt === MAX_RETRIES) {
          logger.warn('sendMessage rate-limited after max retries', { attempts: MAX_RETRIES });
          throw new Error(`sendMessage rate-limited after ${MAX_RETRIES + 1} attempts`);
        }
        logger.warn('sendMessage rate-limited (ret:-2), retrying', { attempt, delayMs: delay });
        await new Promise(r => setTimeout(r, delay));
        delay = Math.min(delay * 2, 60_000);
        continue;
      }
      if (code !== undefined && code !== 0) {
        throw new Error(`sendMessage failed: code=${code} ${errorText}`);
      }
      // The current WeChat API returns only {message_id: ...} on success;
      // older deployments return {ret: 0} or {errcode: 0}.
      if (res.message_id === undefined && code === undefined) {
        throw new Error('sendMessage returned neither message_id nor status');
      }
      return;
    }
  }

  /** Get a presigned upload URL for media files. */
  async getUploadUrl(
    fileType: string,
    fileSize: number,
    fileName: string,
  ): Promise<GetUploadUrlResp> {
    return this.request<GetUploadUrlResp>(
      'ilink/bot/getuploadurl',
      { file_type: fileType, file_size: fileSize, file_name: fileName },
    );
  }

  /** Get bot config for a user — returns typing_ticket needed for sendTyping. */
  async getConfig(toUserId: string, contextToken: string): Promise<{ typing_ticket?: string }> {
    return this.request<{ typing_ticket?: string }>(
      'ilink/bot/getconfig',
      { ilink_user_id: toUserId, context_token: contextToken },
      10_000,
    );
  }

  /** Send typing indicator (status=1 typing, status=0 stop). */
  async sendTyping(toUserId: string, typingTicket: string, status: number = 1): Promise<void> {
    await this.request(
      'ilink/bot/sendtyping',
      { ilink_user_id: toUserId, typing_ticket: typingTicket, status },
      10_000,
    );
  }
}
