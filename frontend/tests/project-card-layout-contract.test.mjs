import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const projectsPage = await readFile(
  new URL("../src/pages/ProjectsPage.tsx", import.meta.url),
  "utf8",
);
const appCss = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

test("projects render as expandable cards with metadata and header actions", () => {
  assert.match(projectsPage, /className="project-card-list"/);
  assert.match(
    projectsPage,
    /<article[\s\S]*?project-card[\s\S]*?is-expanded[\s\S]*?aria-labelledby=\{`project-title-\$\{project\.id\}`\}/,
  );

  const headerStart = projectsPage.indexOf('<header className="project-card-header">');
  const headerEnd = projectsPage.indexOf("</header>", headerStart);
  const headerMarkup = projectsPage.slice(headerStart, headerEnd);

  assert.notEqual(headerStart, -1);
  assert.notEqual(headerEnd, -1);
  assert.match(headerMarkup, /id=\{`project-title-\$\{project\.id\}`\}/);
  assert.match(headerMarkup, /className="project-card-meta"/);
  assert.match(headerMarkup, /데이터셋/);
  assert.match(headerMarkup, /project\.dataset_count\.toLocaleString\(\)/);
  assert.match(headerMarkup, /이미지/);
  assert.match(headerMarkup, /project\.image_count\.toLocaleString\(\)/);
  assert.match(headerMarkup, /클래스/);
  assert.match(headerMarkup, /project\.class_count\.toLocaleString\(\)/);
  assert.match(headerMarkup, /생성/);
  assert.match(headerMarkup, /dateLabel\(project\.created_at\)/);
  assert.match(headerMarkup, /AI 학습[\s\S]*병합[\s\S]*데이터셋/);
});

test("the expansion toggle and card body have a labelled ARIA relationship", () => {
  assert.match(projectsPage, /aria-expanded=\{isExpanded\}/);
  assert.match(projectsPage, /aria-controls=\{`project-details-\$\{project\.id\}`\}/);
  assert.match(
    projectsPage,
    /id=\{`project-details-\$\{project\.id\}`\}[\s\S]*?className="project-card-body"[\s\S]*?aria-labelledby=\{`project-title-\$\{project\.id\}`\}/,
  );
});

test("expanded project cards contain chips and a nested dataset table", () => {
  assert.match(projectsPage, /className="project-chip-strip"/);
  assert.match(projectsPage, /className="class-image-count"/);
  assert.match(projectsPage, /className="class-image-count is-total"/);
  assert.match(projectsPage, />전체 이미지</);
  assert.doesNotMatch(projectsPage, /클래스별 이미지/);

  assert.match(projectsPage, /<table className="project-dataset-table">/);
  assert.match(projectsPage, /<thead>[\s\S]*?<tbody>/);
  assert.match(projectsPage, /aria-label=\{`\$\{project\.name\} 데이터셋 전체 선택`\}/);
  for (const label of ["데이터셋", "검수 상태", "진행률"]) {
    assert.match(projectsPage, new RegExp(`>${label}<`));
  }
  assert.match(projectsPage, /<span className="sr-only">작업<\/span>/);
  assert.match(projectsPage, /<tr className=\{`dataset-row/);
  assert.doesNotMatch(projectsPage, /<td colSpan=\{6\}>\s*<div className="dataset-row-content">/);
});

test("expanded cards use the approved pale accent treatment without hard-coded colors", () => {
  const listRule = appCss.match(/\.project-card-list\s*\{([^}]*)\}/)?.[1] ?? "";
  const groupRule = appCss.match(/\.project-card\s*\{([^}]*)\}/)?.[1] ?? "";
  const expandedRule = appCss.match(/\.project-card\.is-expanded\s*\{([^}]*)\}/)?.[1] ?? "";
  const expandedHeaderRule = appCss.match(
    /\.project-card\.is-expanded\s*>\s*\.project-card-header\s*\{([^}]*)\}/,
  )?.[1] ?? "";

  assert.match(listRule, /gap:/);
  assert.match(groupRule, /border:/);
  assert.match(expandedRule, /box-shadow:\s*inset 4px 0 0 var\(--color-accent\);/);
  assert.match(expandedRule, /var\(--color-accent/);
  assert.match(expandedHeaderRule, /background:/);
  assert.match(expandedHeaderRule, /var\(--color-accent/);
  assert.doesNotMatch(expandedRule + expandedHeaderRule, /#[0-9a-f]{3,8}|rgba?\(/i);
  assert.match(appCss, /\.project-dataset-table\s*\{[^}]*width:\s*100%;/);
});
