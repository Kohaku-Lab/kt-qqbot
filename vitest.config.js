import { fileURLToPath, URL } from "node:url"
import { defineConfig } from "vitest/config"

export default defineConfig({
  resolve: {
    alias: {
      "@kohakuterrarium/ui-sdk": fileURLToPath(new URL("./ui/test/kt-sdk/sdk-mock.js", import.meta.url)),
      vue: "vue/dist/vue.esm-bundler.js",
    },
  },
  test: { environment: "jsdom", include: ["ui/test/**/*.test.js"] },
})
