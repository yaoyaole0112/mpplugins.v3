import federation from '@originjs/vite-plugin-federation'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [
    vue(),
    federation({
      name: 'DirectoryRename',
      filename: 'remoteEntry.js',
      exposes: {
        './Config': './src/Config.vue',
        './Page': './src/Page.vue',
      },
      shared: { vue: { requiredVersion: false, generate: false, singleton: true } },
      format: 'esm',
    }),
  ],
  build: { target: 'esnext', cssCodeSplit: true },
})
