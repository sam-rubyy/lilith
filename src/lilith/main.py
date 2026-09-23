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
from lilith.model_gateway import OllamaGateway
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

    self_state = SelfState(database)

    database.audit(
        event_type="system_start",
        message=(
            f"Lilith runtime started using "
            f"model {model_name}."
        ),
    )

    gateway = OllamaGateway(model_name)

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
            # Remember
            # ---------------------------------------------

            if user_input.lower().startswith(
                "/remember "
            ):
                memory_text = user_input[
                    len("/remember "):
                ].strip()

                if not memory_text:
                    print(
                        "Usage: /remember <something>"
                    )
                    continue

                memory_id = (
                    database.save_memory(
                        memory_type="episodic",
                        content=memory_text,
                        source="owner",
                        confidence=1.0,
                        importance=0.8,
                    )
                )

                database.audit(
                    event_type="memory_created",
                    actor="owner",
                    message=(
                        f"Created memory "
                        f"{memory_id}."
                    ),
                )

                print()
                print(
                    f"Lilith stored memory "
                    f"#{memory_id}."
                )
                print()

                continue

            # ---------------------------------------------
            # Memories
            # ---------------------------------------------

            if (
                user_input.lower()
                == "/memories"
            ):
                memories = (
                    database.recent_memories(
                        limit=20
                    )
                )

                print()

                if not memories:
                    print(
                        "No memories stored."
                    )

                for memory in memories:
                    print(
                        f"#{memory['id']} "
                        f"[{memory['type']}] "
                        f"{memory['content']}"
                    )

                print()

                continue

            # ---------------------------------------------
            # Self model
            # ---------------------------------------------

            if user_input.lower() == "/self":
                print()
                print(self_state.render())
                print()
                continue

            # ---------------------------------------------
            # Affect
            #
            # /affect
            # /affect curiosity 0.8
            # ---------------------------------------------

            if user_input.lower().startswith(
                "/affect"
            ):
                parts = user_input.split()

                if len(parts) == 1:
                    print()

                    for name, value in (
                        self_state
                        .affect()
                        .items()
                    ):
                        print(
                            f"{name:12} "
                            f"{value:.3f}"
                        )

                    print()
                    continue

                if len(parts) != 3:
                    print(
                        "Usage: "
                        "/affect <name> <0.0-1.0>"
                    )
                    continue

                name = parts[1].lower()

                try:
                    value = float(parts[2])

                    self_state.set_affect(
                        name,
                        value,
                    )

                except ValueError as error:
                    print(error)
                    continue

                database.audit(
                    event_type=(
                        "affect_owner_override"
                    ),
                    actor="owner",
                    message=(
                        f"{name} set to "
                        f"{value:.3f}"
                    ),
                )

                print(
                    f"{name} = "
                    f"{self_state.affect()[name]:.3f}"
                )

                continue

            # ---------------------------------------------
            # Interest
            #
            # /interest compiler design
            # ---------------------------------------------

            if user_input.lower().startswith(
                "/interest "
            ):
                topic = user_input[
                    len("/interest "):
                ].strip()

                if not topic:
                    print(
                        "Usage: "
                        "/interest <topic>"
                    )
                    continue

                self_state.add_interest(
                    topic,
                    amount=0.15,
                )

                database.audit(
                    event_type="interest_updated",
                    actor="owner",
                    message=topic,
                )

                print()
                print(
                    f"Interest strengthened: "
                    f"{topic}"
                )
                print()

                continue

            # ---------------------------------------------
            # Ordinary conversation
            # ---------------------------------------------

            from lilith.conversation import Conversation
            print("\nLilith: ", end="", flush=True)
            try:
                response, task_id = Conversation(database, gateway).reply(
                    user_input, lambda chunk: print(chunk, end="", flush=True))
                print(f"\n\nReflection queued as task #{task_id}.")
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
