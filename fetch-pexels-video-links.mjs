import { writeFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

const endpoint = new URL('https://www.pexels.com/en-us/api/v3/search/videos');
endpoint.search = new URLSearchParams({
  query: 'italy',
  page: '1',
  per_page: '100',
  resolution_name: '4K',
  min_duration: '10',
  max_duration: '30',
  people_count: '0',
  orientation: 'landscape',
  seo_tags: 'true',
});

const clientId = process.env.PEXELS_CLIENT_ID;
if (!clientId) throw new Error('PEXELS_CLIENT_ID is required.');

const headers = {
  accept: '*/*',
  'content-type': 'application/json',
  'pexels-client-id': clientId,
  referer: 'https://www.pexels.com/search/videos/italy/?orientation=landscape&min_duration=10&max_duration=30&resolution_name=4K&people_count=0',
  'user-agent': 'Mozilla/5.0',
  'x-client-type': 'react',
};

async function getPage(page) {
  const url = new URL(endpoint);
  url.searchParams.set('page', String(page));
  const args = ['-sS', '-L', '--max-time', '30', '--write-out', '\n%{http_code}', String(url)];
  for (const [name, value] of Object.entries(headers)) args.push('-H', `${name}: ${value}`);
  const { stdout } = await execFileAsync('curl', args, { maxBuffer: 5 * 1024 * 1024 });
  const split = stdout.lastIndexOf('\n');
  const status = Number(stdout.slice(split + 1));
  if (status !== 200) throw new Error(`Page ${page}: HTTP ${status}`);
  const response = JSON.parse(stdout.slice(0, split));
  if (!response.data || !response.pagination) throw new Error(`Page ${page}: invalid API response`);
  return response;
}

// The requested starting page is attempted explicitly. Pexels currently returns
// HTTP 500 for page 0, while the response metadata is one-based.
let pageZero;
let pageZeroError;
try {
  pageZero = await getPage(0);
} catch (error) {
  if (!error.message.includes('HTTP 500')) throw error;
  pageZeroError = error.message;
  console.warn(`${error.message}; continuing with one-based pages.`);
}

const first = await getPage(1);
const pages = [first];
const pageNumbers = [1];
if (pageZero) { pages.unshift(pageZero); pageNumbers.unshift(0); }
// Metadata and returned page sizes disagree. Check every advertised page,
// including pages beyond the first empty response; do not infer completeness.
const lastPageToCheck = Math.max(first.pagination.total_pages, Math.ceil(first.pagination.total_results / first.data.length)) + 1;
for (let page = 2; page <= lastPageToCheck; page += 4) {
  const numbers = Array.from({length: Math.min(4, lastPageToCheck - page + 1)}, (_, i) => page + i);
  const batch = await Promise.all(numbers.map(getPage));
  pages.push(...batch);
  pageNumbers.push(...numbers);
  console.log(`Checked through page ${numbers.at(-1)} of ${lastPageToCheck}`);
}
const finalPage = Math.max(...pageNumbers.filter((_, i) => pages[i].data.length));

const selected = pages.flatMap((result) => result.data.map((item) => {
  const allFiles = item.attributes.video.video_files;
  const files = allFiles.filter((file) => file.download_link);
  if (!files.length) throw new Error(`Video ${item.id} has no downloadable file.`);
  if (allFiles.some(file => !(file.width > 0 && file.height > 0))) throw new Error(`Video ${item.id} has invalid dimensions.`);
  const best = files.reduce((highest, file) =>
    (file.width * file.height > highest.width * highest.height ? file : highest));
  if (allFiles.some(file => file.width * file.height > best.width * best.height)) throw new Error(`Highest resolution for ${item.id} lacks a download link.`);
  return {
    id: item.id,
    width: best.width,
    height: best.height,
    download_link: best.download_link,
    selected_file_index: allFiles.indexOf(best),
    file_count: allFiles.length,
    last_file_is_highest_resolution: allFiles.at(-1).width * allFiles.at(-1).height === best.width * best.height,
  };
}));

const uniqueVideos = [...new Map(selected.map((entry) => [entry.id, entry])).values()];

await writeFile('pexels-italy-raw-pages.json', `${JSON.stringify({
  fetched_at: new Date().toISOString(),
  requested_per_page: 100,
  page_zero_error: pageZeroError,
  pages: pages.map((response, i) => ({requested_page: pageNumbers[i], response})),
})}\n`);

await writeFile('pexels-italy-highest-resolution-download-links.txt', `${uniqueVideos.map((entry) => entry.download_link).join('\n')}\n`);
await writeFile('pexels-italy-highest-resolution-download-links.json', `${JSON.stringify({
  query: 'italy',
  total_videos: uniqueVideos.length,
  returned_records_before_deduplication: selected.length,
  last_nonempty_page: finalPage,
  last_checked_page: lastPageToCheck,
  api_reported_total: first.pagination.total_results,
  completeness_verified: uniqueVideos.length === first.pagination.total_results,
  limitation: uniqueVideos.length === first.pagination.total_results ? null : 'All advertised pages were checked, but the returned unique record count does not match the API-reported total. Full dataset completeness cannot be confirmed.',
  selection: 'Maximum width × height among video_files with a download_link',
  videos: uniqueVideos,
}, null, 2)}\n`);

console.log(`Wrote ${uniqueVideos.length} unique highest-resolution download links from ${selected.length} returned records on pages 1–${finalPage}.`);
