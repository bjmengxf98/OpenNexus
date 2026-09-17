import { type SendMessageReceipt, type WeChatApi } from './api.js';
import { MessageItemType, MessageType, MessageState, type MessageItem, type OutboundMessage } from './types.js';
import { logger } from '../logger.js';

export function createSender(api: WeChatApi, botAccountId: string) {
  let clientCounter = 0;

  function generateClientId(): string {
    return `wcc-${Date.now()}-${++clientCounter}`;
  }

  async function sendText(
    toUserId: string,
    contextToken: string,
    text: string,
    options: { maxRetries?: number } = {},
  ): Promise<SendMessageReceipt> {
    const clientId = generateClientId();

    const items: MessageItem[] = [
      {
        type: MessageItemType.TEXT,
        text_item: { text },
      },
    ];

    const msg: OutboundMessage = {
      from_user_id: botAccountId,
      to_user_id: toUserId,
      client_id: clientId,
      message_type: MessageType.BOT,
      message_state: MessageState.FINISH,
      context_token: contextToken,
      item_list: items,
    };

    logger.info('Sending text message', { toUserId, clientId, textLength: text.length });
    const receipt = await api.sendMessage({ msg }, options.maxRetries);
    logger.info('Text message accepted', {
      toUserId,
      clientId,
      confirmed: receipt.confirmed,
      messageId: receipt.messageId,
    });
    return receipt;
  }

  return { sendText };
}
