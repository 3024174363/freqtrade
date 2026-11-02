import { mergeConfig } from 'vite';
import upstream from '/workspace/frequi/vite.config.ts';

// Keep the upstream frontend unchanged; route the development API to QR's backend.
export default mergeConfig(upstream, {
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    watch: { usePolling: true, interval: 500 },
    proxy: { '/api': { target: 'http://backend:8080', changeOrigin: true, ws: true } },
  },
});
