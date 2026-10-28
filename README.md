# kt-qqbot

A [KohakuTerrarium](https://github.com/Kohaku-Lab/KohakuTerrarium) package that
puts a creature into QQ private chats and QQ group chats.

The library surface is **a trigger and a few tools**:

- `qq_feed` (trigger) wakes the creature with new chat as incremental updates.
- `qq_send` (tool) is the only way the creature speaks in QQ.
- `qq_history` (tool) reads older messages from the on-disk log.
- `qq_sticker` (tool) finds stickers in the user's sticker folder.
- `qq_image_search` (tool) finds images on the web to look at before sending.
- `qq_watch` (tool) lets a creature install a feed itself.

Plugins: `qq_character` (character files), `qq_router` (one creature per chat),
`qq_status` (dashboard panel), `qq_guard` (hides graph / Drive tools),
`qq_web_guard` (refuses `web_fetch` to private addresses).

Text the creature writes freely never reaches QQ; it shows only in the console
or the dashboard. So silence is the default, and the creature decides when to
speak.

## Install

Requires KohakuTerrarium newer than 2.1.8: the first core with the UI
extension system and the `path_arguments`, trigger `bind_host` and
`host_resources_allowed` hooks this package relies on.

```bash
kt install https://github.com/Kohaku-Lab/kt-qqbot
```

## Log in

```bash
kt-qqbot login            # prints a QR code; scan it with mobile QQ
kt-qqbot login --manual   # enter an existing bot's AppID and AppSecret
kt-qqbot status           # list stored bots and check the default one
kt-qqbot logout [APPID]
```

The QR login asks QQ to create or bind a bot on the scanning account and
returns its AppID / AppSecret. The scan page shows the caller as a third-party
bot. Credentials are saved to `~/.kohakuterrarium/qq-bot/credentials.json`
(mode 0600). `QQBOT_APPID` and `QQBOT_SECRET` override the file.

## Run

One shared creature for every chat:

```bash
kt run @kt-qqbot/creatures/qq-group-chat     # or start it from kt serve
```

One creature per chat (each with its own context and session):

```bash
kt run @kt-qqbot/creatures/qq-router         # or start it from kt serve
```

## Character

The example creature has no character. To give it one, put files in
`~/.kohakuterrarium/qq-bot/` (`KT_CONFIG_DIR` is honored):

- `character.md`
- optional `character/*.md`, added in name order

The `qq_character` plugin adds them to the system prompt as `## Character`.
Changes apply the next time the prompt is built.

A persona image `character.png` (or `.jpg`, `.jpeg`, `.webp`; the first in that
order wins, the others are logged as ignored) shows what the character looks
like. `qq_character` downscales it once to at most 768 px (cached by file hash
under `qq-bot/cache/`) and pins it right after the system prompt as a
reference image when the core supports pinned messages; it re-pins on change
and unpins when the file is removed (option `persona: false` turns this off).
`qq_send(media="persona")` sends that file; `qq_send`'s `character_directory`
option must match `qq_character`'s `directory` if you moved the folder.

## Configuration

### `qq_feed` trigger

| Key | Default | Meaning |
|---|---|---|
| `bot` | default bot | AppID to use |
| `conversations` | `all` | `all`, `c2c`, `groups`, or a list of chat handles / `group:<openid>` keys |
| `wake` | `ambient` | `mention`: only private chat, @-mentions, replies to the bot. `all`: every message. `ambient`: direct messages at once, otherwise after `quiet_period` or `every_n` messages, never sooner than `cooldown` after the last ambient wake |
| `quiet_period` | `20` | seconds of silence before an ambient wake |
| `every_n` | `8` | pending messages that force an ambient wake |
| `cooldown` | `60` | seconds between ambient wakes in one chat |
| `batch_delay` | `1.0` | seconds to wait so a burst lands in one delivery |
| `backfill` | `20` | on a chat's first delivery, how many messages from before the feed started to show as `<qq_backfill>` |
| `max_delivery` | `100` | most lines per chat in one delivery; older ones become "N earlier messages not shown; use qq_history" |
| `unseen_history` | `backfill` | `backfill`: undelivered messages from before the feed started show as history; `new`: as new chat |
| `hold` | `true` | `qq_send` returns chat that arrived meanwhile instead of sending, once |
| `typing` | `false` | send the private-chat typing indicator on wake (uses a reply slot) |
| `images` / `image_detail` / `max_images` | `true` / `auto` / `8` | inbound images as vision input |
| `subscriber` | the host creature's id (from core `bind_host`) | cursor key; set it only to share or pin a cursor |

The creature can also install a feed itself: add the `qq_watch` tool
(`type: package`, `module: kt_qqbot.tools`, `class: QQWatchTool`). A feed
installed this way keys its cursor by the creature.

### `qq_send` / `qq_history`

`qq_send(message?, to?, reply_to?, media?, media_type?)`,
`qq_history(chat?, before?, limit?)`. Both only reach chats the creature's feed
delivered.

`media` is one item or a real list of items, sent in order. An item is:

| Item | Meaning |
|---|---|
| `m12.1` | attachment 1 of message m12 in the target chat, as tagged in the feed (`[image m12.1]`, `[file:x.pdf m12.3]`) |
| `u_ab12cd/m12.1` | the same, from another chat this creature's feed delivered |
| `sticker:<name>` | a sticker from `qq_sticker` |
| `img:<n>` | candidate n of this creature's latest `qq_image_search` (15 minutes) |
| `persona` | the persona image `character.png` / `.jpg` / `.jpeg` / `.webp` |
| a path | a file inside the creature's working folder (files the creature made can be sent) |
| an https URL | QQ downloads it |

Chat attachments are resent from QQ's URL; if that fails, or the message is
older than a day, the copy kept when the feed downloaded it is used (images
only; at most 500 kept per bot). Otherwise the tool says the handle is too old. A string is always one item; a JSON-looking string is not split,
so a sandbox plugin checks exactly the paths that are read (`qq_send` declares
`media` as a read path). Text goes first, then each media item as its own message; each
message uses one passive-reply slot. `media_type` (`image`, `video`, `voice`,
`file`) overrides the type inferred from the extension or MIME type.

- Voice must be SILK. Other audio is converted when the optional codec is
  installed (`pip install "kt-qqbot[voice]"`, which adds `graiax-silkcoder`);
  without it the tool answers `NOT SENT: voice needs silk; install kt-qqbot[voice]`.
  Voice URLs must point to a `.silk` file.
- Local files over 5 MB are refused. QQ's chunked upload is not implemented;
  `QQHub.chunked_uploader` (a `ChunkedUploader`) is where it plugs in.
- Group chats refuse `file` messages unless the tool option
  `allow_group_files: true` is set.

`qq_send` options: `max_chars` (default 2000), `allow_group_files` (default
false), `media_roots` (extra folders local media may come from; by default only
the creature's working folder, symlinks resolved), `mention_format` (`native`
by default: in groups, `@<name>` of a known member is sent as
`<qqbot-at-user id="…" />`; set `plain` to send it as text if your client shows
the raw tag; a name two members share always stays text).

Mentions of other members in incoming messages show in the feed as `@<name>`.

### `qq_router` plugin

| Option | Default | Meaning |
|---|---|---|
| `template` | `@kt-qqbot/creatures/qq-group-chat` | creature to build per chat; it must have a `qq_feed` trigger |
| `bot` | default bot | AppID |
| `conversations` | `all` | `all`, `c2c`, or `groups` |

Creature ids are `qq-<appid>-<c2c|group>-<openid>` (safe as file names). Its
creatures read pending chat as new (`unseen_history: new`).

### `qq_character` plugin

`directory` (default `~/.kohakuterrarium/qq-bot`), `heading` (`Character`),
`max_chars` (20000), `persona` (true). It logs "Character loaded" (file names,
size) and "Persona image loaded" (file, pixels, bytes) on first load and on
change; never the content.

### Stickers

Put images (`png`, `jpg`, `gif`, `webp`) in `~/.kohakuterrarium/qq-bot/stickers/`
(subfolders are fine). Names come from the file names. An optional
`stickers/tags.yaml` adds words:

```yaml
thumbs_up.jpg: [ok, agree, good]
cats/sleepy-cat.gif: {tags: [tired], mood: sleepy}
happy.png: excited
```

`qq_sticker(query)` returns up to 4 `sticker:<name>` handles with small
previews. Options: `directory`, `max_results` (at most 8). `qq_send` takes the
folder from its `sticker_directory` option (same default); a sticker handle can
never leave that folder.

### Image search

`qq_image_search(query, count?)` returns 3-6 candidates as `img:<n>` with
low-detail previews, so the creature looks before it sends. Backends:

- **Brave Search API** when `BRAVE_API_KEY` is set (or the `api_key` tool
  option), images endpoint with `safesearch=strict`;
- otherwise **DuckDuckGo** images, unofficial and best effort (it needs a page
  token first and can break; the tool then says so).

Guards: https only; previews are fetched only from globally routable addresses,
must be `image/*` and at most 1 MB; `blocklist` (domains, subdomains included);
`rate_limit_per_minute` (default 6, per creature and chat). The full image is
sent by URL, so QQ downloads it.

### Web tools in the example creature

`qq-group-chat` includes core `web_search` and `web_fetch`. Core `web_fetch`
has no private-address check, so the `qq_web_guard` plugin refuses fetches to
loopback, private, link-local and other non-global addresses. Redirects the
fetch backends follow are not re-checked.

## Access and safety

- A creature started from the dashboard runs privileged and gets graph
  (`group_*`) and Drive (`drive_*`) tools. `qq_guard` hides them from the model
  and refuses them if called (`allow_tools` re-enables names). The example
  creature has it on.
- Under multi-user mode the QQ bot is for admin creatures only: `qq_feed`,
  `qq_send`, `qq_history`, `qq_watch` and the router refuse other creatures
  ("QQ bot is available to admin creatures only"). This uses the core
  `host_resources_allowed` hook.
- One process per bot: a hub takes `~/.kohakuterrarium/qq-bot/data/<appid>.lock`;
  a second process gets a `BotInUse` error.
- Chat text, names and file names are escaped before the creature reads them,
  so a QQ message cannot open or close a feed block or fake a line.
- Inbound images are fetched only from globally routable addresses. The host is
  resolved once for the check and again for the download, so a DNS-rebinding
  host could still differ; attachment URLs come from QQ, which limits this.
- `kt-qqbot logout` while a server runs: the server notices the changed
  credential file within a few seconds (on its next feed poll or send) and
  stops that bot.

## Replies

QQ bots can only send passive replies: each message rides on a recently
received message (any message, @-mention or not). Per QQ's rules:

| Chat | Window | Replies per received message |
|---|---|---|
| group | 5 min | 5 |
| private | 60 min | 4 |

QQ shut down active (unprompted) messages on 2025-04-21, so kt-qqbot never
tries one. When no reply is left, `qq_send` answers "the reply window has
closed" without calling QQ. Errors 40034128 / 40034005 / 304103 close that
slot; 40054005 (duplicate `msg_seq`) is retried once with the next seq. The
private-chat typing indicator (`typing: true`) uses a reply slot and is sent
only when the slot keeps at least one reply for the answer.

### Full-message mode

When a group owner turns on 获取群内全部消息, every group message (including
@bot ones) arrives as `GROUP_MESSAGE_CREATE`. kt-qqbot treats a message as an
@-mention when a `mentions` entry has `is_you`, when a `mentions` entry's id
equals the bot's id from READY, or when the content has a `<@id>` / `<@!id>`
tag for that id (`records.mentions_bot`). The real payload shape is not
confirmed yet. To check it, set `debug_raw_events: 2` on the `qq_feed`
trigger, send one @-message and one plain message, and read
`~/.kohakuterrarium/qq-bot/debug/raw-<time>.jsonl` (mode 0600; payloads only,
no tokens). It is off by default.

## What survives a restart

Kept in `~/.kohakuterrarium/qq-bot/data/<appid>.kvault` (KohakuVault):

- each chat's message log (newest 2000 messages), type, handle, first-seen and
  last-seen times;
- sender aliases and the QQ names seen (at most 1000 stored per chat);
- one cursor per feed per chat, so a resumed creature gets only what it has not
  seen;
- passive-reply slots (`msg_id`, replies used, last `msg_seq`), pruned when
  their window ends;
- the gateway `session_id` and last `seq` (seq written at most once a second and
  on shutdown); on start the hub sends RESUME, and QQ replays what was missed
  during a short downtime; replayed events are deduplicated by message id;
- the bot's own identity from READY (id, name …), so mention detection works
  before the first READY and the settings page shows the bot's name.

Creatures: a creature with a session resumes with its conversation through the
normal `kt resume` path. Router-made chat creatures adopt their own session file
(`<session_dir>/qq-<appid>-<kind>-<openid>.kohakutr`) when it exists.

Not kept:

- events older than QQ's RESUME window, or any event after an INVALID_SESSION
  (QQ has no history API for bots, so they are lost);
- the private-chat typing state and pending delivery timers (recomputed);
- downloaded inbound images (they are fetched again only if still delivered).

## Dashboard UI

The package adds three things to the `kt serve` dashboard (UI extension SDK v1,
plain ESM in `ui/`, no build step):

- **Settings → QQ Bot** (package owner `kt_qqbot.ui_owner:QQBotUI`): bound bots
  and their gateway state, **Bind with QR** (the same flow as `kt-qqbot login`;
  the dashboard draws the QR code and shows a new one when it expires),
  cancel, manual AppID / AppSecret entry, **Test** and **Unbind**. Running bots
  pick up new credentials at once. The secret is never sent back to the
  browser. Under multi-user mode only admins can bind, unbind or enter
  credentials.
- **QQ chats** side panel (owner `plugin:qq_status`): for the focused creature,
  its chats, unread count, last messages and free passive-reply slots, plus the
  connection state, updated live. It shows when the creature has the
  `qq_status` plugin (the example creature does).
- **qq_send renderer**: tool calls show as a chat bubble with the text, media
  and whether it was sent.

The QR encoder is a vendored copy of qrcode-generator (MIT), see
`ui/vendor/README.md`.

### `qq_status` plugin

`max_chats` (default 30), `last_messages` (default 5).

## Development

```bash
uv venv && uv pip install -e /path/to/KohakuTerrarium -e ".[dev]"
pytest                                 # unit + integration
npm install && npm test                # dashboard UI (vitest)
KT_QQBOT_LIVE=1 pytest tests/live      # real platform, after kt-qqbot login
```
