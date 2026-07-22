"""Chainlit frontend for the FalkorDB deep-agent harness.

Run with:
    chainlit run src/falkordb_harness/chainlit_app.py --port 8000
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import chainlit as cl
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage

load_dotenv(override=True)

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

MAX_HISTORY_PAIRS = 20


@cl.on_chat_start
async def on_chat_start() -> None:
    from falkordb_harness.agent import build_agent

    agent = build_agent()
    cl.user_session.set("agent", agent)
    cl.user_session.set("chat_history", [])

    await cl.Message(
        content=(
            "Welcome to the **FalkorDB Knowledge Graph Agent**.\n\n"
            "I can help you:\n"
            "- **Ingest** documents into a knowledge graph\n"
            "- **Query** the graph with natural language or Cypher\n"
            "- **Inspect** the schema, nodes, and edges\n\n"
            "You can also upload files to ingest."
        ),
    ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    agent = cl.user_session.get("agent")
    chat_history: list = cl.user_session.get("chat_history")

    user_content = message.content or ""

    if message.elements:
        for element in message.elements:
            if hasattr(element, "path") and element.path:
                dest = UPLOADS_DIR / element.name
                shutil.copy2(element.path, dest)
                user_content += f"\n[Uploaded file: {dest}]"

    response_msg = cl.Message(content="")
    await response_msg.send()

    active_steps: dict[str, cl.Step] = {}
    full_response = ""

    async for event in agent.astream_events(
        {"messages": chat_history + [HumanMessage(content=user_content)]},
        version="v2",
    ):
        kind = event.get("event")

        if kind == "on_chat_model_stream":
            metadata = event.get("metadata", {})
            if metadata.get("langgraph_node") in ("agent", "log_attachments"):
                if metadata.get("langgraph_node") == "log_attachments":
                    continue
            chunk = event.get("data", {}).get("chunk")
            if chunk:
                token = chunk.content if hasattr(chunk, "content") else str(chunk)
                if token:
                    full_response += token
                    await response_msg.stream_token(token)

        elif kind == "on_tool_start":
            run_id = event.get("run_id", "")
            tool_name = event.get("name", "tool")
            tool_input = event.get("data", {}).get("input", "")
            step = cl.Step(name=tool_name, type="tool")
            step.input = str(tool_input)[:2000]
            await step.send()
            active_steps[run_id] = step

        elif kind == "on_tool_end":
            run_id = event.get("run_id", "")
            step = active_steps.pop(run_id, None)
            if step:
                output = event.get("data", {}).get("output", "")
                step.output = str(output)[:2000]
                await step.update()

    await response_msg.update()

    chat_history.append(HumanMessage(content=user_content))
    chat_history.append(AIMessage(content=full_response))

    if len(chat_history) > MAX_HISTORY_PAIRS * 2:
        chat_history[:] = chat_history[-(MAX_HISTORY_PAIRS * 2) :]

    cl.user_session.set("chat_history", chat_history)
