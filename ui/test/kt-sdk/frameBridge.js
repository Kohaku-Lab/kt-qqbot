/**
 * Reserved surface for ``isolation: "frame"`` contributions.
 *
 * SDK v1 accepts frame contributions but does not render them: the host
 * shows a "not supported yet" placeholder. The protocol name and message
 * types below are reserved so a later SDK can implement the
 * host ↔ iframe ``postMessage`` bridge without renaming anything.
 */

export const FRAME_PROTOCOL = "kt-ui-frame/1"

export const FRAME_MESSAGES = Object.freeze({
  HELLO: "kt:frame:hello",
  READY: "kt:frame:ready",
  CONTEXT: "kt:frame:context",
  API_REQUEST: "kt:frame:api-request",
  API_RESPONSE: "kt:frame:api-response",
  NOTIFY: "kt:frame:notify",
  OPEN_TAB: "kt:frame:open-tab",
  RESIZE: "kt:frame:resize",
})

export const FRAME_SUPPORTED = false

export class FrameIsolationUnsupportedError extends Error {
  constructor() {
    super(`isolation "frame" is reserved; SDK v1 does not implement ${FRAME_PROTOCOL}`)
    this.name = "FrameIsolationUnsupportedError"
  }
}

/** Reserved constructor for the frame bridge; always throws in SDK v1. */
export function createFrameBridge() {
  throw new FrameIsolationUnsupportedError()
}
