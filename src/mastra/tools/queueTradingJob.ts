import { createTool } from "@mastra/core/tools";
import { z } from "zod";
import pkg from "pg";
const { Pool } = pkg;

/**
 * Tool to queue new markets for automated trading
 */
export const queueTradingJob = createTool({
  id: "queue-trading-job",
  description:
    "Queues newly detected markets for automated trading by the Python bot",

  inputSchema: z.object({
    marketIds: z
      .array(z.string())
      .describe("Array of market IDs to queue for trading"),
  }),

  outputSchema: z.object({
    queued: z.number().describe("Number of markets queued for trading"),
    skipped: z.number().describe("Number of markets already queued"),
  }),

  execute: async ({ context, mastra }) => {
    const logger = mastra?.getLogger();
    logger?.info("🔧 [queueTradingJob] Starting execution", {
      marketCount: context.marketIds.length,
    });

    const pool = new Pool({
      connectionString: process.env.DATABASE_URL,
    });

    try {
      let queued = 0;
      let skipped = 0;

      for (const marketId of context.marketIds) {
        const result = await pool.query(
          `
          INSERT INTO trading_jobs (market_id, status)
          VALUES ($1, 'PENDING')
          ON CONFLICT (market_id) DO NOTHING
          RETURNING id
        `,
          [marketId]
        );

        if (result.rowCount > 0) {
          queued++;
          logger?.info(`✅ [queueTradingJob] Queued market ${marketId}`);
        } else {
          skipped++;
          logger?.info(
            `⏭️  [queueTradingJob] Market ${marketId} already queued`
          );
        }
      }

      logger?.info("✅ [queueTradingJob] Processing complete", {
        queued,
        skipped,
      });

      return { queued, skipped };
    } catch (error) {
      logger?.error("❌ [queueTradingJob] Error occurred", { error });
      throw error;
    } finally {
      await pool.end();
    }
  },
});
