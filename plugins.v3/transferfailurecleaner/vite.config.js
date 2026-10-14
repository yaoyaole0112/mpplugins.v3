import federation from '@originjs/vite-plugin-federation'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [
    vue(),
    federation({
      name: 'TransferFailureCleaner',
      filename: 'remoteEntry.js',
      exposes: { './Config': './src/components/Config.vue' },
      shared: {
        vue: { requiredVersion: false, generate: false, singleton: true },
        vuetify: { requiredVersion: false, generate: false, singleton: true },
        'vuetify/styles': { requiredVersion: false, generate: false, singleton: true },
      },
      format: 'esm',
    }),
  ],
  build: { target: 'esnext', minify: false, cssCodeSplit: true },
  css: { postcss: { plugins: [{ postcssPlugin: 'remove-vuetify-charset', AtRule: { charset: rule => rule.remove() } }] } },
})
