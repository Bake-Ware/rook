"""Voice configuration, resolved from four sources (highest first):

1. the voice process's own environment (legacy names such as ``WHISPER_MODEL``
   or the canonical ``ROOK_VOICE_WHISPER_MODEL``);
2. the hub: ``settings.fetch("voice")`` on worker ``rook``, called over the
   Rook MCP with ``ROOK_MCP_TOKEN`` (a token listed in
   ``core.settings.service_readers.voice``) at start, every
   ``settings_refresh_s`` seconds and on SIGHUP;
3. the last-known-good copy of the hub's values cached on disk
   (``settings_cache``; non-secret values only), used while the hub is
   unreachable;
4. the defaults declared in :mod:`rook.core.service_settings`.

Secrets (client token, model API key) arrive through the fetch and stay in
memory. Without ``ROOK_MCP_TOKEN`` the service runs on its environment and the
defaults only. Settings that apply at restart keep the value they had at start;
a later change is logged and reported to the hub as waiting for a restart.

``python -m services.voice.config`` prints the effective settings and where each
one came from (secrets as fingerprints); ``--watch`` keeps refreshing.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Awaitable, Callable

from rook.core import settings as cs
from rook.core.service_settings import DECISION, VOICE, VOICE_CLIENT_SETTINGS

log = logging.getLogger('rook.voice.config')

HUB_WORKER = 'rook'
FETCH_TIMEOUT = 15.0


def _decode(raw: Any) -> dict:
    """A rook_call reply (JSON text, sometimes JSON-in-JSON) as a dict."""
    d = raw
    for _ in range(2):
        if isinstance(d, str):
            d = json.loads(d)
    if not isinstance(d, dict):
        raise ValueError('unexpected reply from the hub')
    return d


class ServiceConfig:
    """Effective settings of one service namespace; see the module docstring."""

    def __init__(self, namespace: str, schema: list, environ: Any = None,
                 cache_path: str | os.PathLike | None = None) -> None:
        self.namespace = namespace
        self.schema = {s.name: s for s in schema}
        self.environ = environ if environ is not None else os.environ
        self._cache_override = cache_path
        self.hub: dict = {}             # last fetched values (secrets included, memory only)
        self.hub_stored: set = set()    # names with a value saved on the hub
        self.hub_ok = False             # a fetch succeeded in this process
        self.users: dict = {}           # per-user overrides from the hub
        self.cache: dict = {}           # non-secret values from the cache file
        self.cache_at: float | None = None
        self.frozen: dict = {}          # restart-apply settings as they were at start
        self.pending: set = set()       # frozen names whose stored value changed since
        self.fetched_at: float | None = None
        self.last_error = ''
        self.started_at = time.time()
        self.fetcher: Callable[[], Awaitable[dict]] | None = None
        self.reporter: Callable[[dict], Awaitable[Any]] | None = None
        self._reported: tuple | None = None
        self._warned: set = set()
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    # -- resolution -------------------------------------------------------
    def _setting(self, name: str):
        try:
            return self.schema[name]
        except KeyError:
            raise KeyError(f'{self.namespace}.{name} is not declared in '
                           'rook/core/service_settings.py') from None

    def _warn_once(self, key: tuple, msg: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *args)

    def _env(self, s) -> tuple | None:
        for var in s.env_names():
            raw = self.environ.get(var)
            if raw is None:
                continue
            if s.secret:
                return var, raw
            try:
                return var, s.coerce(raw)
            except (ValueError, TypeError) as error:
                self._warn_once(('env', var, raw), 'ignoring %s: %s', var, error)
        return None

    def _coerce(self, s, value: Any, source: str) -> tuple[bool, Any]:
        if s.secret:
            return True, value
        try:
            return True, s.coerce(value)
        except (ValueError, TypeError) as error:
            self._warn_once((source, s.name, repr(value)), 'ignoring the %s value of %s.%s: %s',
                            source, self.namespace, s.name, error)
            return False, None

    def _default(self, s) -> Any:
        return s.default() if callable(s.default) else s.default

    def _resolve_live(self, name: str) -> tuple[Any, str, str]:
        s = self._setting(name)
        got = self._env(s)
        if got is not None:
            return got[1], 'env', got[0]
        if not s.bootstrap and s.scope == 'hub':
            if self.hub_ok:
                if name in self.hub_stored and self.hub.get(name) is not None:
                    ok, value = self._coerce(s, self.hub[name], 'hub')
                    if ok:
                        return value, 'hub', ''
            elif not s.secret and name in self.cache:
                ok, value = self._coerce(s, self.cache[name], 'cache')
                if ok:
                    return value, 'cache', ''
        return self._default(s), 'default', ''

    def resolve(self, name: str) -> tuple[Any, str, str]:
        """``(value, source, detail)``; source is env|hub|cache|default and
        detail the environment variable when source is env."""
        if name in self.frozen:
            return self.frozen[name]
        return self._resolve_live(name)

    def get(self, name: str) -> Any:
        return self.resolve(name)[0]

    def source(self, name: str) -> str:
        return self.resolve(name)[1]

    def freeze(self) -> None:
        """Fix restart-apply settings at their current values (at start)."""
        for name, s in self.schema.items():
            if s.apply in ('restart', 'reload') or s.bootstrap:
                self.frozen[name] = self._resolve_live(name)
        self.pending.clear()

    # -- display ----------------------------------------------------------
    def shown(self, name: str) -> Any:
        value = self.get(name)
        if not self._setting(name).secret:
            return value
        return f'{cs.MASK} (fp {cs.fingerprint(value)})' if value else None

    def snapshot(self) -> dict:
        out = {}
        for name in self.schema:
            _, source, detail = self.resolve(name)
            row = {'value': self.shown(name), 'source': source}
            if detail:
                row['env'] = detail
            if name in self.pending:
                row['pending'] = 'restart'
            out[name] = row
        return out

    def announce(self, names: list | None = None, why: str = '') -> None:
        """Log where each value came from: one line per non-default value, one
        line naming the settings left at their default."""
        names = list(self.schema) if names is None else names
        defaults = []
        for name in names:
            _, source, detail = self.resolve(name)
            if source == 'default' and not why:
                defaults.append(name)
                continue
            where = f'{source} {detail}' if detail else source
            log.info('%s%s.%s = %r (%s)', why, self.namespace, name, self.shown(name), where)
        if defaults:
            log.info('%s settings at their defaults: %s', self.namespace, ', '.join(defaults))

    # -- cache ------------------------------------------------------------
    def cache_path(self) -> Path | None:
        if self._cache_override:
            return Path(self._cache_override)
        if 'settings_cache' not in self.schema:
            return None
        path = self.get('settings_cache')
        if path:
            return Path(path)
        return Path(self.get('model_dir') or '.') / f'{self.namespace}-settings.json'

    def load_cache(self) -> bool:
        path = self.cache_path()
        if path is None or not path.exists():
            return False
        try:
            data = json.loads(path.read_text())
            if data.get('namespace') != self.namespace:
                raise ValueError(f'holds {data.get("namespace")!r}')
            values = data.get('values') or {}
        except (OSError, ValueError, AttributeError) as error:
            log.warning('ignoring the settings cache %s: %s', path, error)
            return False
        self.cache = {n: v for n, v in values.items()
                      if n in self.schema and not self.schema[n].secret}
        self.cache_at = data.get('fetched_at')
        return True

    def save_cache(self) -> None:
        path = self.cache_path()
        if path is None:
            return
        values = {n: self.hub[n] for n in sorted(self.hub_stored)
                  if n in self.schema and not self.schema[n].secret
                  and not self.schema[n].bootstrap and self.hub.get(n) is not None}
        body = json.dumps({'namespace': self.namespace, 'fetched_at': self.fetched_at,
                           'values': values}, indent=1, sort_keys=True)
        tmp = path.with_name(path.name + '.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(body)
            os.replace(tmp, path)
            self.cache, self.cache_at = values, self.fetched_at
        except OSError as error:
            log.warning('could not write the settings cache %s: %s', path, error)

    # -- hub --------------------------------------------------------------
    def apply_reply(self, reply: dict) -> list[str]:
        """Take a ``settings.fetch`` reply; returns the names whose effective
        value or source changed (restart-apply ones become pending)."""
        if not isinstance(reply, dict) or not isinstance(reply.get('values'), dict):
            raise ValueError('settings.fetch reply has no values')
        before = {n: self._resolve_live(n) for n in self.schema}
        values = reply['values']
        self.hub = {n: v for n, v in values.items() if n in self.schema}
        if isinstance(reply.get('stored'), list):
            self.hub_stored = {n for n in reply['stored'] if n in self.schema}
        else:  # an older hub: it does not say which values are stored
            self.hub_stored = {n for n, v in self.hub.items() if v is not None}
        self.users = reply.get('users') if isinstance(reply.get('users'), dict) else {}
        self.hub_ok, self.fetched_at, self.last_error = True, time.time(), ''
        self.save_cache()
        changed = []
        for name in self.schema:
            now = self._resolve_live(name)
            if now[:2] != before[name][:2]:
                changed.append(name)
        self._update_pending()
        return changed

    def _update_pending(self) -> None:
        for name, (value, *_rest) in self.frozen.items():
            if self._resolve_live(name)[0] != value:
                if name not in self.pending:
                    log.info('%s.%s changed to %r (%s); restart the service to apply it',
                             self.namespace, name, self._shown_live(name),
                             self._resolve_live(name)[1])
                self.pending.add(name)
            else:
                self.pending.discard(name)

    def _shown_live(self, name: str) -> Any:
        value = self._resolve_live(name)[0]
        if not self._setting(name).secret:
            return value
        return f'{cs.MASK} (fp {cs.fingerprint(value)})' if value else None

    def _fallback(self) -> str:
        if self.hub_ok:
            return 'keeping the values fetched ' + time.strftime('%H:%M:%S', time.localtime(self.fetched_at))
        if self.cache:
            return f'using the cached copy ({len(self.cache)} values) from {self.cache_path()}'
        return 'using the environment and defaults'

    async def refresh(self) -> list[str] | None:
        """Fetch from the hub once. ``None`` when the hub is off or unreachable
        (the current values stay), else the changed names."""
        if self.fetcher is None:
            return None
        try:
            reply = await asyncio.wait_for(self.fetcher(), FETCH_TIMEOUT + 5)
            changed = self.apply_reply(reply)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the hub being down is expected
            msg = f'{type(error).__name__}: {error}'[:300]
            if msg != self.last_error:
                log.warning('could not fetch %s settings from the hub (%s); %s',
                            self.namespace, msg, self._fallback())
            self.last_error = msg
            return None
        for name in changed:
            if name not in self.pending:
                _, source, detail = self.resolve(name)
                log.info('%s.%s = %r (%s)', self.namespace, name, self.shown(name),
                         f'{source} {detail}' if detail else source)
        await self.report()
        return changed

    def report_body(self) -> dict:
        env, values = {}, {}
        for name, s in self.schema.items():
            got = self._env(s)
            if got is None:
                continue
            env[name] = got[0]
            if not s.secret:
                values[name] = got[1]
        return {'namespace': self.namespace, 'env': env, 'values': values,
                'started_at': self.started_at, 'pending': sorted(self.pending)}

    async def report(self) -> None:
        """Tell the hub which settings the environment locks and what waits
        for a restart; only when that changed since the last report."""
        if self.reporter is None:
            return
        body = self.report_body()
        key = (json.dumps(body['env'], sort_keys=True), tuple(body['pending']))
        if key == self._reported:
            return
        try:
            await asyncio.wait_for(self.reporter(body), FETCH_TIMEOUT + 5)
            self._reported = key
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            log.info('could not report %s settings to the hub: %s', self.namespace, error)

    async def load(self) -> None:
        """At start: read the cache (if the hub is configured), fetch once, log
        every value's source."""
        if self.fetcher is not None:
            self.load_cache()
            await self.refresh()
        else:
            log.info('%s: no hub token configured; using the environment and defaults only',
                     self.namespace)

    # -- refresh loop -----------------------------------------------------
    def request_refresh(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def run(self, interval: float | None = None,
                  on_refresh: Callable[[list | None], Any] | None = None) -> None:
        """Refresh every ``settings_refresh_s`` (or ``interval``) seconds and
        on SIGHUP, until cancelled."""
        self._wake = asyncio.Event()
        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError, AttributeError):
            loop.add_signal_handler(signal.SIGHUP, self._wake.set)
        try:
            while True:
                every = interval if interval is not None else (
                    self.get('settings_refresh_s') if 'settings_refresh_s' in self.schema else 300)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), every if every and every > 0 else None)
                self._wake.clear()
                changed = await self.refresh()
                if on_refresh is not None:
                    on_refresh(changed)
        finally:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError, AttributeError):
                loop.remove_signal_handler(signal.SIGHUP)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


# -- hub transport ------------------------------------------------------------

def _hub_call(url: Callable[[], str], token: Callable[[], str], cap: str):
    async def call(args: dict) -> Any:
        from .rookmcp import RookMCP
        raw = await RookMCP(url=url(), token=token(), timeout=FETCH_TIMEOUT).call(
            'rook_call', {'worker': HUB_WORKER, 'cap': cap, 'args': args})
        reply = _decode(raw)
        if not reply.get('ok'):
            raise RuntimeError(str(reply.get('error') or reply)[:300])
        return reply.get('result')
    return call


def connect_hub(config: ServiceConfig, via: ServiceConfig | None = None) -> bool:
    """Wire ``config`` to fetch from the hub with ``via``'s (the voice
    config's) ``mcp_url``/``mcp_token``. False when no token is configured."""
    via = via or config
    if not via.get('mcp_token'):
        config.fetcher = config.reporter = None
        return False
    fetch = _hub_call(lambda: via.get('mcp_url'), lambda: via.get('mcp_token'), 'settings.fetch')
    report = _hub_call(lambda: via.get('mcp_url'), lambda: via.get('mcp_token'), 'settings.report')

    async def fetcher() -> dict:
        return await fetch({'namespace': config.namespace})

    async def reporter(body: dict) -> Any:
        return await report(body)

    config.fetcher, config.reporter = fetcher, reporter
    return True


#: The voice service's settings; ``cfg(name)`` reads one.
CONFIG = ServiceConfig('voice', VOICE)


def cfg(name: str) -> Any:
    return CONFIG.get(name)


def source(name: str) -> str:
    return CONFIG.source(name)


_started = False


async def start_service() -> None:
    """Service start (once per process): connect, fetch once (falling back
    to the cache or the environment), log every source, freeze restart-apply
    values."""
    global _started
    if _started:
        return
    _started = True
    connect_hub(CONFIG)
    await CONFIG.load()
    CONFIG.announce()
    CONFIG.freeze()


def load_blocking() -> None:
    """``start_service`` from synchronous code (before uvicorn starts)."""
    asyncio.run(start_service())


def decision_config(cache_path: str | os.PathLike | None = None) -> ServiceConfig:
    """The decision engine's settings, fetched with the voice service's hub
    token (which must also be listed in core.settings.service_readers.decision)."""
    config = ServiceConfig('decision', DECISION, cache_path=cache_path)
    connect_hub(config, via=CONFIG)
    return config


# -- CLI ----------------------------------------------------------------------

def _table(config: ServiceConfig) -> str:
    rows = []
    for name, row in config.snapshot().items():
        where = row['source'] + (f" {row['env']}" if row.get('env') else '')
        if row.get('pending'):
            where += ' (restart pending)'
        note = ' [read by the app]' if config.namespace == 'voice' and name in VOICE_CLIENT_SETTINGS else ''
        rows.append(f'{config.namespace}.{name:<24} {json.dumps(row["value"])[:60]:<62} {where}{note}')
    return '\n'.join(rows)


async def _cli(args) -> int:
    if args.namespace == 'decision':
        connect_hub(CONFIG)
        config = decision_config(args.cache)
        await config.load()
    else:
        config = CONFIG
        if args.cache:
            config._cache_override = args.cache
        await start_service()

    def emit(event: str, changed: list | None = None) -> None:
        if args.json:
            print(json.dumps({'event': event, 'changed': changed, 'hub_ok': config.hub_ok,
                              'error': config.last_error or None,
                              'settings': config.snapshot()}), flush=True)
        else:
            print(_table(config), flush=True)
            if config.last_error:
                print(f'hub: {config.last_error}', flush=True)

    emit('loaded')
    if not args.watch:
        return 0
    await config.run(interval=args.interval, on_refresh=lambda changed: emit('refreshed', changed))
    return 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog='python -m services.voice.config',
                                 description='Show the effective voice settings and their sources.')
    ap.add_argument('--namespace', choices=('voice', 'decision'), default='voice')
    ap.add_argument('--json', action='store_true', help='one JSON object per load/refresh')
    ap.add_argument('--watch', action='store_true', help='keep refreshing (SIGHUP: now)')
    ap.add_argument('--interval', type=float, default=None,
                    help='refresh interval in seconds (default: settings_refresh_s)')
    ap.add_argument('--cache', default=None, help='settings cache file (default: settings_cache)')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try:
        return asyncio.run(_cli(args))
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
