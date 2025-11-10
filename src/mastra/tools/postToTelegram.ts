import { createTool } from "@mastra/core/tools";
import { z } from "zod";

/**
 * Tool to post messages to Telegram channel
 */
export const postToTelegram = createTool({
  id: "post-to-telegram",
  description: "Posts a formatted message to a Telegram channel",

  inputSchema: z.object({
    message: z.string().describe("The message to post"),
    channelId: z.string().describe("The Telegram channel ID or username"),
  }),

  outputSchema: z.object({
    success: z.boolean(),
    messageId: z.number().optional(),
    error: z.string().optional(),
  }),

  execute: async ({ context, mastra }) => {
    const logger = mastra?.getLogger();
    logger?.info("🔧 [postToTelegram] Starting execution", {
      channelId: context.channelId,
      messageLength: context.message.length,
    });

    try {
      const botToken = process.env.TELEGRAM_BOT_TOKEN;
      if (!botToken) {
        logger?.error("❌ [postToTelegram] Missing TELEGRAM_BOT_TOKEN");
        return {
          success: false,
          error: "TELEGRAM_BOT_TOKEN not configured",
        };
      }

      const url = `https://api.telegram.org/bot${botToken}/sendMessage`;
      
      logger?.info("📝 [postToTelegram] Sending message to Telegram");

      const response = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          chat_id: context.channelId,
          text: context.message,
          parse_mode: "HTML",
          disable_web_page_preview: false,
        }),
      });

      const result = await response.json();

      if (!response.ok || !result.ok) {
        logger?.error("❌ [postToTelegram] Failed to send message", {
          status: response.status,
          error: result.description || result.error,
        });
        return {
          success: false,
          error: result.description || "Failed to send message",
        };
      }

      logger?.info("✅ [postToTelegram] Message sent successfully", {
        messageId: result.result?.message_id,
      });

      return {
        success: true,
        messageId: result.result?.message_id,
      };
    } catch (error) {
      logger?.error("❌ [postToTelegram] Error occurred", { error });
      return {
        success: false,
        error: error instanceof Error ? error.message : "Unknown error",
      };
    }
  },
});
