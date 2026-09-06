import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

// This proxy is only active during explicit source development. It carries the
// browser's local session capability; it never reads GitHub credentials.
function localBoundary(): Plugin {
  return {
    name: "reposteward-local-development",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const origin = req.headers.origin;
        const site = req.headers["sec-fetch-site"];
        if (
          req.socket.remoteAddress !== "127.0.0.1" ||
          req.headers.host !== "127.0.0.1:5173" ||
          (origin && origin !== "http://127.0.0.1:5173") ||
          (site && site !== "none" && site !== "same-origin")
        ) {
          res.statusCode = 403;
          res.end("Local development requests only");
          return;
        }
        next();
      });
    },
  };
}

export default defineConfig(({ command }) => {
  const target = new URL(
    process.env.REPOSTEWARD_DEV_BACKEND || "http://127.0.0.1:8787",
  );
  if (
    command === "serve" &&
    (target.protocol !== "http:" ||
      target.hostname !== "127.0.0.1" ||
      target.username ||
      target.password ||
      target.pathname !== "/" ||
      target.search ||
      target.hash)
  ) {
    throw new Error("REPOSTEWARD_DEV_BACKEND must be a loopback HTTP origin");
  }
  return {
    plugins: [localBoundary(), react()],
    build: { manifest: true, sourcemap: false },
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      cors: false,
      proxy: {
        "/api/": {
          target: target.origin,
          changeOrigin: true,
          configure(proxy) {
            proxy.on("proxyReq", (request) =>
              request.setHeader("Origin", target.origin),
            );
          },
        },
      },
    },
  };
});
