"""CLI entry point for the FalkorDB deep-agent harness.

Two modes:

- **Agent** (default): interactive LangGraph agent REPL, or a single query
  with ``--single``. Selected when no subcommand is given.
- **``create-admin``**: provision an administrator account against the
  persistence database. Creates the user with ``role='admin'`` and
  ``accountStatus='active'`` (email verified), or — if the username
  already exists — promotes it to admin and activates it. Prompts for the
  password interactively when ``--password`` is omitted.

Examples::

    falkordb-agent
    falkordb-agent --model glm-5.2:cloud --single "list graphs"
    falkordb-agent create-admin --username admin --email admin@example.com
    falkordb-agent create-admin --username admin --email a@b.com --password 'S3cret!'
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from dotenv import load_dotenv


async def _async_main(model: str | None, single_query: str | None) -> None:
    from langchain_core.messages import AIMessage, HumanMessage

    from falkordb_harness.agent import _DEFAULT_RECURSION_LIMIT, build_agent

    agent = build_agent({"configurable": {"model_name": model}})
    run_config = {"recursion_limit": _DEFAULT_RECURSION_LIMIT}

    if single_query:
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=single_query)]},
            config=run_config,
        )
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
            {"messages": chat_history + [HumanMessage(content=user_input)]},
            config=run_config,
        )
        output = result["messages"][-1].content
        print(f"\nAgent> {output}")

        chat_history.append(HumanMessage(content=user_input))
        chat_history.append(AIMessage(content=output))


async def _create_admin(username: str, email: str, password: str | None) -> int:
    """Provision an admin account. Returns a process exit code."""
    from falkordb_harness.auth import (
        MAX_USERNAME_LEN,
        _validate_email,
        bootstrap_admin_from_env,
    )
    from falkordb_harness.data_layer import build_data_layer, init_db

    if len(username) > MAX_USERNAME_LEN:
        print(f"Error: username must be at most {MAX_USERNAME_LEN} characters.", file=sys.stderr)
        return 2
    normalized, email_err = _validate_email(email)
    if email_err is not None or normalized is None:
        print(f"Error: invalid email: {email_err}", file=sys.stderr)
        return 2
    if not password:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm:  ")
        if password != confirm:
            print("Error: passwords do not match.", file=sys.stderr)
            return 2
    from falkordb_harness.auth import validate_password_strength

    pw_err = validate_password_strength(password)
    if pw_err is not None:
        print(f"Error: {pw_err}", file=sys.stderr)
        return 2

    # Reuse the env-bootstrap path by populating the env vars it reads,
    # then call it. This keeps a single source of truth for the
    # create-or-promote logic.
    import os

    os.environ["FIRST_ADMIN_USERNAME"] = username
    os.environ["FIRST_ADMIN_EMAIL"] = normalized
    os.environ["FIRST_ADMIN_PASSWORD"] = password

    # Ensure the schema exists before we write.
    layer = build_data_layer()
    try:
        await init_db(layer)
    finally:
        await layer.engine.dispose()

    await bootstrap_admin_from_env()
    print(f"Admin account ready: {username} ({normalized})")
    return 0


def main() -> None:
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(
        description="FalkorDB deep-agent harness — LangGraph agent "
        "over fp-extract-falkordb",
    )
    # Agent-mode options (used only when no subcommand is given).
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
    subparsers = parser.add_subparsers(dest="command")

    admin_parser = subparsers.add_parser(
        "create-admin",
        help="Create or promote an administrator account in the persistence DB.",
    )
    admin_parser.add_argument("--username", required=True, help="Admin username.")
    admin_parser.add_argument("--email", required=True, help="Admin email address.")
    admin_parser.add_argument(
        "--password",
        default=None,
        help="Admin password (prompted interactively if omitted).",
    )
    args = parser.parse_args()

    if args.command == "create-admin":
        code = asyncio.run(_create_admin(args.username, args.email, args.password))
        sys.exit(code)
    elif args.command is not None:
        parser.error(f"Unknown command: {args.command}")

    asyncio.run(_async_main(args.model, args.single))


if __name__ == "__main__":
    main()