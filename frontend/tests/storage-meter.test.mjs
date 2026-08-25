import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const appShell = await readFile(
  new URL("../src/components/AppShell.tsx", import.meta.url),
  "utf8",
);
const client = await readFile(
  new URL("../src/api/client.ts", import.meta.url),
  "utf8",
);
const uploadPage = await readFile(
  new URL("../src/pages/UploadPage.tsx", import.meta.url),
  "utf8",
);
const projectsPage = await readFile(
  new URL("../src/pages/ProjectsPage.tsx", import.meta.url),
  "utf8",
);
const runDetailPage = await readFile(
  new URL("../src/pages/RunDetailPage.tsx", import.meta.url),
  "utf8",
);
const runsPage = await readFile(
  new URL("../src/pages/RunsPage.tsx", import.meta.url),
  "utf8",
);

test("the API client exposes the authenticated storage quota endpoint", () => {
  assert.match(client, /export interface StorageQuota\s*\{/);
  assert.match(client, /used_bytes:\s*number/);
  assert.match(client, /limit_bytes:\s*number/);
  assert.match(client, /export function getStorageQuota\(/);
  assert.match(client, /requestJson<StorageQuota>\("\/api\/storage"\)/);
});

test("one quota request is cached per access token and rejected requests are evicted", () => {
  assert.match(client, /storageQuotaCache\?\.tokenKey === tokenKey/);
  assert.match(client, /return storageQuotaCache\.request/);
  assert.match(client, /storageQuotaCache = \{ tokenKey, request \}/);
  assert.match(
    client,
    /void request\.catch\(\(\) => \{[\s\S]*?storageQuotaCache\?\.request === request[\s\S]*?storageQuotaCache = null/,
  );
  assert.match(client, /export function resetStorageQuotaCache\(\)/);
});

test("the shell fetches once and shares the result with both meter variants", () => {
  assert.equal(
    appShell.match(/getStorageQuota\(/g)?.length,
    1,
    "the parent shell, not each meter, must start the cached request",
  );
  assert.match(appShell, /<StorageMeter quota=\{storageQuota\} \/>/);
  assert.match(appShell, /<StorageMeter compact quota=\{storageQuota\} \/>/);
});

test("a valid token pair loads quota even when user validation is unavailable", () => {
  assert.match(
    appShell,
    /const storageTokenKey = session\.accessToken && session\.refreshToken[\s\S]*?\? session\.accessToken[\s\S]*?: null/,
  );
  assert.match(appShell, /getStorageQuota\(storageTokenKey\)/);
  assert.doesNotMatch(appShell, /session\.user\?\.id/);
});

test("every started upload attempt invalidates quota from finally", () => {
  assert.match(client, /export const STORAGE_QUOTA_INVALIDATED_EVENT/);
  assert.match(client, /export function invalidateStorageQuotaCache\(\)/);
  assert.match(client, /dispatchEvent\(new Event\(STORAGE_QUOTA_INVALIDATED_EVENT\)\)/);
  assert.match(appShell, /addEventListener\(STORAGE_QUOTA_INVALIDATED_EVENT, loadStorageQuota\)/);
  assert.match(appShell, /removeEventListener\(STORAGE_QUOTA_INVALIDATED_EVENT, loadStorageQuota\)/);

  const handlerStart = uploadPage.indexOf("const startUpload = async () => {");
  const handlerEnd = uploadPage.indexOf("const continueUploadAfterClassResolution", handlerStart);
  const handler = uploadPage.slice(handlerStart, handlerEnd);
  const catchStart = handler.lastIndexOf("} catch (reason: unknown) {");
  const finallyStart = handler.indexOf("} finally {", catchStart);
  const invalidation = handler.indexOf("invalidateStorageQuotaCache()", finallyStart);
  assert.ok(catchStart >= 0 && finallyStart > catchStart && invalidation > finallyStart);
  assert.equal(handler.match(/invalidateStorageQuotaCache\(\)/g)?.length, 1);
});

test("successful merge and extraction mutations each invalidate quota exactly once", () => {
  const mergeStart = projectsPage.indexOf("const submitMerge = async");
  const mergeEnd = projectsPage.indexOf("const useExistingMergedDataset", mergeStart);
  const mergeHandler = projectsPage.slice(mergeStart, mergeEnd);
  assert.match(
    mergeHandler,
    /const merged = [\s\S]*?await mergeDatasets\([\s\S]*?\);\s*invalidateStorageQuotaCache\(\);\s*await continueMergedAction/,
  );
  assert.equal(mergeHandler.match(/invalidateStorageQuotaCache\(\)/g)?.length, 1);

  const conflictStart = projectsPage.indexOf("const useExistingMergedDataset");
  const conflictEnd = projectsPage.indexOf("const submitClassExtraction", conflictStart);
  const conflictHandler = projectsPage.slice(conflictStart, conflictEnd);
  assert.doesNotMatch(conflictHandler, /invalidateStorageQuotaCache\(\)/);

  const extractionStart = projectsPage.indexOf("const submitClassExtraction = async");
  const extractionEnd = projectsPage.indexOf("const removeProjectFromView", extractionStart);
  const extractionHandler = projectsPage.slice(extractionStart, extractionEnd);
  assert.match(
    extractionHandler,
    /await extractDatasetClasses\([\s\S]*?\);\s*invalidateStorageQuotaCache\(\);\s*try \{\s*await syncProjectsAfterDatasetMutation/,
  );
  assert.equal(extractionHandler.match(/invalidateStorageQuotaCache\(\)/g)?.length, 1);
});

test("run detail polling invalidates quota once on an observed active-to-terminal transition", () => {
  assert.match(runDetailPage, /const observedRunStateRef = useRef<RunState \| null>\(null\)/);
  assert.match(runDetailPage, /observedRunStateRef\.current = null/);
  assert.match(
    runDetailPage,
    /const previousState = observedRunStateRef\.current;[\s\S]*?ACTIVE_STATES\.has\(previousState\)[\s\S]*?!ACTIVE_STATES\.has\(nextState\)[\s\S]*?invalidateStorageQuotaCache\(\)[\s\S]*?observedRunStateRef\.current = nextState/,
  );
});

test("run list polling coalesces terminal transitions into one quota invalidation", () => {
  assert.match(
    runsPage,
    /const observedRunStatesRef = useRef<Map<number, RunState>>\(new Map\(\)\)/,
  );
  assert.match(
    runsPage,
    /const completedSinceLastPoll = response\.items\.some\([\s\S]*?observedRunStatesRef\.current\.get\(run\.id\)[\s\S]*?ACTIVE_STATES\.has\(previousState\)[\s\S]*?!ACTIVE_STATES\.has\(run\.state\)/,
  );
  assert.match(
    runsPage,
    /observedRunStatesRef\.current = new Map\([\s\S]*?if \(completedSinceLastPoll\) invalidateStorageQuotaCache\(\)/,
  );
});

test("the meter always pairs used bytes with the total quota", () => {
  const meterStart = appShell.indexOf("export function StorageMeter");
  const meterEnd = appShell.indexOf("export function AppShell", meterStart);
  const meter = appShell.slice(meterStart, meterEnd);

  assert.match(appShell, /from "\.\.\/utils\/formatBytes"/);
  assert.match(meter, /formatBytes\(quota\.used_bytes\)/);
  assert.match(meter, /formatBytes\(quota\.limit_bytes\)/);
  assert.match(
    meter,
    /`\$\{formatBytes\(quota\.used_bytes\)\} \/ \$\{formatBytes\(quota\.limit_bytes\)\}`/,
  );
  assert.match(meter, /usageLabel \?\? "—"/);
  assert.doesNotMatch(meter, /referencedLabel|quota\.referenced_bytes|참조/);
});

test("quota refresh keeps the last value and retries a transient API failure", () => {
  const effectStart = appShell.indexOf("useEffect(() => {", appShell.indexOf("storageTokenKey"));
  const effectEnd = appShell.indexOf("useEffect(() => {", effectStart + 1);
  const effect = appShell.slice(effectStart, effectEnd);
  const loaderStart = effect.indexOf("const loadStorageQuota");
  const loaderEnd = effect.indexOf("loadStorageQuota();", loaderStart);
  const loader = effect.slice(loaderStart, loaderEnd);

  assert.match(appShell, /const STORAGE_QUOTA_RETRY_MS = [\d_]+;/);
  assert.doesNotMatch(loader, /setStorageQuota\(null\)/);
  assert.match(loader, /catch\(\(\) => \{[\s\S]*?scheduleStorageQuotaRetry\(\)/);
  assert.match(effect, /setStorageQuota\(null\);[\s\S]*?loadStorageQuota\(\);/);
  assert.match(
    effect,
    /window\.setTimeout\(loadStorageQuota, STORAGE_QUOTA_RETRY_MS\)/,
  );
  assert.match(effect, /window\.clearTimeout\(retryTimer\)/);
});

test("no fabricated quota number is used as a loading or error fallback", () => {
  assert.doesNotMatch(appShell, /12\.4|100 GiB/);
  assert.doesNotMatch(appShell, /used_bytes\s*(?:\|\||\?\?)\s*0/);
  assert.doesNotMatch(appShell, /limit_bytes\s*(?:\|\||\?\?)\s*\d/);
  assert.doesNotMatch(appShell, /useState\([^)]*used_bytes:\s*0/);
});

test("logout clears the in-memory quota cache", () => {
  assert.match(appShell, /resetStorageQuotaCache\(\)/);
});
