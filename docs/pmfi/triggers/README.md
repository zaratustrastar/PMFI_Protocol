# Connector Webhook Triggers

Create webhook handlers for third-party connectors (Linear, GitHub, Slack, etc.) that trigger Mastra workflows.

## Quick Start

**2 steps to add a connector webhook:**

### 1. Create `src/triggers/{connector}Triggers.ts`

```typescript
import { registerApiRoute } from "../mastra/inngest";

export function register{Connector}Trigger({ triggerType, handler }) {
  return [
    registerApiRoute("/{connector}/webhook", {
      method: "POST",
      handler: async (c) => {
        const mastra = c.get("mastra");
        const logger = mastra?.getLogger();
        
        try {
          const payload = await c.req.json();
          logger?.info("📥 [{Connector}] Webhook received", { payload });
          
          // Only process events you care about (optional)
          if (payload.action !== "created" && payload.action !== "updated") {
            logger?.info("[{Connector}] Skipping event", { action: payload.action });
            return c.json({ success: true, skipped: true });
          }
          
          // Pass the entire payload - let the consumer pick what they need
          const triggerInfo = {
            type: triggerType,
            payload,
          };
          
          const result = await handler(mastra, triggerInfo);
          return c.json({ success: true, result });
        } catch (error) {
          logger?.error("Error:", { error });
          return c.json({ success: false, error: String(error) }, 500);
        }
      },
    }),
  ];
}
```

### 2. Register in `src/mastra/index.ts`

```typescript
import { register{Connector}Trigger } from "../triggers/{connector}Triggers";
import { exampleWorkflow } from "./workflows/exampleWorkflow";

// In server > apiRoutes array:
...register{Connector}Trigger({
  triggerType: "{connector}/{event}",
  handler: async (mastra, triggerInfo) => {
    // Extract what you need from the payload
    const data = triggerInfo.payload?.data || {};
    const title = data.title || data.name || "Untitled";
    
    const run = await exampleWorkflow.createRunAsync();
    return await run.start({ 
      inputData: { 
        message: title,
        // Or pass the entire payload if your workflow needs it
        // ...triggerInfo.payload
      } 
    });
  }
}),
```

Done! See `src/triggers/exampleConnectorTrigger.ts` for a complete example.

## Key Principles

**1. Be Lenient:** Use fallbacks for missing fields, don't fail the workflow

```typescript
const id = payload?.data?.id || Date.now();
```

**2. Log Everything:** Log the full payload to understand what you're receiving

```typescript
logger?.info("📥 Webhook received", { payload });
```

**3. Keep it Simple:** Don't validate webhook signatures or authenticate senders - focus on processing

**4. Always Try:** Wrap in try/catch and return 500 on error, but attempt to process any data

## Detailed Guide

### Step 1: Find Webhook Documentation

Find sample payloads in your connector's docs:

- **Linear**: <https://developers.linear.app/docs/graphql/webhooks>
- **GitHub**: <https://docs.github.com/en/webhooks>
- **Stripe**: <https://stripe.com/docs/webhooks>

### Step 2: Create the Trigger File

Create `src/triggers/{connectorName}Triggers.ts`:

```typescript
import { registerApiRoute } from "../mastra/inngest";

export function register{Connector}Trigger({ triggerType, handler }) {
  return [
    registerApiRoute("/{connector}/webhook", {
      method: "POST",
      handler: async (c) => {
        const mastra = c.get("mastra");
        const logger = mastra?.getLogger();

        try {
          const payload = await c.req.json();
          logger?.info("📥 [{Connector}] Webhook received", { payload });

          // Only process events you care about (optional)
          if (payload.action !== "created" && payload.action !== "updated") {
            logger?.info("[{Connector}] Skipping event", { action: payload.action });
            return c.json({ success: true, skipped: true });
          }

          // Pass the entire payload - let the consumer pick what they need
          const triggerInfo = { type: triggerType, payload };

          const result = await handler(mastra, triggerInfo);
          return c.json({ success: true, result });
        } catch (error) {
          logger?.error("❌ [{Connector}] Error", { error });
          return c.json({ success: false, error: String(error) }, 500);
        }
      },
    }),
  ];
}
```

### Step 3: Register in `src/mastra/index.ts`

```typescript
import { register{Connector}Trigger } from "../triggers/{connector}Triggers";
import { exampleWorkflow } from "./workflows/exampleWorkflow";

// In server > apiRoutes array:
...register{Connector}Trigger({
  triggerType: "{connector}/{event}",
  handler: async (mastra, triggerInfo) => {
    // Extract what you need from the full payload
    const data = triggerInfo.payload?.data || {};
    const title = data.title || data.name || "Untitled";
    
    const run = await exampleWorkflow.createRunAsync();
    return await run.start({
      inputData: { message: title }
    });
  }
}),
```

Done! Webhooks will be automatically received at `/{connector}/webhook`.

## Tips

**Same name everywhere:** Use `{connector}` consistently in paths, event types, and file names

**Use fallbacks:** `payload?.data?.id || Date.now()`

**Log the payload:** `logger?.info("Webhook received", { payload })`

**Filter to events you want:** Only process specific actions (e.g., `created`, `updated`), skip the rest

**Don't validate senders:** Focus on processing, not authentication

## Examples

See these files for complete examples:

- `exampleConnectorTrigger.ts` - Linear webhook handler (comprehensive example)
- `slackTriggers.ts` - Slack webhook handler
- `telegramTriggers.ts` - Telegram webhook handler

## Testing

Test your webhook handler:

1. Use the connector's webhook testing tool
2. Check logs for webhook receipt and processing
3. Verify workflow execution in Inngest dashboard
4. Test error cases (missing fields, wrong event type, etc.)

## Troubleshooting

### Webhook not received

- Check webhook URL is correct
- Verify Repl is running and accessible
- Check connector configuration in external service

### Payload parsing errors

- Log the raw payload to see actual structure
- Compare with connector's documentation
- Check for unexpected field types

### Workflow not starting

- Verify workflow is imported and registered
- Check thread ID is unique
- Review Inngest dashboard for errors

## Integration with Inngest

The `registerApiRoute` function (from `src/mastra/inngest/index.ts`) automatically:

1. Creates an Inngest event for each webhook
2. Forwards the request through Inngest for reliability
3. Handles retries for transient failures
4. Provides observability through Inngest dashboard

This means webhooks are:

- ✅ Reliable (retries on failure)
- ✅ Observable (Inngest dashboard)
- ✅ Durable (survives Repl restarts)
- ✅ Scalable (handled asynchronously)
