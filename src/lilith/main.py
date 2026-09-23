import signal
import sys
from threading import Event
from lilith.commands import handle_command
from lilith.workers import WorkerManager

from lilith.config import (
    get_database_path,
    get_model_name,
    get_workspace,
)
from lilith.database import Database
from lilith.models import ModelRole, gateway
from lilith.self_state import SelfState


shutdown_event = Event()


from lilith.personality import SYSTEM_PROMPT



def request_shutdown(signum, frame):
    shutdown_event.set()
    raise KeyboardInterrupt


def main():
    shutdown_event.clear()
    signal.signal(
        signal.SIGINT,
        request_shutdown,
    )

    signal.signal(
        signal.SIGTERM,
        request_shutdown,
    )

    database_path = get_database_path()
    model_name = get_model_name()

    database = Database(database_path)

    SelfState(database)

    database.audit(
        event_type="system_start",
        message=(
            f"Lilith runtime started using "
            f"model {model_name}."
        ),
    )

    conversation_gateway = gateway(database, ModelRole.CONVERSATION)
    router_gateway = gateway(database, ModelRole.ROUTER)

    manager = WorkerManager(database, get_workspace(), model_name)
    try:
        manager.start()
    except Exception:
        database.close()
        raise

    print()
    print("Lilith runtime online.")
    print(
        f"Persistent state: {database_path}"
    )
    print(f"Model: {model_name}")
    print(f"Workspace: {get_workspace()}")
    print("Type /help for commands; /quit to shut down.")
    print()

    try:
        while not shutdown_event.is_set():

            try:
                print("You: ", end="", flush=True)
                line = sys.stdin.readline()
                if not line:
                    break
                user_input = line.strip()

            except (
                EOFError,
                KeyboardInterrupt,
            ):
                break

            if not user_input:
                continue

            if handle_command(user_input, manager.store, manager):
                continue

            # ---------------------------------------------
            # Shutdown
            # ---------------------------------------------

            if user_input.lower() in {
                "/quit",
                "/exit",
                "/shutdown",
            }:
                break

            # ---------------------------------------------
            # Ordinary conversation
            # ---------------------------------------------

            from lilith.conversation import Conversation
            print("\nLilith: ", end="", flush=True)
            try:
                response, task_id = Conversation(database, conversation_gateway, router_gateway).reply(
                    user_input, lambda chunk: print(chunk, end="", flush=True))
                print(f"\n\nReflection queued as task #{task_id}.", flush=True)
            except Exception as error:
                print(f"\nReply interrupted: {error}\n")

    finally:
        shutdown_event.set()
        manager.close()

        database.audit(
            event_type="system_stop",
            message=(
                "Lilith runtime stopped "
                "cleanly."
            ),
        )

        database.close()

        print()
        print(
            "Lilith runtime offline."
        )


if __name__ == "__main__":
    main()
