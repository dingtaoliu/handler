# Agent Framework

Documentation and design for the agent framework that helps users automate tasks on the platform.

## Use Cases

- **Help buyers create RFQs** — Buyers provide unstructured data (text, images, PDFs, spreadsheets) containing RFQ information such as destination, timeline, and product/demand listing details. Demand listings are the most important part of an RFQ.
- **Answer platform questions** — e.g. "How do I create an RFQ?" or "What's the status of my bids?"

## Components

- **Event Loop** — The main loop that drives agent execution.
- **Tools** — A registry and interface for tools; APIs the agent can call to perform actions on the platform.
- **Workspace** — Temporary storage for files, data, and notes during a session.
- **LLM Provider** — Interface to the language model (e.g. OpenAI) used for reasoning and response generation.
- **Agent Configuration** — Settings including which tools are available, the LLM provider, and other options.
- **Memory** — Long-term storage for information across sessions, such as user preferences and past interactions.
- **Session Management** — Creates and maintains per-session state, including conversation context, user messages, and reasoning steps.
- **Context** — An object representing a single agent state/step; includes user input, agent reasoning, and conversation history. This is what gets passed to the LLM provider.
- **Logging** — Records all LLM calls, tool calls, and agent actions for monitoring and debugging. Critical for iteration and ensuring the agent performs as expected.

> **Note:** The LLM is stateless, but the agent is stateful. Agent configuration and memory maintain state across interactions and sessions, while the LLM provider generates responses based on the current context.

---

## Triggering the Agent

The agent runs in the backend and is triggered by API calls. For now, the only entry point is the chat interface, but scheduled tasks and event-based triggers can be added in the future.

---

## Flow

### Chat Flow

1. User sends a message to the chat endpoint.
2. **New conversation:** Initialize an agent session — this includes a state object for conversation context, a workspace for temporary storage, and access to the tool registry and memory.  
   **Existing conversation:** Retrieve the session and build context from the user message and session data.
3. Enter the event loop:
   - Call the LLM provider with the current context to generate a response and reasoning steps.
   - Call tools as needed (e.g. if the LLM wants to call an API, call it and store the result in the workspace).
   - Update the context with the LLM response, tool results, and reasoning steps.
   - Repeat until the LLM indicates the conversation is complete or a threshold is reached (max iterations, time, or tokens).
4. Return the final response to the user and end the session.

---

## User Experience

Users interact with the agent through a chat interface. Responses are streamed back to the frontend so users can see reasoning steps and intermediate actions in real-time. This is critical for building trust and helping users understand how the agent arrives at its conclusions.
