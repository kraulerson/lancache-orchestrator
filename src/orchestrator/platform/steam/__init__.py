"""Steam platform adapter — subprocess-isolated steam-next worker.

The subprocess (worker.py) is gevent-patched and runs in a separate
Python venv. The asyncio orchestrator process communicates with it via
SteamWorkerClient (client.py) over newline-delimited JSON pipes.
"""
