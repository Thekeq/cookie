import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { brotliCompressSync, constants } from 'node:zlib'
import { defineConfig, type Plugin, type ResolvedConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Что имеет смысл жать: текст. woff2/png/mp3 уже сжаты — brotli поверх них
// даёт единицы байт и только тратит время сборки.
const COMPRESSIBLE = /\.(js|mjs|css|html|svg|json|txt|map|webmanifest)$/i
// Ниже ~1 КБ выигрыш меньше накладных расходов на отдельный файл.
const MIN_SIZE = 1024

/**
 * Precompress: кладёт рядом с каждым текстовым ассетом .br максимального
 * качества. Жмём один раз при сборке, а не на каждый запрос: nginx с
 * `brotli_static on` отдаёт готовый файл, и CPU сервера в это не упирается.
 * Динамический `brotli on` с quality 11 на лету никто не включает — дорого,
 * поэтому на лету обычно жмут на 4-5 и теряют десятки процентов.
 */
function brotliPrecompress(): Plugin {
  let config: ResolvedConfig
  return {
    name: 'cookie-brotli-precompress',
    apply: 'build',
    configResolved(c) {
      config = c
    },
    closeBundle() {
      const outDir = join(config.root, config.build.outDir)
      const walk = (dir: string): string[] =>
        readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
          const p = join(dir, e.name)
          return e.isDirectory() ? walk(p) : [p]
        })

      let files: string[]
      try {
        files = walk(outDir)
      } catch {
        return // сборки нет — жать нечего
      }

      let saved = 0
      for (const file of files) {
        if (!COMPRESSIBLE.test(file) || file.endsWith('.br')) continue
        const raw = readFileSync(file)
        if (raw.length < MIN_SIZE) continue
        const br = brotliCompressSync(raw, {
          params: {
            [constants.BROTLI_PARAM_QUALITY]: constants.BROTLI_MAX_QUALITY,
            [constants.BROTLI_PARAM_SIZE_HINT]: raw.length,
          },
        })
        // Если brotli не выиграл (бывает на мелочи), .br не создаём:
        // иначе nginx отдал бы файл больше оригинала.
        if (br.length >= raw.length) continue
        writeFileSync(file + '.br', br)
        saved += raw.length - br.length
      }
      if (saved > 0) {
        const sizes = files
          .filter((f) => COMPRESSIBLE.test(f) && !f.endsWith('.br'))
          .map((f) => statSync(f).size)
          .reduce((a, b) => a + b, 0)
        this.info?.(
          `brotli: ${(sizes / 1024).toFixed(1)} kB текста -> ` +
            `${((sizes - saved) / 1024).toFixed(1)} kB (.br)`,
        )
      }
    },
  }
}

export default defineConfig({
  plugins: [react(), brotliPrecompress()],
  server: {
    proxy: { '/api': 'http://localhost:8000' },
  },
  build: {
    // Хеш в имени — обязательное условие immutable-кеша: при изменении
    // содержимого меняется URL, поэтому браузеру незачем перепроверять.
    // Шрифты в public/ не хешируются, но в их имени зашита версия
    // семейства (rubik-v31-…), что даёт тот же эффект.
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
        // React живёт своей жизнью и между релизами не меняется — держим
        // его отдельным чанком, чтобы обновление игры не сбрасывало 140 кБ
        // вендора из кеша игроков.
        manualChunks(id: string) {
          if (id.includes('node_modules/react') || id.includes('node_modules/scheduler'))
            return 'react-vendor'
        },
      },
    },
    // Вкладки грузятся лениво — предупреждение о размере имеет смысл
    // получать раньше, чем чанк дорастёт до дефолтных 500 кБ.
    chunkSizeWarningLimit: 300,
  },
})
