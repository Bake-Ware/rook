# Start page

The **Start page** (`#start`) is your own page in the dashboard: the links you
use every day, news you follow, and what your band is doing, in columns you
arrange. It is saved for your account, so it looks the same on every device
you sign in from. It was inspired by BakeDash, a homelab dashboard of app
bookmarks and feeds.

## Widgets

| Widget | Shows |
|---|---|
| Bookmarks | groups of links (name and address), for example your media server, router and NAS |
| News feed | headlines from an RSS or Atom feed, optionally with a short summary |
| Note | a few lines of your own text |
| Workers | how many workers are online, and which are not answering |
| Open tasks | tasks in progress or blocked |
| Chat rooms | the busiest rooms, with your unread counts |
| Recently changed pages | knowledge pages that changed lately |
| Agent sessions | sessions running now, and those waiting for you |

A new account starts with workers, tasks, rooms, recent pages, a few links
around the dashboard and a note.

## Editing

Choose **Edit page**:

- drag a widget by its handle (⠿) to another place or column, or use its
  arrow buttons (they also work with a keyboard and on phones);
- the pencil changes a widget: its title and its settings. Bookmarks take one
  link per line, `Name | https://address`; a group can have a name;
- **+ Add widget** at the foot of a column adds one there;
- **Layout** sets 1 to 4 columns (widgets of a removed column move to the
  last one kept);
- **Save** keeps the page; **Cancel** drops the changes; **Reset to default**
  puts the starting widgets back.

Links must start with `http://`, `https://` or `#` (a dashboard page).

## Feeds

Your hub fetches feeds, not your browser, and keeps each one for a while
(**Settings > Dashboard > Keep a fetched feed for**, 15 minutes by default),
so every start page showing the same feed costs the site one request per
period. The operator sets limits under **Settings > Dashboard** (namespace
`ui`):

| Setting | Default | |
|---|---|---|
| `ui.feeds_allow_private` | off | Off: feeds come only from public internet addresses. On: also from private networks and the hub itself, for a feed served in your homelab. Anyone who can sign in could then make the hub fetch those addresses. |
| `ui.feed_max_kb` | 512 | A larger feed is refused. |
| `ui.feed_timeout` | 8 s | The whole fetch, redirects included. |
| `ui.feed_cache_minutes` | 15 | How long a fetched feed is reused. |

The address guard: only `http` and `https`, no user name or password in the
address, at most three redirects, and the hub connects only to addresses that
pass the check (it resolves the name itself and connects to the address it
checked, so a DNS answer cannot change in between; each redirect is checked
again). With private addresses off, loopback, private ranges (10/8,
172.16/12, 192.168/16, fc00::/7), link-local (including 169.254.169.254),
shared (100.64/10), documentation and reserved ranges are refused. A feed that
declares XML entities is refused.

## Settings and storage

The layout is the user-scope setting `ui.startpage` (also listed under
**Settings > My preferences**, where it can be reset): `{"version": 1, "columns":
[[widget, ...], ...]}`, each widget `{"id", "type", "title", "options"}`. The
hub checks it on save: at most 4 columns and 40 widgets, 8 KB per widget,
64 KB in all, and the options of the built-in kinds.

API: `GET /account/ui/api` returns `startpage`; `POST {action: "startpage",
layout, csrf}` saves, `{action: "startpage_reset"}` resets, `{action: "feed",
url, limit}` fetches a feed (`{ok, feed: {title, link, items: [{title, link,
published, summary}]}}` or `{ok: false, error}`).

## Widgets from plugins

A hub plugin can add a widget kind. Declare it on the plugin class:

```python
class Weather(Plugin):
    NAMESPACE = "weather"
    WIDGETS = ({"type": "weather.now", "title": "Weather",
                "description": "Today's forecast for one place.",
                "module": "/account/weather/assets/widget.js"},)
```

`type` must contain a dot (built-in kinds have none) and `module` must be a
same-origin path the plugin serves. The start page imports the module, which
registers the kind:

```js
const {register} = await import('/account/ui/assets/widgets.js');
register('weather.now', {
  title: 'Weather', description: "Today's forecast for one place.",
  defaults: {place: ''},
  async render(body, widget, ctx) {        // fill `body`; set text, never HTML
    const r = await fetch('/account/weather/api?place=' + encodeURIComponent(widget.options.place));
    body.textContent = (await r.json()).summary;
  },
  editor(form, widget) {                   // optional: fields for the widget form
    const input = document.createElement('input'); input.value = widget.options.place || '';
    form.append(input);
    return () => ({place: input.value});   // read the options back; throw Error(msg) to refuse
  },
});
```

`ctx.band()` gives the shared band data (workers, rooms, sessions, knowledge)
the built-in widgets use. Without an `editor`, the widget form edits the
options as JSON. The hub stores a plugin widget's options as given, within the
size limits.
