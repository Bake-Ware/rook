"""Background-only voice tools: timers, weather, calendar, mail, Rook tasks,
music and Home Assistant (docs/design/voice-front-background.md).

Each tool returns short text for the Background model and may put facts on the
conversation board. Policy (who may use what) is decided in policy.py and
enforced by the Background agent before a tool runs; device reads are pinned
again here by identity.authorize_read.
"""
import asyncio
import difflib
import json
import math
import os
import re
import time
from datetime import datetime, timedelta

import httpx

from .identity import authorize_read, current_identity
from .rookmcp import RookMCP
from .thinking import decode, function

MAIL_PACKAGES = ['com.google.android.gm', 'com.microsoft.office.outlook']
MAX_TIMER_S = 7 * 86400

SCHEMAS = {
    'timer_set': function('timer_set', 'Set a timer the user\'s device rings. Give seconds (a duration) or at (a clock '
                          'time like "19:30", "7pm" or ISO 8601), plus a short label.',
                          {'seconds': {'type': 'integer'}, 'at': {'type': 'string'}, 'label': {'type': 'string'}}),
    'timer_list': function('timer_list', 'List the active timers in this conversation.', {}),
    'timer_cancel': function('timer_cancel', 'Cancel a timer by id or by label.',
                             {'id': {'type': 'string'}, 'label': {'type': 'string'}}),
    'weather': function('weather', 'Current weather and today\'s forecast for the caller\'s location '
                        '(device location when allowed, otherwise the configured home location).', {}),
    'calendar_list': function('calendar_list', 'Read events from the caller\'s phone calendar (Google Calendar, '
                              'Outlook). start/end: ISO 8601 or "now"; default the next 24 hours.',
                              {'start': {'type': 'string'}, 'end': {'type': 'string'}, 'limit': {'type': 'integer'}}),
    'mail_list': function('mail_list', 'Recent Gmail/Outlook mail notifications on the caller\'s phone: sender and '
                          'subject. Read-only; no mail history.', {'limit': {'type': 'integer'}}),
    'tasks_deck': function('tasks_deck', 'The owner\'s open Rook tasks (read-only).', {}),
    'task_get': function('task_get', 'One Rook task by id (read-only).', {'id': {'type': 'string'}}, ('id',)),
    'music': function('music', 'Control the pianobar music player.',
                      {'action': {'type': 'string', 'enum': ['now_playing', 'play', 'pause', 'toggle', 'next',
                                                             'love', 'ban', 'volume_up', 'volume_down',
                                                             'stations']}}, ('action',)),
    'ha_list': function('ha_list', 'List Home Assistant lights, switches, scenes and media players with their state.',
                        {'domain': {'type': 'string', 'enum': ['light', 'switch', 'scene', 'media_player']}}),
    'ha_call': function('ha_call', 'Control one Home Assistant entity. target: entity id or its spoken name. '
                        'action: turn_on, turn_off, toggle (lights, switches, media players), activate (scenes), '
                        'play, pause, next (media players).',
                        {'target': {'type': 'string'},
                         'action': {'type': 'string', 'enum': ['turn_on', 'turn_off', 'toggle', 'activate',
                                                               'play', 'pause', 'next']}}, ('target', 'action')),
}

MUSIC_CAPS = {'now_playing': 'cmd.pianobar-now-playing', 'play': 'cmd.pianobar-songplay',
              'pause': 'cmd.pianobar-songpause', 'toggle': 'cmd.pianobar-songpausetoggle',
              'next': 'cmd.pianobar-songnext', 'love': 'cmd.pianobar-songlove', 'ban': 'cmd.pianobar-songban',
              'volume_up': 'cmd.pianobar-volup', 'volume_down': 'cmd.pianobar-voldown',
              'stations': 'cmd.pianobar-stations'}

WEATHER_CODES = {0: 'clear', 1: 'mostly clear', 2: 'partly cloudy', 3: 'overcast', 45: 'foggy', 48: 'foggy',
                 51: 'light drizzle', 53: 'drizzle', 55: 'heavy drizzle', 56: 'freezing drizzle',
                 57: 'freezing drizzle', 61: 'light rain', 63: 'rain', 65: 'heavy rain', 66: 'freezing rain',
                 67: 'freezing rain', 71: 'light snow', 73: 'snow', 75: 'heavy snow', 77: 'snow grains',
                 80: 'rain showers', 81: 'rain showers', 82: 'heavy rain showers', 85: 'snow showers',
                 86: 'heavy snow showers', 95: 'thunderstorms', 96: 'thunderstorms with hail',
                 99: 'thunderstorms with hail'}


def _tz():
    name = os.environ.get('VOICE_TZ', '').strip()
    if name:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    return None


def now_local():
    return datetime.now(_tz()).astimezone(_tz()) if _tz() else datetime.now().astimezone()


def spoken_time(dt):
    return dt.strftime('%I:%M %p').lstrip('0')


def parse_at(text, now=None):
    """A clock time ("7pm", "19:30", "7:30 am") or ISO 8601 -> aware datetime in the future."""
    now = now or now_local()
    s = str(text).strip().lower().replace('.', '')
    m = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', s)
    if m:
        hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if half:
            if not 1 <= hour <= 12:
                raise ValueError('Invalid clock time')
            hour = hour % 12 + (12 if half == 'pm' else 0)
        if hour > 23 or minute > 59:
            raise ValueError('Invalid clock time')
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)
        return when
    try:
        when = datetime.fromisoformat(str(text).strip().replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Give at as a clock time like 19:30 or 7pm, or ISO 8601') from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=now.tzinfo)
    return when


def duration_words(seconds):
    seconds = int(seconds)
    parts = []
    for size, name in ((3600, 'hour'), (60, 'minute'), (1, 'second')):
        if seconds >= size:
            n, seconds = divmod(seconds, size)
            parts.append(f'{n} {name}' + ('s' if n != 1 else ''))
    return ' '.join(parts) or '0 seconds'


async def device_read(worker, cap, args):
    """rook_call of one read capability on one device; returns its result object."""
    payload = {'worker': worker, 'cap': cap}
    if args:
        payload['args'] = args
    reply = decode(await RookMCP(timeout=30).call('rook_call', payload))
    if not isinstance(reply, dict) or not reply.get('ok', False):
        raise ValueError(str((reply or {}).get('error') if isinstance(reply, dict) else reply)[:300] or 'Device read failed')
    result = reply.get('result')
    if isinstance(result, dict) and result.get('ok') is False:
        raise ValueError(str(result.get('error') or 'The capability reported failure')[:300])
    return result


class HomeAssistant:
    """Minimal Home Assistant REST client: cached entity list and allowlisted services."""
    DOMAINS = ('light', 'switch', 'scene', 'media_player')
    SERVICES = {
        'light': {'turn_on': 'turn_on', 'turn_off': 'turn_off', 'toggle': 'toggle'},
        'switch': {'turn_on': 'turn_on', 'turn_off': 'turn_off', 'toggle': 'toggle'},
        'scene': {'activate': 'turn_on', 'turn_on': 'turn_on'},
        'media_player': {'turn_on': 'turn_on', 'turn_off': 'turn_off', 'toggle': 'toggle',
                         'play': 'media_play', 'pause': 'media_pause', 'next': 'media_next_track'},
    }

    def __init__(self, url=None, token=None, verify=None, transport=None, ttl=60):
        self.url = (url if url is not None else os.environ.get('VOICE_HASS_URL', '')).rstrip('/')
        self.token = token if token is not None else os.environ.get('VOICE_HASS_TOKEN', '')
        self.verify = verify if verify is not None else os.environ.get('VOICE_HASS_VERIFY_TLS', '1') != '0'
        self.transport, self.ttl = transport, ttl
        self.cache = None

    @property
    def configured(self):
        return bool(self.url and self.token)

    def _client(self):
        kwargs = {'timeout': 10, 'verify': self.verify, 'trust_env': False,
                  'headers': {'Authorization': f'Bearer {self.token}'}}
        if self.transport is not None:
            kwargs['transport'] = self.transport
        return httpx.AsyncClient(**kwargs)

    async def entities(self, refresh=False):
        if not self.configured:
            raise ValueError('Home Assistant is not configured (VOICE_HASS_URL, VOICE_HASS_TOKEN)')
        if refresh or self.cache is None or time.monotonic() - self.cache[0] > self.ttl:
            async with self._client() as client:
                response = await client.get(self.url + '/api/states')
                response.raise_for_status()
                rows = response.json()
            entities = []
            for row in rows if isinstance(rows, list) else []:
                eid = str(row.get('entity_id', ''))
                if eid.split('.', 1)[0] in self.DOMAINS:
                    name = (row.get('attributes') or {}).get('friendly_name') or eid.split('.', 1)[-1].replace('_', ' ')
                    entities.append({'entity_id': eid, 'name': str(name), 'state': str(row.get('state', ''))})
            self.cache = (time.monotonic(), entities)
        return self.cache[1]

    @staticmethod
    def _norm(text):
        return ' '.join(re.sub(r'[^a-z0-9 ]+', ' ', str(text).lower().replace('_', ' ')).split())

    # Words that say which kind of thing, not which one.
    FILLERS = frozenset({'the', 'my', 'a', 'an', 'please', 'in', 'on', 'of'})
    DOMAIN_WORDS = {'light': {'light', 'lights'}, 'switch': {'switch'}, 'scene': {'scene'},
                    'media_player': {'media', 'player'}}
    SURE, MARGIN = .85, .1

    @staticmethod
    def _stem(word):
        return word[:-1] if len(word) > 3 and word.endswith('s') else word

    def _score(self, want, words, e):
        """(score, every request word names this entity). Score 1.0 is an exact name."""
        domain, obj = e['entity_id'].split('.', 1)
        labels = (self._norm(e['name']), self._norm(obj))
        if want in labels:
            return 1.0, True
        name_words = {self._stem(w) for label in labels for w in label.split()}
        vocab = name_words | {self._stem(w) for w in self.DOMAIN_WORDS.get(domain, ())}
        covers = bool(words) and all(w in vocab for w in words)
        ratio = max(difflib.SequenceMatcher(None, want, label).ratio() for label in labels)
        if not covers:
            return min(ratio, self.SURE - .01), False
        named = {self._stem(w) for w in self._norm(e['name']).split()} or name_words
        # All the request's words fit; the more of the entity's own name it says, the surer.
        return max(ratio, self.SURE + .15 * len(named & set(words)) / len(named)), True

    def match(self, target, entities, domain=None):
        """(entity, candidates). Acts only on an exact entity id or one clear match:
        score >= SURE, every significant word of the request belongs to that entity,
        and MARGIN ahead of the runner-up. Otherwise entity is None and candidates
        are the closest names, for asking the user which one."""
        pool = [e for e in entities if not domain or e['entity_id'].startswith(domain + '.')]
        target = str(target).strip()
        for e in pool:
            if e['entity_id'] == target:
                return e, []
        want = self._norm(target)
        words = [self._stem(w) for w in want.split() if w not in self.FILLERS]
        want = ' '.join(w for w in want.split() if w not in self.FILLERS)
        if not want:
            return None, []
        scored = sorted(((*self._score(want, words, e), e) for e in pool), key=lambda r: r[0], reverse=True)
        if scored:
            score, covers, best = scored[0]
            runner = scored[1][0] if len(scored) > 1 else 0.0
            if covers and score >= self.SURE and score - runner >= self.MARGIN:
                return best, []
        return None, [e for score, _, e in scored[:3] if score >= .4]

    async def call(self, target, action):
        entities = await self.entities()
        entity, candidates = self.match(target, entities)
        if entity is None:
            hint = ('. Closest: ' + ', '.join(f"{e['name']} ({e['entity_id']})" for e in candidates) +
                    '. Ask the user which one they mean; do not pick one.') if candidates else ''
            raise ValueError(f'No light, switch, scene or media player clearly matches {target!r}' + hint)
        domain = entity['entity_id'].split('.', 1)[0]
        service = self.SERVICES.get(domain, {}).get(action)
        if domain not in self.DOMAINS or service is None:
            raise ValueError(f'{action} is not allowed for {domain} entities')
        async with self._client() as client:
            response = await client.post(f'{self.url}/api/services/{domain}/{service}',
                                         json={'entity_id': entity['entity_id']})
            response.raise_for_status()
        self.cache = None
        return entity, service


class Toolbox:
    """Background tools for one connection. Collaborators are injectable for tests."""

    def __init__(self, session, store, board, *, timers_enabled=False, emit_timer=None, read=None, mcp=None,
                 http_transport=None, hass=None, on_board=None):
        self.session, self.store, self.board = session, store, board
        self.timers_enabled = timers_enabled
        self.emit_timer = emit_timer or (lambda event: None)
        self.read = read or device_read
        self.mcp = mcp or RookMCP(timeout=30)
        self.http_transport = http_transport
        self.hass = hass or HomeAssistant()
        self.on_board = on_board or (lambda item: None)
        self.location = None

    def note(self, key, text, ttl_s=600, untrusted=False):
        item = self.board.put(key, text, 'background', ttl_s, untrusted)
        if item:
            self.on_board(item)
        return item

    async def run(self, name, args):
        handler = getattr(self, 'tool_' + name, None)
        if handler is None:
            raise ValueError('Unknown background tool: ' + name)
        return await handler(args)

    # --- timers ------------------------------------------------------------
    def _timer_event(self, action, timer):
        event = {'type': 'timer', 'action': action, 'id': timer['id'], 'label': timer['label']}
        if action == 'set':
            event['fires_at'] = int(timer['fires_at'])
            event['duration_s'] = int(timer['duration_s'])
        return event

    def refresh_timer_board(self):
        timers = self.store.timers(self.session)
        if not timers:
            self.board.remove('timers')
            return
        now = time.time() * 1000
        parts = [f"{t['label'] or 'timer'} ({duration_words(max(1, math.ceil((t['fires_at'] - now) / 1000)))} left)"
                 for t in timers[:5]]
        # Labels are model-written: Front may say them, Background never acts on them.
        self.note('timers', 'Active timers: ' + '; '.join(parts) + '.', 60, untrusted=True)

    async def tool_timer_set(self, args):
        if not self.timers_enabled:
            raise ValueError('This device cannot ring timers (its app did not enable timers)')
        label = ' '.join(str(args.get('label') or '').split())[:60]
        seconds, at = args.get('seconds'), args.get('at')
        now_ms = int(time.time() * 1000)
        if seconds is not None:
            if isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 < seconds <= MAX_TIMER_S:
                raise ValueError('seconds must be between 1 and 604800')
            timer = self.store.add_timer(self.session, label, now_ms + seconds * 1000, seconds)
            spoken = f'{duration_words(seconds)} timer' + (f' for {label}' if label else '') + ' is set.'
        elif at:
            when = parse_at(at)
            fires = int(when.timestamp() * 1000)
            if not now_ms < fires <= now_ms + MAX_TIMER_S * 1000:
                raise ValueError('That time is in the past or more than a week away')
            timer = self.store.add_timer(self.session, label, fires, 0)
            spoken = (f'{label.capitalize()} timer' if label else 'Timer') + f' set for {spoken_time(when.astimezone(now_local().tzinfo))}.'
        else:
            raise ValueError('Give seconds or at')
        self.emit_timer(self._timer_event('set', timer))
        self.refresh_timer_board()
        return spoken

    async def tool_timer_list(self, args):
        timers = self.store.timers(self.session)
        self.refresh_timer_board()
        if not timers:
            return 'No active timers.'
        now = time.time() * 1000
        return 'Active timers: ' + '; '.join(
            f"{t['label'] or 'timer'} (id {t['id'][:8]}, {duration_words(max(1, math.ceil((t['fires_at'] - now) / 1000)))} left)"
            for t in timers)

    async def tool_timer_cancel(self, args):
        timers = self.store.timers(self.session)
        want_id, want_label = str(args.get('id') or '').strip(), str(args.get('label') or '').strip().lower()
        chosen = [t for t in timers if want_id and (t['id'] == want_id or t['id'].startswith(want_id))]
        if not chosen and want_label:
            chosen = [t for t in timers if t['label'].lower() == want_label] or \
                     [t for t in timers if want_label in t['label'].lower()]
        if not chosen and not want_id and not want_label and len(timers) == 1:
            chosen = timers
        if not chosen:
            raise ValueError('No matching active timer' if timers else 'There are no active timers')
        if len(chosen) > 1:
            raise ValueError('More than one timer matches: ' + ', '.join(t['label'] or t['id'][:8] for t in chosen))
        timer = chosen[0]
        self.store.remove_timer(self.session, timer['id'])
        self.emit_timer(self._timer_event('cancel', timer))
        self.refresh_timer_board()
        return (f"{timer['label'].capitalize()} timer" if timer['label'] else 'Timer') + ' cancelled.'

    def client_cancel(self, tid):
        """The device cancelled a timer: forget it, no echo; unknown ids are ignored."""
        if isinstance(tid, str) and tid and self.store.remove_timer(self.session, tid):
            self.refresh_timer_board()
            return True
        return False

    # --- device reads ------------------------------------------------------
    def _own_worker(self, cap):
        identity = current_identity.get()
        if not identity.worker:
            raise ValueError('No device is mapped to this voice key, so I cannot read its ' +
                             {'calendar.list': 'calendar', 'notify.list': 'notifications'}.get(cap, 'data'))
        authorize_read(cap, identity.worker)
        return identity.worker

    async def tool_calendar_list(self, args):
        worker = self._own_worker('calendar.list')
        call = {'start': str(args.get('start') or 'now')}
        call['end'] = str(args.get('end') or int((time.time() + 86400) * 1000))
        limit = args.get('limit')
        call['limit'] = max(1, min(int(limit), 20)) if isinstance(limit, int) and not isinstance(limit, bool) else 10
        result = await self.read(worker, 'calendar.list', call)
        events = (result or {}).get('events') or []
        if not events:
            text = 'No calendar events in that window.'
        else:
            parts = []
            for e in events[:call['limit']]:
                when = 'all day' if e.get('all_day') else _clock(e.get('start'))
                parts.append(f"{when}: {e.get('title') or '(no title)'}" +
                             (f" at {e['location']}" if e.get('location') else ''))
            text = f"{len(events)} calendar event{'s' if len(events) != 1 else ''}: " + '; '.join(parts) + '.'
        self.note('calendar', text, 600, untrusted=True)
        return text

    async def tool_mail_list(self, args):
        worker = self._own_worker('notify.list')
        limit = args.get('limit')
        limit = max(1, min(int(limit), 10)) if isinstance(limit, int) and not isinstance(limit, bool) else 5
        result = await self.read(worker, 'notify.list', {'packages': MAIL_PACKAGES, 'limit': limit})
        items = (result or {}).get('notifications') or []
        if not items:
            text = 'No recent mail notifications.'
        else:
            app = {'com.google.android.gm': 'Gmail', 'com.microsoft.office.outlook': 'Outlook'}
            parts = [f"from {str(i.get('title') or 'unknown sender')[:60]}: {str(i.get('text') or '(no subject)')[:100]}"
                     f" ({app.get(i.get('package'), 'mail')})" for i in items[:limit]]
            text = f"{len(items)} recent mail notification{'s' if len(items) != 1 else ''}: " + '; '.join(parts) + '.'
        self.note('mail', text, 300, untrusted=True)
        return text

    async def device_state(self):
        """Prefetch: battery and location of the caller's own device, when allowed."""
        identity = current_identity.get()
        if not identity.worker:
            return None
        authorize_read('battery.status', identity.worker)
        result = await self.read(identity.worker, 'battery.status', {})
        if isinstance(result, dict) and result.get('percent') is not None:
            text = f"The caller's device {identity.worker} is at {result['percent']}% battery" + \
                   (', charging.' if result.get('charging') else '.')
            self.note('device', text, 300)
            return text
        return None

    async def _location(self):
        identity = current_identity.get()
        if identity.worker:
            if self.location and time.monotonic() - self.location[0] < 600:
                return self.location[1]
            try:
                authorize_read('location.get', identity.worker)
                result = await self.read(identity.worker, 'location.get', {'timeout': 8})
                lat = result.get('lat', result.get('latitude'))
                lon = result.get('lon', result.get('longitude'))
                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                    self.location = (time.monotonic(), (float(lat), float(lon), 'your location'))
                    return self.location[1]
            except Exception:
                pass    # no device location (denied, offline, no grant): use the home location
        lat, lon = os.environ.get('VOICE_HOME_LAT', ''), os.environ.get('VOICE_HOME_LON', '')
        try:
            return float(lat), float(lon), 'home'
        except ValueError:
            raise ValueError('No location: the device location is unavailable and VOICE_HOME_LAT/VOICE_HOME_LON '
                             'are not set') from None

    async def tool_weather(self, args):
        lat, lon, where = await self._location()
        units = 'fahrenheit' if os.environ.get('VOICE_WEATHER_UNITS', 'celsius').lower().startswith('f') else 'celsius'
        params = {'latitude': round(lat, 3), 'longitude': round(lon, 3), 'timezone': 'auto', 'forecast_days': 1,
                  'current': 'temperature_2m,apparent_temperature,weather_code,wind_speed_10m',
                  'daily': 'temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code',
                  'temperature_unit': units, 'wind_speed_unit': 'mph' if units == 'fahrenheit' else 'kmh'}
        kwargs = {'timeout': 10, 'trust_env': False}
        if self.http_transport is not None:
            kwargs['transport'] = self.http_transport
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.get(os.environ.get('VOICE_WEATHER_URL', 'https://api.open-meteo.com/v1/forecast'),
                                        params=params)
            response.raise_for_status()
            data = response.json()
        cur = data.get('current') if isinstance(data, dict) and isinstance(data.get('current'), dict) else {}
        daily = data.get('daily') if isinstance(data, dict) and isinstance(data.get('daily'), dict) else {}
        def number(value):
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) \
                else None
        def first(key):
            values = daily.get(key)
            return values[0] if isinstance(values, list) and values else None
        deg = '°F' if units == 'fahrenheit' else '°C'
        temp, feels = number(cur.get('temperature_2m')), number(cur.get('apparent_temperature'))
        high, low = number(first('temperature_2m_max')), number(first('temperature_2m_min'))
        rain = number(first('precipitation_probability_max'))
        text = f"Weather at {where}: {WEATHER_CODES.get(number(cur.get('weather_code')), 'unknown conditions')}"
        if temp is not None:
            text += f", {round(temp)}{deg}"
            if feels is not None and abs(feels - temp) >= 3:
                text += f" (feels like {round(feels)}{deg})"
        today = [f"high {round(high)}{deg}" if high is not None else '', f"low {round(low)}{deg}" if low is not None else '']
        if any(today):
            text += f"; today {WEATHER_CODES.get(number(first('weather_code')), '').strip() or 'mixed'}, " + \
                ', '.join(t for t in today if t)
        if rain is not None:
            text += f", {round(rain)}% chance of precipitation"
        text += '.'
        self.note('weather', text, 1800)
        return text

    # --- owner tools -------------------------------------------------------
    async def tool_tasks_deck(self, args):
        reply = decode(await self.mcp.call('rook_task', {'action': 'deck'}))
        titles = []
        for entry in (reply or {}).get('deck', []) if isinstance(reply, dict) else []:
            for state, items in entry.items():
                if isinstance(items, list) and state not in ('done', 'retracted', 'cancelled'):
                    titles += [str(i.get('title') or i.get('id'))[:80] for i in items if isinstance(i, dict)]
        text = (f"{len(titles)} open Rook task{'s' if len(titles) != 1 else ''}" +
                (': ' + '; '.join(titles[:5]) + ('…' if len(titles) > 5 else '') if titles else '') + '.')
        self.note('tasks', text, 600, untrusted=True)
        return text

    async def tool_task_get(self, args):
        reply = decode(await self.mcp.call('rook_task', {'action': 'get', 'id': str(args['id'])}))
        return json.dumps(reply, ensure_ascii=False)[:1500]

    async def tool_music(self, args):
        worker = os.environ.get('VOICE_PIANOBAR_WORKER', '').strip()
        if not worker:
            raise ValueError('Music is not configured (VOICE_PIANOBAR_WORKER)')
        cap = MUSIC_CAPS.get(args.get('action'))
        if cap is None:
            raise ValueError('Unknown music action')
        reply = decode(await self.mcp.call('rook_call', {'worker': worker, 'cap': cap}))
        if isinstance(reply, dict) and reply.get('ok') is False:
            raise ValueError(str(reply.get('error'))[:200])
        result = reply.get('result') if isinstance(reply, dict) else reply
        text = f"Music {args['action'].replace('_', ' ')}: " + (result if isinstance(result, str) else json.dumps(result))[:300]
        self.note('music', text, 120, untrusted=True)
        return text

    async def tool_ha_list(self, args):
        domain = args.get('domain')
        entities = await self.hass.entities()
        rows = [e for e in entities if not domain or e['entity_id'].startswith(domain + '.')]
        return '; '.join(f"{e['name']} ({e['entity_id']}): {e['state']}" for e in rows[:60]) or 'No matching entities.'

    async def tool_ha_call(self, args):
        entity, service = await self.hass.call(str(args['target']), str(args['action']))
        verb = {'turn_on': 'turned on', 'turn_off': 'turned off', 'toggle': 'toggled', 'media_play': 'resumed',
                'media_pause': 'paused', 'media_next_track': 'skipped to the next track on'}.get(service, service)
        text = (f"Activated the {entity['name']} scene." if entity['entity_id'].startswith('scene.')
                else f"{verb.capitalize()} {entity['name']}.")
        self.note('home:' + entity['entity_id'], text, 300)
        return text


def _clock(value):
    try:
        return spoken_time(datetime.fromisoformat(str(value)))
    except ValueError:
        return str(value or 'unknown time')
