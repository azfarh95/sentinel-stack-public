// HTTP proxy: 0.0.0.0:8932 (IPv4) -> [::1]:8931 (IPv6 Playwright MCP)
// Flushes SSE headers immediately so EventSource connections don't time out.
const http = require('http');

const LISTEN_HOST = '127.0.0.1';
const LISTEN_PORT = 8932;
const TARGET_HOST = '::1';
const TARGET_PORT = 8931;

const server = http.createServer((req, res) => {
    const isSSE = (req.headers['accept'] || '').includes('text/event-stream');

    const options = {
        hostname: TARGET_HOST,
        port:     TARGET_PORT,
        path:     req.url,
        method:   req.method,
        headers:  req.headers,
        family:   6,
    };

    const proxy = http.request(options, (proxyRes) => {
        res.writeHead(proxyRes.statusCode, proxyRes.headers);
        if (isSSE) {
            res.flushHeaders();
            if (res.socket) res.socket.setNoDelay(true);
        }
        proxyRes.pipe(res, { end: true });
    });

    proxy.on('error', () => {
        if (!res.headersSent) {
            res.writeHead(502);
            res.end('Playwright MCP not reachable');
        }
    });

    req.pipe(proxy, { end: true });
});

server.listen(LISTEN_PORT, LISTEN_HOST, () => {
    console.log(`Playwright HTTP proxy: ${LISTEN_HOST}:${LISTEN_PORT} -> [${TARGET_HOST}]:${TARGET_PORT}`);
});
