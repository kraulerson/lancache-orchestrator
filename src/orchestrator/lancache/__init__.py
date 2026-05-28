"""Lancache integration subsystem (ID2 self-test + downstream features).

This package owns the orchestrator's interactions with the lancache
nginx cache as a SERVICE — heartbeat probes for /health, future
prefill steering, future access-log tailing. The on-disk validator
(F7, reads `lancache_nginx_cache_path`) is a separate subsystem that
lives under `orchestrator.validator` when it ships.
"""
