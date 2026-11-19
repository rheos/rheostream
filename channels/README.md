# Conversation channels

Reserved for text, Telegram, voice, and other conversation surfaces. Each channel
uses the shared identity, policy, session, and action services. Nothing is implemented
here yet (`rheo_channels` is empty); agents reach rheoStream today through the MCP
facade in `apps/mcp`.

Different audiences do not automatically share transcripts. Channel enrollment,
revocation, and response visibility must respect authenticated workspace context.
