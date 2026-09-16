# cache-catcher

The fanotify guard that watches the lancache filesystem on the NAS and emails
when cached game files disappear or become unreadable. It is the tripwire for
the **2026-07-31 mass-deletion incident** class: nginx's cache-manager evicting
from a nearly-full `keys_zone`, silently, for days.

## Why these files are here

Until 2026-09-16 this code lived **only** inside the running container, in the
persistent `/log` volume. There was no version control, no review, and no way to
test a change before it was the thing guarding the cache. Issue #337 was the
prompt: the guard had the deleting process on every log line and ignored it, so
an operator purge raised the same emergency alert as a real eviction.

| file | role |
|---|---|
| `delete_actor.py` | pure, stdlib-only: who deleted, and does it count as eviction |
| `fanotify_guard.py` | the deployed guard — loads `libc.so.6`, NAS-only |

`fanotify_guard.py` cannot be imported off the NAS (`ctypes.CDLL("libc.so.6")`
runs at import), which is exactly why the decision logic is in `delete_actor.py`
instead. That module is covered by `tests/tools/test_delete_actor.py` and runs in
CI like any other code.

## Deploying a change

Both files live in the container's `/log` volume, which survives `docker rm`:

```sh
scp tools/cache_catcher/delete_actor.py   karl@192.168.1.30:/tmp/
scp tools/cache_catcher/fanotify_guard.py karl@192.168.1.30:/tmp/
ssh karl@192.168.1.30 'docker cp /tmp/delete_actor.py   cache-catcher:/log/delete_actor.py && \
                       docker cp /tmp/fanotify_guard.py cache-catcher:/log/fanotify_guard.py && \
                       docker restart cache-catcher'
```

`/log/entrypoint.sh` runs the guard as the container's main process, so a restart
picks up the new code. Verify afterwards:

```sh
ssh karl@192.168.1.30 'docker logs --since 2m cache-catcher | head -5'
# expect: FANOTIFY guard started (delete+attrib; evict>=10/60s; ...)
```

## Alert semantics (#337)

| deleter | classified | alert |
|---|---|---|
| `python -m orchestrator.agent` | `orchestrator` | **NOTICE**, commanded purge, own cooldown |
| `nginx` | `nginx` | **ALERT**, eviction — names the actor |
| anything else, or unattributable | `other` | **ALERT**, eviction — names the actor |

**Unknown alerts.** Only a positively identified orchestrator agent is treated as
commanded. A process whose `/proc` entry vanished before the event was read still
counts toward the eviction signature — a monitor that guesses "probably fine" is
worse than none, because the incident it exists for ran unnoticed for days.

Config (SMTP credentials) stays in `/log/alert.env` on the NAS and is **not**
version-controlled.
