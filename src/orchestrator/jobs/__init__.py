"""Async jobs subsystem (BL11).

Generic single-loop asyncio job dispatcher. Handlers register against
`jobs.kind` values via the registry in `handlers/__init__.py`.
"""
