import { createTool } from "@mastra/core/tools";
import { z } from "zod";

/**
 * Tool to fetch new markets from Polymarket
 */
export const fetchPolymarketMarkets = createTool({
  id: "fetch-polymarket-markets",
  description:
    "Fetches the latest active markets from Polymarket using their Gamma API",

  inputSchema: z.object({
    limit: z
      .number()
      .optional()
      .default(10)
      .describe("Number of markets to fetch"),
  }),

  outputSchema: z.object({
    markets: z.array(
      z.object({
        id: z.string(),
        question: z.string(),
        slug: z.string(),
        description: z.string().optional(),
        active: z.boolean(),
        closed: z.boolean(),
        startDate: z.string().optional(),
        endDate: z.string().optional(),
        url: z.string(),
      })
    ),
    count: z.number(),
  }),

  execute: async ({ context, mastra }) => {
    const logger = mastra?.getLogger();
    logger?.info("🔧 [fetchPolymarketMarkets] Starting execution", {
      limit: context.limit,
    });

    try {
      const baseUrl = "https://gamma-api.polymarket.com";
      const params = new URLSearchParams({
        order: "id",
        ascending: "false",
        closed: "false",
        limit: context.limit?.toString() || "10",
      });

      const url = `${baseUrl}/events?${params.toString()}`;
      logger?.info("📝 [fetchPolymarketMarkets] Fetching from Polymarket API", {
        url,
      });

      const response = await fetch(url);

      if (!response.ok) {
        logger?.error("❌ [fetchPolymarketMarkets] API request failed", {
          status: response.status,
          statusText: response.statusText,
        });
        throw new Error(
          `Polymarket API request failed: ${response.statusText}`
        );
      }

      const events = await response.json();
      logger?.info("📊 [fetchPolymarketMarkets] Received events", {
        count: events?.length || 0,
      });

      // Extract markets from events
      const markets = [];
      for (const event of events || []) {
        if (event.markets && Array.isArray(event.markets)) {
          for (const market of event.markets) {
            markets.push({
              id: market.id || market.conditionId || "",
              question: market.question || event.title || "",
              slug: market.slug || event.slug || "",
              description: market.description || event.description || "",
              active: market.active !== false,
              closed: market.closed === true,
              startDate: market.startDate || event.startDate,
              endDate: market.endDate || event.endDate,
              url: `https://polymarket.com/event/${event.slug || market.slug}`,
            });
          }
        }
      }

      logger?.info("✅ [fetchPolymarketMarkets] Processing complete", {
        marketsCount: markets.length,
      });

      return {
        markets: markets.slice(0, context.limit || 10),
        count: markets.length,
      };
    } catch (error) {
      logger?.error("❌ [fetchPolymarketMarkets] Error occurred", { error });
      throw error;
    }
  },
});
