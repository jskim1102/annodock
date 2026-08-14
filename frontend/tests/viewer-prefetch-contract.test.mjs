import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const authenticatedImage = await readFile(
  new URL("../src/components/AuthenticatedImage.tsx", import.meta.url),
  "utf8",
);
const viewer = await readFile(
  new URL("../src/pages/Viewer.tsx", import.meta.url),
  "utf8",
);
const client = await readFile(
  new URL("../src/api/client.ts", import.meta.url),
  "utf8",
);
const authStore = await readFile(
  new URL("../src/store/auth.ts", import.meta.url),
  "utf8",
);

test("authenticated images acquire shared blob URLs instead of owning throwaway URLs", () => {
  assert.match(authenticatedImage, /acquireAuthenticatedResource/);
  assert.match(authenticatedImage, /peekAuthenticatedResource/);
  assert.match(authenticatedImage, /prefetchAuthenticatedResource/);
  assert.match(authenticatedImage, /downloadResponse\(resourcePath, \{ signal \}\)/);
  assert.match(client, /cache: init\?\.cache \?\? "no-store"/);
  assert.doesNotMatch(authenticatedImage, /URL\.createObjectURL/);
  assert.doesNotMatch(authenticatedImage, /URL\.revokeObjectURL/);
});

test("auth teardown clears every user-scoped image blob", () => {
  assert.match(authStore, /clearAuthenticatedResourceCache/);
  assert.match(
    authStore,
    /export function clearAuthSession\(\): void \{[\s\S]*clearAuthenticatedResourceCache\(\)[\s\S]*publish\(EMPTY_SNAPSHOT\)/,
  );
  assert.match(
    authStore,
    /snapshot\.user\?\.id[^;]*user\.id[\s\S]*clearAuthenticatedResourceCache\(\)/,
  );
});

test("viewer prefetches three images and annotations on each side during idle time", () => {
  assert.match(viewer, /const PREFETCH_RADIUS = 3/);
  assert.match(viewer, /scheduleIdleWork/);
  assert.match(viewer, /prefetchAuthenticatedResource\(imageResourceUrl\(image\.id\)\)/);
  assert.match(viewer, /loadImageAnnotations\(image\.id\)/);
  assert.match(viewer, /let offset = -PREFETCH_RADIUS/);
  assert.match(viewer, /offset <= PREFETCH_RADIUS/);
  assert.doesNotMatch(viewer, /for \(const index of \[imageIndex - 1, imageIndex \+ 1\]\)/);
});

test("annotation prefetch is deduplicated, dataset-scoped, and refreshed after save", () => {
  assert.match(viewer, /annotationCacheRef = useRef\(new Map<number, AnnotationResponse>\(\)\)/);
  assert.match(viewer, /annotationFlightsRef = useRef\(new Map<number,/);
  assert.match(viewer, /const loadImageAnnotations = useCallback/);
  assert.match(viewer, /annotationFlightsRef\.current\.get\(imageId\)/);
  assert.match(viewer, /annotationCacheRef\.current\.set\(imageId, response\)/);
  assert.match(viewer, /annotationGenerationRef\.current/);
  assert.match(viewer, /controller\.abort\(\)/);
  assert.match(viewer, /annotationCacheRef\.current\.set\(active\.id,/);
  assert.match(client, /getImageAnnotations\([\s\S]*signal\?: AbortSignal/);
});
