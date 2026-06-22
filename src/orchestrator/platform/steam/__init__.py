"""Steam platform adapter.

Steam prefill and library enumeration are delegated to the host SteamPrefill
binary via `SteamPrefillDriver` (prefill_driver.py); game metadata is resolved
through the public Steam store API (store.py). The data-plane agent runs these
on the lancache host; the control plane talks to it over HTTP.
"""
