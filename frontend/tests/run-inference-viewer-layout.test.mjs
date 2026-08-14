import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const runDetailPage = await readFile(
  new URL("../src/pages/RunDetailPage.tsx", import.meta.url),
  "utf8",
);
const appCss = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

test("best.pt inference uses one large viewer followed by a scrubber and filmstrip", () => {
  const sectionStart = runDetailPage.indexOf('className="inference-section"');
  const sectionMarkup = runDetailPage.slice(sectionStart);

  assert.match(runDetailPage, /const INFERENCE_PAGE_SIZE = 16/);
  assert.match(sectionMarkup, /className="card inference-viewer"/);
  assert.match(sectionMarkup, /className="inference-stage"/);
  assert.match(sectionMarkup, /className="inference-image-viewport"/);
  assert.match(sectionMarkup, /className="inference-timeline"/);
  assert.match(sectionMarkup, /className="inference-scrub"/);
  assert.match(sectionMarkup, /role="progressbar"/);
  assert.match(sectionMarkup, /className="inference-thumb-strip"/);
  assert.ok(
    sectionMarkup.indexOf('className="inference-stage"')
      < sectionMarkup.indexOf('className="inference-timeline"'),
  );
  assert.doesNotMatch(sectionMarkup, /inference-thumb-grid/);
});

test("the inference viewer keeps image navigation and usable zoom controls", () => {
  assert.match(runDetailPage, /const \[inferenceZoom, setInferenceZoom\] = useState\(100\)/);
  assert.match(runDetailPage, /aria-label="축소"/);
  assert.match(runDetailPage, /aria-label="확대"/);
  assert.match(runDetailPage, />화면 초기화</);
  assert.match(runDetailPage, /navigateInferenceImage\(-1\)/);
  assert.match(runDetailPage, /navigateInferenceImage\(1\)/);
  assert.match(runDetailPage, /aria-current=\{selectedImage === index \? "true" : undefined\}/);
});

test("the inference stage and thumbnails match the full-width viewer hierarchy", () => {
  const viewerRule = appCss.match(/\.inference-viewer\s*\{([^}]*)\}/)?.[1] ?? "";
  const stageRule = appCss.match(/\.inference-stage\s*\{([^}]*)\}/)?.[1] ?? "";
  const viewportRule = appCss.match(/\.inference-image-viewport\s*\{([^}]*)\}/)?.[1] ?? "";
  const timelineRule = appCss.match(/\.inference-timeline\s*\{([^}]*)\}/)?.[1] ?? "";
  const stripRule = appCss.match(/\.inference-thumb-strip\s*\{([^}]*)\}/)?.[1] ?? "";
  const thumbRule = appCss.match(/\.inference-thumb\s*\{([^}]*)\}/)?.[1] ?? "";

  assert.match(viewerRule, /overflow:\s*hidden/);
  assert.match(stageRule, /min-height:\s*480px/);
  assert.match(stageRule, /height:\s*min\(68vh, 720px\)/);
  assert.match(stageRule, /background:\s*var\(--color-stage\)/);
  assert.match(viewportRule, /overflow:\s*auto/);
  assert.match(timelineRule, /border-top:\s*1px solid var\(--color-divider\)/);
  assert.match(stripRule, /display:\s*flex/);
  assert.match(stripRule, /overflow-x:\s*auto/);
  assert.match(thumbRule, /min-width:\s*96px/);
  assert.match(thumbRule, /opacity:\s*\.5/);
});
