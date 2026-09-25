import federation from '@originjs/vite-plugin-federation'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue(), federation({
    name: 'EmeTools',
    filename: 'remoteEntry.js',
    exposes: {
      './Page': './src/Page.vue',
      './Config': './src/Config.vue',
      './AppPage': './src/AppPage.vue',
    },
    shared: {
      vue: { requiredVersion: false, generate: false, singleton: true },
      vuetify: { requiredVersion: false, generate: false, singleton: true },
      'vuetify/styles': { requiredVersion: false, generate: false, singleton: true },
    },
    format: 'esm',
  })],
  build: { target: 'esnext', cssCodeSplit: true },
})
