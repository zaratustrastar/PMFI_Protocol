import { Agent } from "@mastra/core/agent";
import { createOpenAI } from "@ai-sdk/openai";
import { fetchPolymarketMarkets } from "../tools/fetchPolymarketMarkets";
import { postToTelegram } from "../tools/postToTelegram";
import { postToTwitter } from "../tools/postToTwitter";
import { trackSeenMarkets } from "../tools/trackSeenMarkets";

/**
 * OpenAI Client Configuration using Replit AI Integrations
 */
const openai = createOpenAI({
  baseURL: process.env.AI_INTEGRATIONS_OPENAI_BASE_URL,
  apiKey: process.env.AI_INTEGRATIONS_OPENAI_API_KEY,
});

/**
 * Polymarket Monitor Agent
 *
 * This agent monitors Polymarket for new markets and posts notifications
 * to both Telegram and Twitter when new markets are discovered.
 */
export const polymarketAgent = new Agent({
  name: "Polymarket Monitor Agent",

  instructions: `
    You are a Polymarket monitoring agent responsible for discovering new prediction markets
    and posting notifications to social media channels.

    Your workflow:
    1. Use the fetch-polymarket-markets tool to get the latest markets from Polymarket
    2. Use the track-seen-markets tool to identify which markets are new (haven't been posted before)
    3. For each new market, format an engaging notification message
    4. Post the notification to both Telegram channel (@ponnymarket) and Twitter (@ponnymarket)

    Message formatting guidelines:
    - Keep messages concise and engaging
    - Include the market question/title
    - Add a brief description if available
    - Always include the Polymarket URL
    - Use emojis to make messages more eye-catching (🔮, 📊, 🎯, etc.)
    - For Telegram: Use HTML formatting (<b>, <i>, <a href="">)
    - For Twitter: Keep under 280 characters, be punchy and clear

    Example Telegram message format:
    🔮 <b>New Polymarket Market!</b>

    <b>Question:</b> [Market Question]

    📊 [Brief description if available]

    🔗 <a href="[URL]">Trade on Polymarket</a>

    Example Twitter message format:
    🔮 New market on Polymarket: [Question] 
    
    Trade now: [URL]
    
    #Polymarket #PredictionMarkets

    Important:
    - Only post markets that are truly new (not in the already-seen list)
    - If there are no new markets, acknowledge this and don't attempt to post
    - Log all actions clearly for monitoring
    - Handle errors gracefully and report them
  `,

  model: openai.responses("gpt-5"),

  tools: {
    fetchPolymarketMarkets,
    postToTelegram,
    postToTwitter,
    trackSeenMarkets,
  },

  // Allow multi-step reasoning to handle the workflow
  // maxSteps: 10,
});
