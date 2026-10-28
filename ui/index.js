// kt-qqbot dashboard UI. Plain ESM: the dashboard supplies `vue` and the SDK.
import { computed, reactive, ref } from "vue"
import { confirm, defineExtension, notify, useExtension, useOwner } from "@kohakuterrarium/ui-sdk"

import qrcode from "./vendor/qrcode.mjs"

/** SVG path of a QR code for ``text`` (one unit per module, 2-unit margin). */
export function qrPath(text) {
  const qr = qrcode(0, "M")
  qr.addData(text)
  qr.make()
  const size = qr.getModuleCount()
  let d = ""
  for (let row = 0; row < size; row++) {
    for (let col = 0; col < size; col++) {
      if (qr.isDark(row, col)) d += `M${col + 2} ${row + 2}h1v1h-1z`
    }
  }
  return { d, size: size + 4 }
}

export const QrCode = {
  name: "QqQrCode",
  props: { text: { type: String, required: true } },
  setup(props) {
    const shape = computed(() => qrPath(props.text))
    return { shape }
  },
  template: `
    <svg class="qq-qr" :viewBox="'0 0 ' + shape.size + ' ' + shape.size" role="img" aria-label="QR code" shape-rendering="crispEdges">
      <rect :width="shape.size" :height="shape.size" fill="#fff" />
      <path :d="shape.d" fill="#000" />
    </svg>`,
}

function useActions(owner) {
  const busy = ref("")
  async function run(action, args = {}) {
    busy.value = action
    try {
      const result = await owner.call(action, args)
      if (result && result.ok === false) notify(result.message || action, "error")
      else if (result?.message) notify(result.message, "success")
      return result
    } catch (error) {
      notify(error.message || String(error), "error")
      return null
    } finally {
      busy.value = ""
    }
  }
  return { busy, run }
}

export const SettingsSection = {
  name: "QqBotSettings",
  components: { QrCode },
  setup() {
    const kt = useExtension()
    const owner = useOwner()
    const { busy, run } = useActions(owner)
    const form = reactive({ app_id: "", app_secret: "" })
    const bots = computed(() => owner.state.value?.bots || [])
    const bind = computed(() => owner.state.value?.bind || { phase: "idle" })
    const binding = computed(() => ["starting", "waiting"].includes(bind.value.phase))
    owner.on("bind", (step) => {
      if (step?.kind === "done") notify(step.message || "Bound", "success")
      if (step?.kind === "error") notify(step.message || "Binding failed", "error")
    })
    async function save() {
      const result = await run("save_manual", { app_id: form.app_id, app_secret: form.app_secret })
      form.app_secret = ""
      if (result?.ok) form.app_id = ""
    }
    async function unbind(appId) {
      if (await confirm(kt.t("unbindConfirm", { app_id: appId })))
        await run("unbind", { app_id: appId })
    }
    return { kt, owner, busy, run, form, bots, bind, binding, save, unbind }
  },
  template: `
    <div class="qq-card">
      <strong>{{ kt.t("bots") }}</strong>
      <p v-if="!bots.length" class="qq-muted">{{ kt.t("noBots") }}</p>
      <div v-for="bot in bots" :key="bot.app_id" class="qq-row" data-test="bot">
        <code>{{ bot.app_id }}</code>
        <strong v-if="bot.name" data-test="bot-name">{{ bot.name }}</strong>
        <span v-if="bot.default" class="qq-badge">{{ kt.t("default") }}</span>
        <span v-if="bot.source === 'env'" class="qq-badge">{{ kt.t("fromEnv") }}</span>
        <span class="qq-badge" :class="bot.gateway === 'ready' ? 'ok' : (bot.gateway === 'fatal' || bot.gateway === 'unbound') ? 'bad' : ''">{{ kt.t("gateway", { state: bot.gateway }) }}</span>
        <button class="qq-button" data-test="test" :disabled="!!busy" @click="run('test', { app_id: bot.app_id })">{{ kt.t("test") }}</button>
        <button v-if="bot.source !== 'env'" class="qq-button" data-test="unbind" :disabled="!!busy" @click="unbind(bot.app_id)">{{ kt.t("unbind") }}</button>
      </div>

      <strong>{{ kt.t("bindTitle") }}</strong>
      <p class="qq-muted">{{ kt.t("bindHint") }}</p>
      <div class="qq-row">
        <button class="qq-button" data-test="bind" :disabled="!!busy" @click="run('bind_start')">{{ binding ? kt.t("bindRestart") : kt.t("bindStart") }}</button>
        <button v-if="binding" class="qq-button" data-test="cancel" :disabled="!!busy" @click="run('bind_cancel')">{{ kt.t("bindCancel") }}</button>
      </div>
      <p v-if="bind.phase === 'starting'" class="qq-muted">{{ kt.t("bindStarting") }}</p>
      <div v-if="bind.phase === 'waiting' && bind.url" data-test="qr">
        <QrCode :text="bind.url" />
        <p class="qq-muted">{{ kt.t("openLink") }}: <a :href="bind.url" target="_blank" rel="noopener noreferrer">{{ bind.url }}</a></p>
      </div>
      <p v-if="bind.message" class="qq-muted" data-test="bind-message">{{ bind.message }}</p>

      <strong>{{ kt.t("manualTitle") }}</strong>
      <div class="qq-row">
        <input v-model="form.app_id" class="qq-input" data-test="app-id" :placeholder="kt.t('appId')" autocomplete="off" />
        <input v-model="form.app_secret" class="qq-input" data-test="app-secret" type="password" :placeholder="kt.t('appSecret')" autocomplete="new-password" />
        <button class="qq-button" data-test="save" :disabled="!!busy || !form.app_id || !form.app_secret" @click="save">{{ kt.t("save") }}</button>
      </div>
    </div>`,
}

function clock(ts) {
  if (!ts) return "--:--"
  const date = new Date(ts * 1000)
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`
}

export const ChatsPanel = {
  name: "QqChatsPanel",
  setup() {
    const kt = useExtension()
    const owner = useOwner()
    const feeds = computed(() => owner.state.value?.feeds || [])
    return { kt, owner, feeds, clock }
  },
  template: `
    <div class="qq-card">
      <p v-if="!feeds.length || feeds.every(f => !f.chats.length)" class="qq-muted">{{ kt.t("chatsEmpty") }}</p>
      <div v-for="feed in feeds" :key="feed.subscriber" data-test="feed">
        <div class="qq-row">
          <code>{{ feed.bot }}</code>
          <span class="qq-badge" :class="feed.gateway === 'ready' ? 'ok' : (feed.gateway === 'fatal' || feed.gateway === 'unbound') ? 'bad' : ''">{{ kt.t("gateway", { state: feed.gateway }) }}</span>
          <span class="qq-muted">{{ feed.wake }} · {{ feed.total_chats }}</span>
        </div>
        <div v-for="chat in feed.chats" :key="chat.handle" class="qq-chat" data-test="chat">
          <div class="qq-row">
            <strong>{{ chat.handle }}</strong>
            <span class="qq-muted">{{ chat.type }}</span>
            <span v-if="chat.seen" class="qq-badge">{{ kt.t("seen") }}</span>
            <span v-if="chat.unread" class="qq-badge">{{ kt.t("unread", { n: chat.unread }) }}</span>
            <span class="qq-badge" :class="chat.free_slots ? 'ok' : 'bad'" data-test="slots">{{ kt.t("slots", { n: chat.free_slots }) }}</span>
          </div>
          <div v-for="line in chat.last" :key="line.tag" class="qq-line">[{{ clock(line.ts) }}] {{ line.sender }}: {{ line.text }}</div>
        </div>
      </div>
    </div>`,
}

/** The ``media`` argument of a qq_send call as a list of strings. */
export function mediaList(value) {
  if (value == null || value === "") return []
  if (Array.isArray(value)) return value.map(String)
  const text = String(value).trim()
  if (text.startsWith("[")) {
    try {
      const parsed = JSON.parse(text)
      if (Array.isArray(parsed)) return parsed.map(String)
    } catch {
      return [text]
    }
  }
  return [text]
}

/** A short label for a qq_send media spec: a handle or a file name. */
export function mediaLabel(spec) {
  if (spec.startsWith("sticker:")) return `sticker ${spec.slice(8)}`
  if (spec.startsWith("img:")) return `search image ${spec.slice(4)}`
  const chat = /^(?:([\w:.-]+)\/)?m(\d+)\.(\d+)$/.exec(spec)
  if (chat) return `chat media ${chat[1] ? chat[1] + "/" : ""}m${chat[2]}.${chat[3]}`
  return spec.split(/[\\/]/).pop().split("?")[0]
}

const IMAGE_RE = /\.(png|jpe?g|gif|webp|bmp)(\?|$)/i

export const SendRenderer = {
  name: "QqSendRenderer",
  props: { call: { type: Object, required: true } },
  setup(props) {
    const kt = useExtension()
    const args = computed(() => props.call.args || {})
    const media = computed(() =>
      mediaList(args.value.media).map((spec) => ({
        spec,
        name: mediaLabel(spec),
        thumb: spec.startsWith("https://") && IMAGE_RE.test(spec),
      })),
    )
    const result = computed(() => String(props.call.result || ""))
    const outcome = computed(() => {
      if (props.call.status === "running") return "running"
      if (result.value.startsWith("PARTLY SENT")) return "partly"
      if (result.value.startsWith("NOT SENT") || props.call.status === "error") return "failed"
      return "sent"
    })
    return { kt, args, media, result, outcome }
  },
  template: `
    <div class="qq-card">
      <div class="qq-bubble" :class="{ failed: outcome === 'failed' || outcome === 'partly' }" data-test="bubble">
        <div v-if="args.to" class="qq-muted">→ {{ args.to }}<template v-if="args.reply_to"> ↩{{ args.reply_to }}</template></div>
        <div v-if="args.message" class="qq-line">{{ args.message }}</div>
        <div v-if="media.length" class="qq-row">
          <template v-for="item in media" :key="item.spec">
            <img v-if="item.thumb" class="qq-thumb" :src="item.spec" :alt="item.name" data-test="thumb" />
            <span v-else class="qq-badge" data-test="media">{{ item.name }}</span>
          </template>
        </div>
      </div>
      <div class="qq-muted" data-test="outcome">
        <template v-if="outcome === 'running'">{{ kt.t("sending") }}</template>
        <template v-else-if="outcome === 'failed'">{{ kt.t("notSent") }}: {{ result.replace(/^NOT SENT:\\s*/, "") }}</template>
        <template v-else-if="outcome === 'partly'">{{ kt.t("partlySent") }}: {{ result }}</template>
        <template v-else>{{ result }}</template>
      </div>
    </div>`,
}

export default defineExtension((kt) => {
  kt.registerSettingsSection("qqbot", SettingsSection)
  kt.registerPanel("chats", ChatsPanel)
  kt.registerToolRenderer("qq-send", SendRenderer)
})
