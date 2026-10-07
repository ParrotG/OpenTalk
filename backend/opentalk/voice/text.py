"""Text conversation and local session commands without browser or audio devices."""

import argparse
import asyncio
import json
import shlex
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from opentalk.config import load_config
from opentalk.sessions.config import load_session_config
from opentalk.sessions.store import SessionError, SessionStore
from opentalk.voice.factories import agent_factory
from opentalk.voice.session import open_session


HELP = """Session commands:
/session                         Show the current session.
/session list [limit]            List saved sessions.
/session show ID                 Show a saved session and its attempts.
/session history [ID]            Show conversation history (last 100 items).
/session new                     Finish the current session and start a new one.
/session end                     Finish the current session normally.
/session resume ID               Resume a normally completed session.
/session help                    Show this help.
/quit                            Finish normally and exit.
"""


def display(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


async def converse(backend_config=None, *, session_config=None, store=None, agent_key="booking", factory=None):
    configuration = session_config or load_session_config()
    store = store or await asyncio.to_thread(SessionStore, configuration.database_path,
                                             lease_seconds=configuration.lease_seconds,
                                             pending_seconds=configuration.token_ttl_seconds)
    factory = factory or agent_factory(agent_key, backend_config=backend_config)
    stack = AsyncExitStack()
    current = None
    closed = asyncio.Event()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    async def finish():
        nonlocal stack, current
        if current is not None:
            session_id = current[0].userdata.session_id
            await stack.aclose()
            display(await asyncio.to_thread(store.get, session_id))
        stack, current = AsyncExitStack(), None

    async def start(resume_id=None):
        nonlocal current
        closed.clear()
        current = await stack.enter_async_context(open_session(
            text_only=True, store=store, session_config=configuration, resume_id=resume_id,
            agent_key=agent_key, factory=factory,
        ))
        await current[0].start(agent=current[1])
        current[0].on("close", lambda event: closed.set())
        display({"session_id": current[0].userdata.session_id,
                 "attempt_id": current[0].userdata.attempt_id, "status": "active"})

    print("Assistant ready. Enter a message, /session help, or /quit. No session starts until requested.", flush=True)
    try:
        while True:
            print("User> ", end="", flush=True)
            reading = asyncio.create_task(reader.readline())
            closing = asyncio.create_task(closed.wait()) if current else None
            try:
                if closing is not None:
                    done, _ = await asyncio.wait([reading, closing], return_when=asyncio.FIRST_COMPLETED)
                    if closing in done:
                        await finish()
                raw = await reading
            finally:
                reading.cancel()
                if closing is not None:
                    closing.cancel()
                await asyncio.gather(reading, *([closing] if closing is not None else []), return_exceptions=True)
            if not raw:
                break
            line = raw.decode("utf-8").strip()
            if line == "/quit":
                break
            if not line:
                continue
            if line.startswith("/"):
                try:
                    parts = shlex.split(line)
                    if parts[0] != "/session":
                        raise ValueError("Unknown command. Use /session help.")
                    command = parts[1] if len(parts) > 1 else "current"
                    args = parts[2:]
                    if command in {"current", "help", "new", "end"} and args:
                        raise ValueError("This command takes no arguments.")
                    if command == "help":
                        print(HELP, flush=True)
                    elif command == "current":
                        display(await asyncio.to_thread(store.get, current[0].userdata.session_id)
                                if current else {"session_id": None, "status": "idle"})
                    elif command == "list":
                        if len(args) > 1:
                            raise ValueError("Use /session list [limit].")
                        display(await asyncio.to_thread(store.list, limit=int(args[0]) if args else 20))
                    elif command == "show" and len(args) == 1:
                        display({"session": await asyncio.to_thread(store.get, args[0]),
                                 "attempts": await asyncio.to_thread(store.attempts, args[0])})
                    elif command == "history" and len(args) <= 1:
                        session_id = args[0] if args else current[0].userdata.session_id if current else None
                        if session_id is None:
                            raise ValueError("Provide a session ID when no session is active.")
                        history = await asyncio.to_thread(store.history_tail, session_id, max_items=100)
                        display({"session_id": session_id, "items": history["items"],
                                 "total_items": await asyncio.to_thread(store.history_count, session_id)})
                    elif command == "end":
                        await finish()
                    elif command == "new":
                        await finish()
                        await start()
                    elif command == "resume" and len(args) == 1:
                        target = await asyncio.to_thread(store.get, args[0])
                        if target["status"] != "completed":
                            raise SessionError("not_resumable", "Only normally completed sessions can be resumed.")
                        if target["agent_key"] != agent_key:
                            raise SessionError("agent_mismatch", "Run the CLI with the original --agent type.")
                        await finish()
                        await start(args[0])
                    else:
                        raise ValueError("Invalid session command. Use /session help.")
                except (SessionError, ValueError) as error:
                    display({"error": getattr(error, "code", "invalid_command"), "message": str(error)})
                continue
            if current is None:
                await start()
            session, agent, journal = current
            before = {item.id for item in session.history.items}
            await session.run(user_input=line)
            replies = [item.text_content for item in session.history.items
                       if item.type == "message" and item.role == "assistant" and item.id not in before]
            if replies:
                print(f"Assistant> {replies[-1]}", flush=True)
            await journal.save()
    except BaseException:
        await stack.__aexit__(*sys.exc_info())
        current = None
        raise
    else:
        await finish()
    finally:
        transport.close()


def main():
    parser = argparse.ArgumentParser(description="Talk to the agent and inspect or resume SQLite sessions.")
    parser.add_argument("--config", type=Path, help="Booking backend configuration path.")
    parser.add_argument("--session-config", type=Path, help="Session persistence configuration path.")
    parser.add_argument("--agent", choices=("booking", "conversation"), default="booking")
    args = parser.parse_args()
    try:
        asyncio.run(converse(load_config(args.config) if args.agent == "booking" else None,
                             session_config=load_session_config(args.session_config), agent_key=args.agent))
    except KeyboardInterrupt:
        print("Conversation cancelled. The active session cannot be resumed.", file=sys.stderr)
        raise SystemExit(130)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        print(f"Text conversation failed ({type(error).__name__}). Check credentials and session diagnostics.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
