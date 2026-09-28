// Write `<asset>.br` and `<asset>.gz` next to every text asset in dist/ so the API can serve a
// precompressed sibling (dagentos/api/ui.py) instead of compressing on every request. Runs as
// the last step of `npm run build`. Node's zlib has brotli and gzip built in: no dependency.
//
// Only compressible text types are done; images and fonts are already compressed and would
// grow. Anything under 1 KiB is skipped: the headers cost more than the saving. Each sibling is
// written atomically (tmp + rename) so a killed build never leaves a truncated .br for the
// server to hand out.
import { promises as fs } from "node:fs";
import path from "node:path";
import zlib from "node:zlib";

const DIST = path.resolve(process.argv[2] ?? "dist");
const TEXT = new Set([".js", ".mjs", ".css", ".html", ".svg", ".json", ".map", ".txt", ".webmanifest"]);
const MIN_BYTES = 1024;

async function* walk(dir) {
  for (const e of await fs.readdir(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) yield* walk(p);
    else yield p;
  }
}

async function writeAtomic(target, bytes) {
  const tmp = `${target}.tmp-${process.pid}`;
  await fs.writeFile(tmp, bytes);
  await fs.rename(tmp, target);
}

let done = 0, skipped = 0, rawTotal = 0, brTotal = 0, gzTotal = 0;
for await (const file of walk(DIST)) {
  const ext = path.extname(file);
  if (ext === ".br" || ext === ".gz" || !TEXT.has(ext)) continue;
  const raw = await fs.readFile(file);
  if (raw.length < MIN_BYTES) { skipped++; continue; }
  const br = zlib.brotliCompressSync(raw, {
    params: {
      [zlib.constants.BROTLI_PARAM_QUALITY]: 11,
      [zlib.constants.BROTLI_PARAM_SIZE_HINT]: raw.length,
    },
  });
  const gz = zlib.gzipSync(raw, { level: 9 });
  // Never ship a sibling that is not actually smaller: the server would pick it blindly.
  if (br.length < raw.length) { await writeAtomic(`${file}.br`, br); brTotal += br.length; }
  if (gz.length < raw.length) { await writeAtomic(`${file}.gz`, gz); gzTotal += gz.length; }
  rawTotal += raw.length; done++;
}
const pct = (n) => rawTotal ? `${(100 - (100 * n) / rawTotal).toFixed(0)}% smaller` : "n/a";
console.log(`precompress: ${done} asset(s) (${skipped} under ${MIN_BYTES} B skipped); ` +
  `raw ${rawTotal} B, br ${brTotal} B (${pct(brTotal)}), gz ${gzTotal} B (${pct(gzTotal)})`);
