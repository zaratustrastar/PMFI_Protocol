import { createStep, createWorkflow } from "../inngest";
import { z } from "zod";
import { polymarketAgent } from "../agents/polymarketAgent";

/**
 * Polymarket Monitoring Workflow
 *
 * This workflow runs on a schedule to check for new Polymarket markets
 * and post notifications to Telegram and Twitter.
 */

/**
 * Step 1: Monitor and Post New Markets
 * Uses the agent to orchestrate the entire process
 */
const monitorAndPostMarkets = createStep({
  id: "monitor-and-post-markets",
  description:
    "Monitors Polymarket for new markets and posts notifications to social media",

  // Empty input schema for time-based trigger
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
    logger?.info("🚀 [monitorAndPostMarkets] Starting Polymarket monitoring");

    try {
      // Use the agent to handle the entire workflow
      const prompt = `
        Please check Polymarket for new markets and post them to our social media channels:
        
        1. Fetch the latest markets from Polymarket (limit to 20)
        2. Check which ones are new (haven't been posted before)
        3. For each new market, create an engaging notification
        4. Post to Telegram channel @ponnymarket
        5. Post to Twitter account @ponnymarket
        
        Please provide a summary of what you did, including:
        - How many new markets were found
        - How many were posted to Telegram
        - How many were posted to Twitter
        - Any errors or issues encountered
      `;

      logger?.info("📝 [monitorAndPostMarkets] Calling agent");

      const response = await polymarketAgent.generateLegacy(
        [{ role: "user", content: prompt }],
        {
          maxSteps: 15, // Allow enough steps for fetching, checking, and posting multiple markets
        }
      );

      logger?.info("✅ [monitorAndPostMarkets] Agent completed", {
        responseLength: response.text.length,
      });

      // Parse the response to extract metrics
      // This is a simplified version - in production you might want structured output
      const responseText = response.text.toLowerCase();
      const hasNewMarkets = responseText.includes("new market");
      
      return {
        success: true,
        newMarketsFound: hasNewMarkets ? 1 : 0, // Simplified - would parse from agent response
        telegramPosts: hasNewMarkets ? 1 : 0,
        twitterPosts: hasNewMarkets ? 1 : 0,
        message: response.text,
      };
    } catch (error) {
      logger?.error("❌ [monitorAndPostMarkets] Error occurred", { error });
      
      return {
        success: false,
        newMarketsFound: 0,
        telegramPosts: 0,
        twitterPosts: 0,
        message: `Error: ${error instanceof Error ? error.message : "Unknown error"}`,
      };
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
  .then(monitorAndPostMarkets as any)
  .commit();
