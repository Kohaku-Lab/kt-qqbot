# kt-qqbot

A [KohakuTerrarium](https://github.com/Kohaku-Lab/KohakuTerrarium) package that
puts a creature into QQ private chats and QQ group chats.

The library surface is **a trigger and two tools**:

- `qq_feed` (trigger) wakes the creature with new chat as incremental updates.
- `qq_send` (tool) is the only way the creature speaks in QQ.
- `qq_history` (tool) reads older messages from the on-disk log.

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

`media` is a local path (resolved against the creature's working folder, so
files the creature made can be sent) or an https URL, or a real list of them
sent in order. A string is always one item; a JSON-looking string is not split,
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
the creature's working folder, symlinks resolved).

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
`max_chars` (20000).

## Access and safety

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

## Storage

`~/.kohakuterrarium/qq-bot/data/<appid>.kvault` (KohakuVault) holds each chat's
message log (the newest 2000 messages per chat), sender aliases, and one
cursor per feed per chat.

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
