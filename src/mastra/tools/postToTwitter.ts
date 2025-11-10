import { createTool } from "@mastra/core/tools";
import { z } from "zod";

/**
 * Tool to post tweets to Twitter/X
 */
export const postToTwitter = createTool({
  id: "post-to-twitter",
  description: "Posts a tweet to Twitter/X",

  inputSchema: z.object({
    message: z.string().describe("The tweet text to post (max 280 characters)"),
  }),

  outputSchema: z.object({
    success: z.boolean(),
    tweetId: z.string().optional(),
    error: z.string().optional(),
  }),

  execute: async ({ context, mastra }) => {
    const logger = mastra?.getLogger();
    logger?.info("🔧 [postToTwitter] Starting execution", {
      messageLength: context.message.length,
    });

    try {
      const apiKey = process.env.TWITTER_API_KEY;
      const apiSecret = process.env.TWITTER_API_SECRET;
      const accessToken = process.env.TWITTER_ACCESS_TOKEN;
      const accessSecret = process.env.TWITTER_ACCESS_SECRET;

      if (!apiKey || !apiSecret || !accessToken || !accessSecret) {
        logger?.error("❌ [postToTwitter] Missing Twitter API credentials");
        return {
          success: false,
          error: "Twitter API credentials not configured",
        };
      }

      // Truncate message if too long
      const tweetText =
        context.message.length > 280
          ? context.message.substring(0, 277) + "..."
          : context.message;

      logger?.info("📝 [postToTwitter] Posting tweet to Twitter");

      // Using Twitter API v2
      const url = "https://api.twitter.com/2/tweets";

      // Generate OAuth 1.0a signature (simplified version using fetch)
      // For production, consider using a library like 'twitter-api-v2'
      const oauth = await generateTwitterOAuth(
        url,
        "POST",
        {
          api_key: apiKey,
          api_secret: apiSecret,
          access_token: accessToken,
          access_secret: accessSecret,
        }
      );

      const response = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: oauth,
        },
        body: JSON.stringify({
          text: tweetText,
        }),
      });

      const result = await response.json();

      if (!response.ok) {
        logger?.error("❌ [postToTwitter] Failed to post tweet", {
          status: response.status,
          error: result,
        });
        return {
          success: false,
          error: result.detail || result.title || "Failed to post tweet",
        };
      }

      logger?.info("✅ [postToTwitter] Tweet posted successfully", {
        tweetId: result.data?.id,
      });

      return {
        success: true,
        tweetId: result.data?.id,
      };
    } catch (error) {
      logger?.error("❌ [postToTwitter] Error occurred", { error });
      return {
        success: false,
        error: error instanceof Error ? error.message : "Unknown error",
      };
    }
  },
});

// Helper function to generate OAuth signature for Twitter API
async function generateTwitterOAuth(
  url: string,
  method: string,
  credentials: {
    api_key: string;
    api_secret: string;
    access_token: string;
    access_secret: string;
  }
): Promise<string> {
  // This is a simplified OAuth implementation
  // For production, use a proper OAuth library
  const oauth_nonce = Math.random().toString(36).substring(2);
  const oauth_timestamp = Math.floor(Date.now() / 1000).toString();

  const params: Record<string, string> = {
    oauth_consumer_key: credentials.api_key,
    oauth_nonce,
    oauth_signature_method: "HMAC-SHA1",
    oauth_timestamp,
    oauth_token: credentials.access_token,
    oauth_version: "1.0",
  };

  // Create signature base string
  const paramString = Object.keys(params)
    .sort()
    .map((key) => `${key}=${encodeURIComponent(params[key])}`)
    .join("&");

  const signatureBase = `${method}&${encodeURIComponent(url)}&${encodeURIComponent(paramString)}`;

  // Create signing key
  const signingKey = `${encodeURIComponent(credentials.api_secret)}&${encodeURIComponent(credentials.access_secret)}`;

  // Generate signature using Web Crypto API
  const encoder = new TextEncoder();
  const keyData = encoder.encode(signingKey);
  const messageData = encoder.encode(signatureBase);

  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    keyData,
    { name: "HMAC", hash: "SHA-1" },
    false,
    ["sign"]
  );

  const signature = await crypto.subtle.sign("HMAC", cryptoKey, messageData);
  const oauth_signature = btoa(
    String.fromCharCode(...new Uint8Array(signature))
  );

  params.oauth_signature = oauth_signature;

  // Build OAuth header
  const authHeader =
    "OAuth " +
    Object.keys(params)
      .sort()
      .map((key) => `${key}="${encodeURIComponent(params[key])}"`)
      .join(", ");

  return authHeader;
}
