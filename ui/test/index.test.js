// Components against the SDK mock host (aliased in vitest.config.js).
import { mount } from "@vue/test-utils"
import { expect, test } from "vitest"
import { createMockHost } from "@kohakuterrarium/ui-sdk"

import en from "../i18n/en.json"
import register, {
  ChatsPanel,
  SendRenderer,
  SettingsSection,
  mediaLabel,
  mediaList,
  qrPath,
} from "../index.js"

const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

function bindState(bind = { phase: "idle" }, bots = []) {
  return { bots, default: bots[0]?.app_id ?? null, bind }
}

test("registers the settings section, panel and renderer", async () => {
  const host = createMockHost({ package: "kt-qqbot" })
  await host.register(register)
  expect([...host.registered.settings.keys()]).toEqual(["qqbot"])
  expect([...host.registered.panel.keys()]).toEqual(["chats"])
  expect([...host.registered["tool-renderer"].keys()]).toEqual(["qq-send"])
})

test("qrPath encodes text into dark modules inside a margin", () => {
  const shape = qrPath("https://q.qq.com/qqbot/openclaw/connect.html?task_id=T1")
  expect(shape.size).toBeGreaterThanOrEqual(25)
  expect(shape.d).toMatch(/^M2 2h1v1h-1z/)
  expect(qrPath("other").d).not.toEqual(shape.d)
})

test("settings: bots, QR pushes, cancel, then done", async () => {
  const actions = {
    bind_start: () => ({ ok: true }),
    bind_cancel: () => ({ ok: true }),
    test: () => ({ ok: true, message: "Token check OK." }),
  }
  const host = createMockHost({
    messages: { en },
    owner: {
      state: bindState({ phase: "idle" }, [
        { app_id: "101", name: "kohaku-bot", default: true, source: "file", gateway: "ready" },
      ]),
      actions,
    },
  })
  const wrapper = mount(SettingsSection)
  expect(wrapper.findAll("[data-test=bot]")).toHaveLength(1)
  expect(wrapper.text()).toContain("Gateway: ready")
  expect(wrapper.find("[data-test=bot-name]").text()).toBe("kohaku-bot")
  expect(wrapper.find("[data-test=qr]").exists()).toBe(false)
  await wrapper.find("[data-test=test]").trigger("click")
  await flush()
  expect(host.ownerCalls.at(-1)).toEqual({ action: "test", args: { app_id: "101" } })
  expect(host.notifications.at(-1)).toEqual({ message: "Token check OK.", type: "success" })

  await wrapper.find("[data-test=bind]").trigger("click")
  await flush()
  expect(host.ownerCalls.at(-1)).toEqual({ action: "bind_start", args: {} })

  host.emit(
    "state",
    bindState({ phase: "waiting", url: "https://q.qq.com/x?task_id=T1", message: "" }),
  )
  await flush()
  expect(wrapper.find("[data-test=qr] svg path").attributes("d")).toContain("h1v1h-1z")
  expect(wrapper.find("a").attributes("href")).toBe("https://q.qq.com/x?task_id=T1")

  host.emit(
    "state",
    bindState({
      phase: "waiting",
      url: "https://q.qq.com/x?task_id=T2",
      message: "QR code expired; showing a new one.",
    }),
  )
  await flush()
  expect(wrapper.find("a").attributes("href")).toContain("T2")
  expect(wrapper.find("[data-test=bind-message]").text()).toContain("expired")

  await wrapper.find("[data-test=cancel]").trigger("click")
  await flush()
  expect(host.ownerCalls.at(-1).action).toBe("bind_cancel")

  host.emit("bind", { kind: "done", message: "Bound bot 777.", app_id: "777" })
  host.emit("state", bindState({ phase: "done", url: "", message: "Bound bot 777." }))
  await flush()
  expect(wrapper.find("[data-test=qr]").exists()).toBe(false)
  expect(host.notifications).toContainEqual({ message: "Bound bot 777.", type: "success" })
})

test("settings: manual entry clears the secret and reports owner errors", async () => {
  const host = createMockHost({
    messages: { en },
    owner: {
      state: bindState(),
      actions: { save_manual: () => ({ ok: false, message: "QQ rejected the credentials" }) },
    },
  })
  const wrapper = mount(SettingsSection)
  await wrapper.find("[data-test=app-id]").setValue("555")
  await wrapper.find("[data-test=app-secret]").setValue("pw")
  expect(wrapper.find("[data-test=app-secret]").attributes("type")).toBe("password")
  await wrapper.find("[data-test=save]").trigger("click")
  await flush()
  expect(host.ownerCalls.at(-1)).toEqual({
    action: "save_manual",
    args: { app_id: "555", app_secret: "pw" },
  })
  expect(wrapper.find("[data-test=app-secret]").element.value).toBe("")
  expect(wrapper.find("[data-test=app-id]").element.value).toBe("555")
  expect(host.notifications.at(-1)).toEqual({
    message: "QQ rejected the credentials",
    type: "error",
  })
})

test("settings: unbind asks first; admin errors surface", async () => {
  const host = createMockHost({
    messages: { en },
    confirmAnswer: false,
    owner: {
      state: bindState({ phase: "idle" }, [{ app_id: "101", source: "file", gateway: "unbound" }]),
      actions: {},
    },
  })
  const wrapper = mount(SettingsSection)
  await wrapper.find("[data-test=unbind]").trigger("click")
  await flush()
  expect(host.ownerCalls).toEqual([])
  host.state.confirmAnswer = true
  await wrapper.find("[data-test=unbind]").trigger("click")
  await flush()
  expect(host.ownerCalls).toEqual([{ action: "unbind", args: { app_id: "101" } }])
  expect(host.notifications.at(-1).type).toBe("error")
})

test("panel: chats, slots and live pushes", async () => {
  const host = createMockHost({ messages: { en }, owner: { state: { feeds: [] }, actions: {} } })
  const wrapper = mount(ChatsPanel)
  expect(wrapper.text()).toContain(en.chatsEmpty)
  host.emit("state", {
    feeds: [
      {
        bot: "101",
        gateway: "ready",
        subscriber: "s",
        wake: "ambient",
        total_chats: 1,
        chats: [
          {
            handle: "g_ab12",
            type: "group",
            seen: true,
            unread: 2,
            free_slots: 3,
            last: [{ tag: "m1", sender: "#a3f9", text: "hi", ts: 0, self: false }],
          },
        ],
      },
    ],
  })
  await flush()
  expect(wrapper.findAll("[data-test=chat]")).toHaveLength(1)
  expect(wrapper.find("[data-test=slots]").text()).toBe("3 reply slots")
  expect(wrapper.text()).toContain("2 unread")
  expect(wrapper.text()).toContain("#a3f9: hi")
})

test("renderer: sent, not sent, partly sent, media", () => {
  createMockHost({ messages: { en } })
  const sent = mount(SendRenderer, {
    props: {
      call: {
        name: "qq_send",
        status: "done",
        args: { message: "hi", media: '["https://x.test/a.png", "notes.pdf"]' },
        result: "Sent as m3, m4, m5",
      },
    },
  })
  expect(sent.find("[data-test=thumb]").attributes("src")).toBe("https://x.test/a.png")
  expect(sent.find("[data-test=media]").text()).toBe("notes.pdf")
  expect(sent.find("[data-test=outcome]").text()).toBe("Sent as m3, m4, m5")
  const failed = mount(SendRenderer, {
    props: {
      call: {
        name: "qq_send",
        status: "error",
        args: { message: "x" },
        result: "NOT SENT: banned word",
      },
    },
  })
  expect(failed.find("[data-test=bubble]").classes()).toContain("failed")
  expect(failed.find("[data-test=outcome]").text()).toBe("not sent: banned word")
  const partly = mount(SendRenderer, {
    props: {
      call: {
        name: "qq_send",
        status: "done",
        args: { media: ["a.png", "b.png"] },
        result: "PARTLY SENT as m2; NOT SENT the rest: x",
      },
    },
  })
  expect(partly.find("[data-test=outcome]").text()).toContain("partly sent")
  const running = mount(SendRenderer, {
    props: { call: { name: "qq_send", status: "running", args: { message: "x" } } },
  })
  expect(running.find("[data-test=outcome]").text()).toBe("sending…")
  expect(mediaList("a.png")).toEqual(["a.png"])
  expect(mediaLabel("sticker:cats/sleepy.gif")).toBe("sticker cats/sleepy.gif")
  expect(mediaLabel("img:2")).toBe("search image 2")
  expect(mediaLabel("m12.1")).toBe("chat media m12.1")
  expect(mediaLabel("g_ab12/m3.2")).toBe("chat media g_ab12/m3.2")
  expect(mediaLabel("out/gen.png")).toBe("gen.png")
  const handles = mount(SendRenderer, {
    props: {
      call: {
        name: "qq_send",
        status: "done",
        args: { media: ["m4.1", "sticker:ok.png"] },
        result: "Sent as m5, m6",
      },
    },
  })
  expect(handles.findAll("[data-test=media]").map((w) => w.text())).toEqual([
    "chat media m4.1",
    "sticker ok.png",
  ])
  expect(mediaList("[broken")).toEqual(["[broken"])
})
