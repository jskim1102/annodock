import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

async function readProjectFile(relativePath) {
  return await readFile(new URL(`../../${relativePath}`, import.meta.url), "utf8")
    .catch(() => "");
}

const [
  authCompose,
  devRunner,
  mainCompose,
  envExample,
  caddyfile,
  productionRunner,
  cloudflaredConfig,
  tunnelRenderer,
  proxyService,
  cloudflaredService,
  gitignore,
  nginx,
  app,
  indexHtml,
  appShell,
  authPages,
  projectsPage,
  uploadPage,
] = await Promise.all([
  readProjectFile("docker-compose.auth.yml"),
  readProjectFile("dev.sh"),
  readProjectFile("docker-compose.yml"),
  readProjectFile(".env.example"),
  readProjectFile("deploy/Caddyfile"),
  readProjectFile("deploy/prod.sh"),
  readProjectFile("deploy/cloudflared-config.yml"),
  readProjectFile("deploy/render-cloudflared-config.sh"),
  readProjectFile("deploy/annodock-proxy.service"),
  readProjectFile("deploy/annodock-tunnel.service"),
  readProjectFile(".gitignore"),
  readProjectFile("frontend/nginx.conf"),
  readProjectFile("frontend/src/App.tsx"),
  readProjectFile("frontend/index.html"),
  readProjectFile("frontend/src/components/AppShell.tsx"),
  readProjectFile("frontend/src/pages/AuthPages.tsx"),
  readProjectFile("frontend/src/pages/ProjectsPage.tsx"),
  readProjectFile("frontend/src/pages/UploadPage.tsx"),
]);

test("production auth derives every public redirect from one canonical HTTPS origin", () => {
  assert.match(envExample, /^PUBLIC_APP_URL=$/m);
  assert.doesNotMatch(envExample, /8003|8015|5188|9015|8090/);
  assert.match(authCompose, /OAUTH_REDIRECT_BASE:\s+\$\{PUBLIC_APP_URL:\?[^}]+\}/);
  assert.match(authCompose, /APP_BASE_URL:\s+\$\{PUBLIC_APP_URL:\?[^}]+\}/);
  assert.match(
    authCompose,
    /ALLOWED_REDIRECT_URIS:[\s\S]*\$\{PUBLIC_APP_URL\}\/auth\/callback/,
  );
  assert.match(authCompose, /CORS_ORIGINS:\s+\$\{PUBLIC_APP_URL:\?[^}]+\}/);
  assert.match(authCompose, /COOKIE_SECURE:\s+["']true["']/);
  assert.doesNotMatch(authCompose, /OAUTH_REDIRECT_BASE:\s+http:\/\/localhost/);
});

test("production OAuth credentials override the auth module's development app", () => {
  const credentialKeys = [
    "NAVER_CLIENT_ID",
    "NAVER_CLIENT_SECRET",
    "KAKAO_CLIENT_ID",
    "KAKAO_CLIENT_SECRET",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
  ];

  for (const key of credentialKeys) {
    assert.match(envExample, new RegExp(`^${key}=$`, "m"));
    assert.match(
      authCompose,
      new RegExp(`${key}:\\s+\\$\\{${key}:-\\}`),
      `${key} must remain inspectable when the project credential is absent`,
    );
    assert.doesNotMatch(authCompose, new RegExp(`\\$\\{${key}:\\?`));
    assert.match(devRunner, new RegExp(`\\b${key}\\b`));
  }

  for (const key of ["NAVER_SCOPE", "KAKAO_SCOPE", "GOOGLE_SCOPE"]) {
    assert.match(envExample, new RegExp(`^${key}=$`, "m"));
    assert.match(authCompose, new RegExp(`${key}:`));
  }
  assert.match(authCompose, /KAKAO_SCOPE:\s+["']?\$\{KAKAO_SCOPE-\}["']?/);

  const preUp = devRunner.match(/pre_up\(\)\s*\{([\s\S]*?)\n\}/)?.[1] ?? "";
  assert.match(preUp, /require_oauth_credentials/);
  assert.ok(
    preUp.indexOf("require_oauth_credentials") < preUp.indexOf("docker compose"),
    "the host runner must reject incomplete OAuth credentials before starting auth",
  );
  assert.match(devRunner, /OAuth[^\n]*환경변수[^\n]*비어/);
});

test("auth and its local mail service recover after Docker restarts", () => {
  const authService = authCompose.split("\n  auth:\n")[1]?.split("\n  mailhog:\n")[0] ?? "";
  const mailhogService = authCompose.split("\n  mailhog:\n")[1]?.split("\nnetworks:\n")[0] ?? "";

  assert.match(authService, /^    restart:\s+unless-stopped$/m);
  assert.match(mailhogService, /^    restart:\s+unless-stopped$/m);
});

test("the host runner fails closed before Docker when any OAuth credential is blank", async () => {
  const fixtureDir = await mkdtemp(join(tmpdir(), "annodock-auth-gate-"));
  const runnerPath = join(fixtureDir, "dev.sh");
  const fakeBin = join(fixtureDir, "bin");
  const dockerMarker = join(fixtureDir, "docker-called");
  const credentialKeys = [
    "NAVER_CLIENT_ID",
    "NAVER_CLIENT_SECRET",
    "KAKAO_CLIENT_ID",
    "KAKAO_CLIENT_SECRET",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
  ];

  try {
    await mkdir(fakeBin);
    await writeFile(runnerPath, devRunner);
    await chmod(runnerPath, 0o755);
    await writeFile(
      join(fixtureDir, ".env"),
      [
        "AUTH_PORT=0",
        "BACKEND_PORT=0",
        "FRONTEND_PORT=0",
        "DATABASE_URL=postgresql://unused",
        "PUBLIC_APP_URL=https://app.example.com",
        "",
      ].join("\n"),
    );
    await writeFile(
      join(fakeBin, "docker"),
      "#!/usr/bin/env bash\n: > \"${DOCKER_CALLED_MARKER:?}\"\nexit 97\n",
    );
    await chmod(join(fakeBin, "docker"), 0o755);

    const result = spawnSync("bash", [runnerPath, "up"], {
      cwd: fixtureDir,
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${fakeBin}:${process.env.PATH ?? ""}`,
        DOCKER_CALLED_MARKER: dockerMarker,
        ...Object.fromEntries(credentialKeys.map((key) => [key, ""])),
      },
    });

    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /OAuth[^\n]*환경변수[^\n]*비어/);
    for (const key of credentialKeys) assert.match(result.stderr, new RegExp(`\\b${key}\\b`));
    assert.equal(await readFile(dockerMarker, "utf8").catch(() => ""), "");
  } finally {
    await rm(fixtureDir, { recursive: true, force: true });
  }
});

test("non-OAuth auth deployment inputs stay mandatory at compose interpolation", () => {
  assert.match(authCompose, /PUBLIC_APP_URL:\?set PUBLIC_APP_URL in \.env/);
  assert.match(authCompose, /AUTH_PORT:\?set AUTH_PORT in \.env/);
});

test("Caddy routes APIs and exact SPA callbacks without exposing Vite", () => {
  assert.match(envExample, /^PROXY_PORT=$/m);
  assert.match(caddyfile, /http:\/\/:\{\$PROXY_PORT\}\s*\{/);
  assert.match(caddyfile, /\bbind\s+127\.0\.0\.1\b/);
  assert.doesNotMatch(caddyfile, /http:\/\/127\.0\.0\.1:\{\$PROXY_PORT\}/);
  assert.match(caddyfile, /reverse_proxy(?:\s+@\w+)?\s+127\.0\.0\.1:\{\$BACKEND_PORT\}/);
  assert.match(caddyfile, /reverse_proxy(?:\s+@\w+)?\s+127\.0\.0\.1:\{\$AUTH_PORT\}/);
  assert.match(caddyfile, /path\s+\/auth\/callback\s+\/reset/);
  assert.match(caddyfile, /root\s+\*\s+frontend\/dist/);
  assert.doesNotMatch(caddyfile, /5188|vite/i);
});

test("the production runner builds static assets and owns a paired up/down lifecycle", () => {
  assert.match(productionRunner, /npm\s+--prefix\s+frontend\s+run\s+build/);
  assert.match(productionRunner, /\bup\)/);
  assert.match(productionRunner, /\bdown\)/);
  assert.match(productionRunner, /\bCADDY_BIN\b["']?\s+run/);
  assert.doesNotMatch(productionRunner, /vite|dev\.sh/);
});

test("the named tunnel is a user service and only publishes the approved app hostname", () => {
  assert.match(cloudflaredConfig, /hostname:\s+app\.annodock\.com/);
  assert.match(cloudflaredConfig, /service:\s+http:\/\/127\.0\.0\.1:__PROXY_PORT__/);
  assert.doesNotMatch(cloudflaredConfig, /:8090/);
  assert.match(cloudflaredConfig, /service:\s+http_status:404/);
  assert.match(tunnelRenderer, /PROXY_PORT:\?/);
  assert.match(tunnelRenderer, /__PROXY_PORT__/);
  assert.match(cloudflaredService, /EnvironmentFile=.*\/\.env/);
  assert.match(cloudflaredService, /ExecStartPre=.*render-cloudflared-config\.sh/);
  assert.match(cloudflaredService, /\.prod-runtime\/cloudflared-config\.yml/);
  assert.match(gitignore, /^\.prod-runtime\/$/m);
  assert.match(cloudflaredService, /ExecStart=.*cloudflared/);
  assert.match(cloudflaredService, /Restart=on-failure/);
  assert.doesNotMatch(cloudflaredConfig, /api\.annodock\.com|auth\.annodock\.com/);
});

test("the user services recover the static proxy before reconnecting the tunnel", () => {
  assert.match(proxyService, /EnvironmentFile=.*\/\.env/);
  assert.match(proxyService, /ExecStartPre=.*npm.*--prefix frontend.*run build/);
  assert.match(proxyService, /ExecStart=.*caddy run.*deploy\/Caddyfile/);
  assert.match(proxyService, /Restart=on-failure/);
  assert.match(cloudflaredService, /Requires=annodock-proxy\.service/);
  assert.match(cloudflaredService, /After=.*annodock-proxy\.service/);
});

test("the production proxy keeps the SPA callback local and proxies all other auth traffic", () => {
  assert.match(nginx, /client_max_body_size\s+8m;/);
  const callbackLocation = nginx.indexOf("location = /auth/callback");
  const authLocation = nginx.indexOf("location /auth/");
  assert.ok(callbackLocation >= 0, "the SPA OAuth callback must have an exact location");
  assert.ok(authLocation > callbackLocation, "the exact SPA callback must precede the auth proxy");
  assert.match(nginx, /location \/auth\/\s*\{[\s\S]*proxy_pass http:\/\/auth:8000;/);
  assert.match(nginx, /location \/api\/\s*\{[\s\S]*proxy_pass http:\/\/backend:8000;/);
});

test("host and container runtimes mount the same physical storage root", () => {
  assert.match(mainCompose, /\.\/backend\/storage:\/app\/storage/);
  assert.doesNotMatch(mainCompose, /\n\s*- \.\/storage:\/app\/storage/);
});

test("auth-service reset links remain compatible with the canonical password reset page", () => {
  assert.match(app, /path === "\/reset"/);
  assert.match(app, /path === "\/password-reset"/);
});

test("all production-visible product labels use the Annodock brand", () => {
  const visibleSources = [indexHtml, appShell, authPages, projectsPage, uploadPage].join("\n");
  assert.match(visibleSources, /Annodock/);
  assert.doesNotMatch(visibleSources, /DeepLabel/);
});
