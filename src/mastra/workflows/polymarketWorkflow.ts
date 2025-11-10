import { createStep, createWorkflow } from "../inngest";
import { z } from "zod";
import { fetchPolymarketMarkets } from "../tools/fetchPolymarketMarkets";
import { postToTelegram } from "../tools/postToTelegram";
import { postToTwitter } from "../tools/postToTwitter";
import pkg from "pg";
const { Pool } = pkg;

/**
 * Polymarket Monitoring Workflow
 *
 * This workflow runs on a schedule to check for new Polymarket markets
 * and post notifications to Telegram and Twitter.
 */

/**
 * Step 1: Fetch Markets and Post to Social Media
 * Fetches markets, identifies new ones, posts them, and ONLY THEN marks as seen
 */
const monitorAndPost = createStep({
  id: "monitor-and-post",
  description:
    "Fetches markets, posts new ones to social media, marks as seen only after successful posting",

  inputSchema: z.object({}),

  outputSchema: z.object({
    success: z.boolean(),
    newMarketsFound: z.number(),
    telegramPosts: z.number(),
    twitterPosts: z.number(),
    message: z.string(),
  }),

  execute: async ({ mastra }) => {
    const logger = mastra?.getLogger();
    logger?.info("🚀 [monitorAndPost] Starting Polymarket monitoring");

    const pool = new Pool({
      connectionString: process.env.DATABASE_URL,
    });

    try {
      // Step 1: Fetch latest markets
      const marketsResult = await fetchPolymarketMarkets.execute({
        context: { limit: 20 },
        mastra,
        runtimeContext: {},
      });

      logger?.info("📊 [monitorAndPost] Fetched markets", {
        count: marketsResult.count,
      });

      if (marketsResult.markets.length === 0) {
        return {
          success: true,
          newMarketsFound: 0,
          telegramPosts: 0,
          twitterPosts: 0,
          message: "No markets found",
        };
      }

      // Step 2: Check which markets are new (not yet in database)
      await pool.query(`
        CREATE TABLE IF NOT EXISTS seen_polymarket_markets (
          market_id TEXT PRIMARY KEY,
          seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
      `);

      const newMarkets = [];
      for (const market of marketsResult.markets) {
        const result = await pool.query(
          "SELECT market_id FROM seen_polymarket_markets WHERE market_id = $1",
          [market.id]
        );

        if (result.rows.length === 0) {
          newMarkets.push(market);
        }
      }

      logger?.info("✅ [monitorAndPost] Identified new markets", {
        newCount: newMarkets.length,
      });

      if (newMarkets.length === 0) {
        return {
          success: true,
          newMarketsFound: 0,
          telegramPosts: 0,
          twitterPosts: 0,
          message: "No new markets found",
        };
      }

      // Step 3: Post each new market and mark as seen ONLY after successful posting
      let telegramSuccesses = 0;
      let twitterSuccesses = 0;
      let bothSucceeded = 0;

      for (const market of newMarkets) {
        logger?.info("📝 [monitorAndPost] Processing market", {
          id: market.id,
          question: market.question,
        });

        // Format messages
        const telegramMessage = `🔮 <b>New Polymarket Market!</b>

<b>Question:</b> ${market.question}

${market.description ? `📊 ${market.description.substring(0, 200)}${market.description.length > 200 ? "..." : ""}

` : ""}<a href="${market.url}">🔗 Trade on Polymarket</a>

#Polymarket #PredictionMarkets`;

        const twitterMessage = `🔮 New market on Polymarket:

${market.question.substring(0, 180)}${market.question.length > 180 ? "..." : ""}

${market.url}

#Polymarket`;

        let telegramSuccess = false;
        let twitterSuccess = false;

        // Post to Telegram
        try {
          const telegramResult = await postToTelegram.execute({
            context: {
              message: telegramMessage,
              channelId: "@ponnymarket",
            },
            mastra,
            runtimeContext: {},
          });

          if (telegramResult.success) {
            telegramSuccess = true;
            telegramSuccesses++;
            logger?.info("✅ [monitorAndPost] Posted to Telegram", {
              marketId: market.id,
            });
          } else {
            logger?.error("❌ [monitorAndPost] Telegram post failed", {
              marketId: market.id,
              error: telegramResult.error,
            });
          }
        } catch (error) {
          logger?.error("❌ [monitorAndPost] Telegram error", {
            marketId: market.id,
            error,
          });
        }

        // Post to Twitter
        try {
          const twitterResult = await postToTwitter.execute({
            context: {
              message: twitterMessage,
            },
            mastra,
            runtimeContext: {},
          });

          if (twitterResult.success) {
            twitterSuccess = true;
            twitterSuccesses++;
            logger?.info("✅ [monitorAndPost] Posted to Twitter", {
              marketId: market.id,
            });
          } else {
            logger?.error("❌ [monitorAndPost] Twitter post failed", {
              marketId: market.id,
              error: twitterResult.error,
            });
          }
        } catch (error) {
          logger?.error("❌ [monitorAndPost] Twitter error", {
            marketId: market.id,
            error,
          });
        }

        // CRITICAL: Only mark as seen if BOTH posts succeeded
        if (telegramSuccess && twitterSuccess) {
          await pool.query(
            "INSERT INTO seen_polymarket_markets (market_id) VALUES ($1)",
            [market.id]
          );
          bothSucceeded++;
          logger?.info("✅ [monitorAndPost] Marked as seen", {
            marketId: market.id,
          });
        } else {
          logger?.warn("⚠️ [monitorAndPost] Not marking as seen (posting failed)", {
            marketId: market.id,
            telegramSuccess,
            twitterSuccess,
          });
        }

        // Small delay between posts to avoid rate limiting
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }

      const allSucceeded = bothSucceeded === newMarkets.length;
      const message = `Processed ${newMarkets.length} new markets: ${telegramSuccesses} to Telegram, ${twitterSuccesses} to Twitter, ${bothSucceeded} fully posted and marked as seen`;

      logger?.info("✅ [monitorAndPost] Completed", {
        newMarkets: newMarkets.length,
        telegramSuccesses,
        twitterSuccesses,
        bothSucceeded,
        allSucceeded,
      });

      return {
        success: allSucceeded,
        newMarketsFound: newMarkets.length,
        telegramPosts: telegramSuccesses,
        twitterPosts: twitterSuccesses,
        message,
      };
    } catch (error) {
      logger?.error("❌ [monitorAndPost] Fatal error", { error });
      return {
        success: false,
        newMarketsFound: 0,
        telegramPosts: 0,
        twitterPosts: 0,
        message: `Error: ${error instanceof Error ? error.message : "Unknown error"}`,
      };
    } finally {
      await pool.end();
    }
  },
});

/**
 * Create the workflow
 */
export const polymarketWorkflow = createWorkflow({
  id: "polymarket-monitor",

  // Empty input schema for time-based trigger
  inputSchema: z.object({}) as any,

  outputSchema: z.object({
    success: z.boolean(),
    newMarketsFound: z.number(),
    telegramPosts: z.number(),
    twitterPosts: z.number(),
    message: z.string(),
  }),
})
  .then(monitorAndPost as any)
  .commit();
