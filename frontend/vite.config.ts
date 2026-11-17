import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const projectEnv = loadEnv(mode, "..", "");
  const frontendEnv = loadEnv(mode, ".", "");
  const target =
    frontendEnv.DIDGO_API_ORIGIN ??
    `http://127.0.0.1:${projectEnv.API_PORT ?? "8000"}`;

  const proxy = {
    "/api": {
      target,
      changeOrigin: true,
      rewrite: (path: string) => path.replace(/^\/api(?=\/)/, ""),
    },
    "/ocr-api": {
      target: "http://127.0.0.1:8001",
      changeOrigin: true,
      headers: {
        Authorization: "Bearer local-test-token",
      },
      rewrite: (path: string) => path.replace(/^\/ocr-api(?=\/)/, ""),
    },
  };

  return { server: { proxy }, preview: { proxy } };
});
