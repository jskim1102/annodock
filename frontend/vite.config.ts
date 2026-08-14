import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

function requiredPort(value: string | undefined, name: string): number {
  const port = Number.parseInt(value ?? "", 10);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`${name} must be a valid TCP port`);
  }
  return port;
}

function requiredUrl(value: string | undefined, name: string): string {
  if (!value) {
    throw new Error(`${name} must be set`);
  }
  return new URL(value).toString();
}

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, "..", "");
  const server =
    command === "serve"
      ? (() => {
          const frontendPort = requiredPort(
            env.FRONTEND_PORT,
            "FRONTEND_PORT",
          );
          const backendPort = requiredPort(env.BACKEND_PORT, "BACKEND_PORT");
          const apiTarget = requiredUrl(
            env.VITE_API_TARGET ?? `http://localhost:${backendPort}`,
            "VITE_API_TARGET",
          );
          const authTarget = requiredUrl(env.AUTH_BASE_URL, "AUTH_BASE_URL");

          return {
            host: true,
            allowedHosts: true,
            port: frontendPort,
            strictPort: true,
            proxy: {
              "/api": { target: apiTarget, changeOrigin: true },
              "/auth": { target: authTarget, changeOrigin: true },
            },
          };
        })()
      : undefined;

  return {
    envDir: "..",
    plugins: [react()],
    server,
  };
});
