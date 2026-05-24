"""Platform adapters (Steam, Epic, ...). Each platform lives in its own
sub-package and is isolated from the orchestrator process where its
runtime constraints (e.g. gevent monkey-patching) would conflict."""
