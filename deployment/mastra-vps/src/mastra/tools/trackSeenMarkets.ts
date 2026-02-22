import { createTool } from "@mastra/core/tools";
import { z } from "zod";
import pkg from "pg";
const { Pool } = pkg;

/**
 * Tool to track which markets have been posted to avoid duplicates
 */
export const trackSeenMarkets = createTool({
  id: "track-seen-markets",
  description:
    "Checks which markets are new (not previously posted) and marks them as seen",

  inputSchema: z.object({
    marketIds: z.array(z.string()).describe("Array of market IDs to check"),
  }),

  outputSchema: z.object({
    newMarkets: z.array(z.string()).describe("IDs of markets not seen before"),
    alreadySeen: z
      .array(z.string())
      .describe("IDs of markets already posted"),
  }),

  execute: async ({ context, mastra }) => {
    const logger = mastra?.getLogger();
    logger?.info("🔧 [trackSeenMarkets] Starting execution", {
      marketCount: context.marketIds.length,
    });

    const pool = new Pool({
      connectionString: process.env.TRADING_DATABASE_URL || process.env.DATABASE_URL,
    });

    try {
      // Create table if it doesn't exist (already created, but just in case)
      logger?.info("📝 [trackSeenMarkets] Ensuring table exists");
      await pool.query(`
        CREATE TABLE IF NOT EXISTS seen_polymarket_markets (
          market_id TEXT PRIMARY KEY,
          seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
      `);

      const newMarkets: string[] = [];
      const alreadySeen: string[] = [];

      // Check each market
      for (const marketId of context.marketIds) {
        const result = await pool.query(
          "SELECT market_id FROM seen_polymarket_markets WHERE market_id = $1",
          [marketId]
        );

        if (result.rows.length === 0) {
          newMarkets.push(marketId);
          // Mark as seen
          await pool.query(
            "INSERT INTO seen_polymarket_markets (market_id) VALUES ($1)",
            [marketId]
          );
        } else {
          alreadySeen.push(marketId);
        }
      }

      logger?.info("✅ [trackSeenMarkets] Processing complete", {
        newCount: newMarkets.length,
        seenCount: alreadySeen.length,
      });

      return {
        newMarkets,
        alreadySeen,
      };
    } catch (error) {
      logger?.error("❌ [trackSeenMarkets] Error occurred", { error });
      throw error;
    } finally {
      await pool.end();
    }
  },
});
