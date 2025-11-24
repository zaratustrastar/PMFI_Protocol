import { createStep, createWorkflow } from "../inngest";
import { z } from "zod";
import { fetchPolymarketMarkets } from "../tools/fetchPolymarketMarkets";
import { postToTelegram } from "../tools/postToTelegram";
import { queueTradingJob } from "../tools/queueTradingJob";
import pkg from "pg";
const { Pool } = pkg;

/**
 * Polymarket Monitoring Workflow
 *
 * This workflow runs on a schedule to check for new Polymarket markets
 * and post notifications to Telegram.
 */

/**
 * Escape HTML special characters for Telegram HTML parse mode
 */
function escapeTelegramHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/**
 * Add referral code to Polymarket URLs
 */
function addReferralCode(url: string): string {
  const referralCode = "q2XDjZW";
  
  // Check if URL already has query parameters
  if (url.includes("?")) {
    return `${url}&via=${referralCode}`;
  } else {
    return `${url}?via=${referralCode}`;
  }
}

/**
 * Check if a market is an up/down short-term market
 */
function isUpDownMarket(market: any): boolean {
  const UPDOWN_KEYWORDS = [
    "up or down",
    "up-or-down",
    "updown",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "12pm et",
    "1pm et",
    "2pm et",
    "3pm et",
    "4pm et",
    "5pm et",
  ];

  const name = (market.question || market.name || market.title || "").toLowerCase();
  const slug = (market.slug || "").toLowerCase();
  const tags = (market.tags || []).map((t: any) => String(t).toLowerCase());
  const haystack = [name, slug, ...tags].join(" ");

  return UPDOWN_KEYWORDS.some((keyword) => haystack.includes(keyword)) || tags.includes("updown");
}

/**
 * Check if a market's duration is too short (< 15 hours)
 * Markets with short durations (same-day sports, quick events) should not be traded
 */
function isShortDurationMarket(market: any, minHours: number = 15): { isShort: boolean; durationHours?: number } {
  if (!market.createdAt || !market.closedTime) {
    // No date info - can't determine duration, allow trading (fallback to keyword filter)
    return { isShort: false };
  }

  try {
    const created = new Date(market.createdAt);
    const closed = new Date(market.closedTime);
    
    const durationMs = closed.getTime() - created.getTime();
    const durationHours = durationMs / (1000 * 60 * 60);
    
    return {
      isShort: durationHours < minHours,
      durationHours: Math.round(durationHours * 10) / 10, // Round to 1 decimal
    };
  } catch (error) {
    // Invalid date format - can't parse, allow trading (fallback to keyword filter)
    return { isShort: false };
  }
}

/**
 * Step 1: Fetch Markets and Post to Telegram
 * Fetches markets, identifies new ones, posts them, and ONLY THEN marks as seen
 */
const monitorAndPost = createStep({
  id: "monitor-and-post",
  description:
    "Fetches markets, posts new ones to Telegram, marks as seen only after successful posting",

  inputSchema: z.object({}),

  outputSchema: z.object({
    success: z.boolean(),
    newMarketsFound: z.number(),
    telegramPosts: z.number(),
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
          message: "No new markets found",
        };
      }

      // Step 3: Post each new market to Telegram and mark as seen ONLY after successful posting
      let telegramSuccesses = 0;

      for (const market of newMarkets) {
        logger?.info("📝 [monitorAndPost] Processing market", {
          id: market.id,
          question: market.question,
        });

        // Format Telegram message (escape HTML special chars in user content)
        const escapedQuestion = escapeTelegramHtml(market.question);
        const escapedDescription = market.description
          ? escapeTelegramHtml(market.description.substring(0, 200)) +
            (market.description.length > 200 ? "..." : "")
          : "";
        
        const telegramMessage = `🔮 <b>New Polymarket Market!</b>

<b>Question:</b> ${escapedQuestion}

${escapedDescription ? `📊 ${escapedDescription}

` : ""}<a href="${market.url}">🔗 Trade on Polymarket</a>

#Polymarket #PredictionMarkets`;

        let telegramSuccess = false;

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

        // CRITICAL: Only mark as seen if Telegram post succeeded
        if (telegramSuccess) {
          await pool.query(
            "INSERT INTO seen_polymarket_markets (market_id) VALUES ($1)",
            [market.id]
          );
          logger?.info("✅ [monitorAndPost] Marked as seen", {
            marketId: market.id,
          });

          // Queue this market for automated trading ONLY if it passes all filters
          const isUpDown = isUpDownMarket(market);
          const durationCheck = isShortDurationMarket(market);
          
          // Skip trading if it's an up/down market OR too short duration
          if (isUpDown) {
            logger?.info("⏭️ [monitorAndPost] Skipped trading queue (up/down market)", {
              marketId: market.id,
              question: market.question.substring(0, 100),
            });
          } else if (durationCheck.isShort) {
            logger?.info("⏭️ [monitorAndPost] Skipped trading queue (short duration)", {
              marketId: market.id,
              question: market.question.substring(0, 100),
              durationHours: durationCheck.durationHours,
              minRequired: 15,
            });
          } else {
            // Market passed all filters - queue for trading
            try {
              const queueResult = await queueTradingJob.execute({
                context: {
                  marketIds: [market.slug],
                  marketData: [
                    {
                      id: market.slug,
                      createdAt: market.createdAt,
                      closedTime: market.closedTime,
                    },
                  ],
                },
                mastra,
                runtimeContext: {},
              });
              logger?.info("💰 [monitorAndPost] Queued for trading", {
                marketId: market.id,
                marketSlug: market.slug,
                queued: queueResult.queued,
                durationHours: durationCheck.durationHours,
              });
            } catch (error) {
              logger?.error("❌ [monitorAndPost] Failed to queue trading job", {
                marketId: market.id,
                marketSlug: market.slug,
                error,
              });
            }
          }
        } else {
          logger?.warn("⚠️ [monitorAndPost] Not marking as seen (posting failed)", {
            marketId: market.id,
            telegramSuccess,
          });
        }

        // Small delay between posts to avoid rate limiting
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }

      const allSucceeded = telegramSuccesses === newMarkets.length;
      const message = `Processed ${newMarkets.length} new markets: ${telegramSuccesses} posted to Telegram`;

      logger?.info("✅ [monitorAndPost] Completed", {
        newMarkets: newMarkets.length,
        telegramSuccesses,
        allSucceeded,
      });

      return {
        success: allSucceeded,
        newMarketsFound: newMarkets.length,
        telegramPosts: telegramSuccesses,
        message,
      };
    } catch (error) {
      logger?.error("❌ [monitorAndPost] Fatal error", { error });
      return {
        success: false,
        newMarketsFound: 0,
        telegramPosts: 0,
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
    message: z.string(),
  }),
})
  .then(monitorAndPost as any)
  .commit();
