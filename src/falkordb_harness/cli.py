"""CLI entry point for the FalkorDB deep-agent harness."""

from __future__ import annotations

import argparse
import asyncio

from dotenv import load_dotenv


async def _async_main(model: str | None, single_query: str | None) -> None:
    from langchain_core.messages import AIMessage, HumanMessage

    from falkordb_harness.agent import build_agent

    agent = build_agent({"configurable": {"model_name": model}})

    if single_query:
        result = await agent.ainvoke({"messages": [HumanMessage(content=single_query)]})
        print(result["messages"][-1].content)
        return

    print("FalkorDB Agent (type 'quit' to exit)")
    print("=" * 40)
    chat_history: list = []

    while True:
        try:
            user_input = input("\nYou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.lower() in {"quit", "exit", "q"}:
            break
        if not user_input:
            continue

        result = await agent.ainvoke(
            {"messages": chat_history + [HumanMessage(content=user_input)]}
        )
        output = result["messages"][-1].content
        print(f"\nAgent> {output}")

        chat_history.append(HumanMessage(content=user_input))
        chat_history.append(AIMessage(content=output))


def main() -> None:
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(
        description="FalkorDB deep-agent harness — LangGraph agent "
        "over fp-extract-falkordb",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Agent LLM model (default: AGENT_LLM_MODEL env "
        "or anthropic/claude-sonnet-4-20250514)",
    )
    parser.add_argument(
        "--single",
        type=str,
        default=None,
        help="Run a single query instead of interactive loop",
    )
    args = parser.parse_args()

    asyncio.run(_async_main(args.model, args.single))


if __name__ == "__main__":
    main()