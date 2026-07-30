# FalkorDB Knowledge Graph Agent

Upload your planning documents and ask questions in plain language — the assistant builds a knowledge graph from them and answers your questions.

## Quick Start

1. **Choose a knowledge graph** — open the sidebar and pick the graph you want to use.
2. **Upload files** — click the paperclip next to the input box and upload your documents (PDF, Word, PowerPoint, Excel, images, text, CSV, JSON, HTML).
3. **Ingest documents** — click the **Ingest Documents** button. The assistant reads the files, recognizes the content, and writes it into the knowledge graph.
4. **Ask questions** — type your question into the chat, or use one of the suggested example prompts.

## Supported File Types

PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx), CSV, JSON, HTML, Markdown, plain text, images.

## What Can You Ask?

- "Which machines exist and what are their processing times?"
- "Show me the transport routes and vehicles."
- "Which shift models and worker pools are defined?"
- "Search for resources related to washing machines."
- "What does the knowledge graph contain?"

## Tips

- Upload your files first, then click **Ingest Documents** — that's all it takes to build the graph automatically.
- Switch the knowledge graph any time via the sidebar.
- If you're unsure what's inside, just ask: "What is in the knowledge graph?"

## Accounts & Chat History

- **Login is required.** On first visit, sign in with your username and password.
- **No account yet?** Open the `/register` page (e.g. `http://localhost:8000/register`) to create one — pick a username, an optional display name, and a password (minimum 8 characters). Registration can be disabled by your administrator (`REGISTER_ENABLED=0`).
- **Your chats are saved.** Every conversation is stored under your account and listed in the sidebar; you can resume any past thread. Uploaded files in those threads are kept on the server under `./data/elements`.