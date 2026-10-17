import federation from '@originjs/vite-plugin-federation'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue(), federation({
    name: 'Ed2kFolderWatcher', filename: 'remoteEntry.js',
    exposes: { './Page': './src/Page.vue', './Config': './src/Config.vue', './AppPage': './src/Page.vue' },
    shared: { vue: { requiredVersion: false, generate: false, singleton: true } }, format: 'esm',
  })],
  build: { target: 'esnext', cssCodeSplit: true },
})
