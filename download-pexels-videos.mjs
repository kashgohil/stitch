import { readFile, writeFile, mkdir, stat, open, rename, statfs } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import path from 'node:path';

const run = promisify(execFile);
const source = '/Users/darshilshah/stitch/pexels-italy-highest-resolution-download-links.json';
const folder = '/Users/darshilshah/Downloads/Pexels-Italy-Highest-Resolution-2026-10-16';
const manifestPath = path.join(folder, 'download-manifest.json');
const videos = JSON.parse(await readFile(source, 'utf8')).videos;
await mkdir(folder, { recursive: true });
let previous = { results: [] };
try { previous = JSON.parse(await readFile(manifestPath, 'utf8')); } catch (e) { if (e.code !== 'ENOENT') throw e; }
const results = [];
let next = 0;
let active = 0;
let savedBytes = 0;

async function verify(file) {
  const info = await stat(file);
  if (info.size < 32) throw new Error('Downloaded file is too small');
  const handle = await open(file, 'r');
  const header = Buffer.alloc(32);
  try { await handle.read(header, 0, 32, 0); } finally { await handle.close(); }
  if (header.toString('ascii', 4, 8) !== 'ftyp') throw new Error('Downloaded file is not an MP4');
  return info.size;
}

async function worker() {
  while (next < videos.length) {
    const video = videos[next++];
    const filename = `${video.id}_${video.width}x${video.height}.mp4`;
    const target = path.join(folder, filename);
    const partial = `${target}.part`;
    active++;
    try {
      const old = previous.results.find(r => r.id === video.id && r.status === 'downloaded');
      if (old) {
        const size = await verify(target);
        if (size !== old.bytes) throw new Error('Existing file size differs from manifest');
        results.push(old);
        savedBytes += size;
        continue;
      }
      const disk = await statfs(folder);
      if (disk.bavail * disk.bsize < 10 * 1024 ** 3) throw new Error('Less than 10 GB free disk space');
      const { stdout } = await run('curl', [
        '--silent', '--show-error', '--location', '--fail',
        '--connect-timeout', '30', '--max-time', '1800',
        '--retry', '3', '--retry-delay', '2', '--retry-all-errors',
        '--output', partial, '--write-out', '%{json}', video.download_link,
      ], { maxBuffer: 1024 * 1024 });
      const transfer = JSON.parse(stdout);
      if (transfer.http_code !== 200 || !transfer.content_type?.startsWith('video/')) {
        throw new Error(`Unexpected response: ${transfer.http_code} ${transfer.content_type}`);
      }
      const bytes = await verify(partial);
      if (bytes !== transfer.size_download) throw new Error('Transfer byte count mismatch');
      await rename(partial, target);
      results.push({ id: video.id, filename, status: 'downloaded', bytes, width: video.width, height: video.height, source: video.download_link, final_url: transfer.url_effective });
      savedBytes += bytes;
    } catch (error) {
      results.push({ id: video.id, filename, status: 'failed', error: error.message, source: video.download_link });
      console.error(`FAILED ${video.id}: ${error.message.slice(0, 200)}`);
    } finally {
      active--;
    }
  }
}

function progress() {
  console.log(JSON.stringify({ completed: results.filter(r => r.status === 'downloaded').length, failed: results.filter(r => r.status === 'failed').length, active, total: videos.length, gigabytes: +(savedBytes / 1e9).toFixed(2) }));
}
async function checkpoint() {
  const manifest = { folder, total_requested: videos.length, updated_at: new Date().toISOString(), results };
  await writeFile(`${manifestPath}.tmp`, JSON.stringify(manifest, null, 2) + '\n');
  await rename(`${manifestPath}.tmp`, manifestPath);
}
let saving = Promise.resolve();
const timer = setInterval(() => {
  progress();
  saving = saving.then(checkpoint).catch(e => console.error(`Checkpoint failed: ${e.message}`));
}, 10000);
console.log(`Downloading ${videos.length} videos to ${folder}`);
await Promise.all(Array.from({ length: 6 }, worker));
clearInterval(timer);
await saving;
await checkpoint();
progress();
if (results.some(r => r.status === 'failed')) process.exitCode = 1;
