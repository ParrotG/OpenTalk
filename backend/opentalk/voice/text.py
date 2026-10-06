"""Interact with the same native AgentSession over standard input, without audio IO."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from opentalk.config import load_config
from opentalk.voice.session import open_session


async def converse(backend_config):
    async with open_session(text_only=True, backend_config=backend_config) as (session, agent, journal):
        await session.start(agent=agent)
        print("Meeting-room assistant ready. Enter a message, or /quit to exit.", flush=True)
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
        try:
            while True:
                print("User> ", end="", flush=True)
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8").strip()
                if line == "/quit":
                    break
                if not line:
                    continue
                before = {item.id for item in session.history.items}
                await session.run(user_input=line)
                replies = [item.text_content for item in session.history.items
                           if item.type == "message" and item.role == "assistant" and item.id not in before]
                if replies:
                    print(f"Assistant> {replies[-1]}", flush=True)
                await journal.save(agent.booking_tools.service)
        finally:
            transport.close()
        print(json.dumps({"report_file": str(journal.path)}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description="Run the booking AgentSession without a frontend or audio devices.")
    parser.add_argument("--config", type=Path, help="Booking backend configuration path.")
    args = parser.parse_args()
    try:
        asyncio.run(converse(load_config(args.config)))
    except (EOFError, KeyboardInterrupt):
        pass
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        print(f"Text conversation failed ({type(error).__name__}). Check credentials, configuration, and the session log.",
              file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
