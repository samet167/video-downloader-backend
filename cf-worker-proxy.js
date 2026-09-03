/**
 * Cloudflare Worker — Free YouTube Proxy
 * ========================================
 * Acts as a free edge forwarder/proxy for yt-dlp requests.
 * Runs on Cloudflare's global edge network (Free 100,000 requests/day).
 */

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Health check
    if (url.pathname === "/" || url.pathname === "/health") {
      return new Response(JSON.stringify({ status: "ok", service: "cf-yt-proxy" }), {
        headers: { "content-type": "application/json", "access-control-allow-origin": "*" }
      });
    }

    // Extract target URL from query parameter (?url=https://...)
    const targetUrl = url.searchParams.get("url") || url.pathname.slice(1);
    if (!targetUrl || !targetUrl.startsWith("http")) {
      return new Response(JSON.stringify({ error: "Missing or invalid 'url' parameter" }), {
        status: 400,
        headers: { "content-type": "application/json" }
      });
    }

    // Forward request with custom headers
    const headers = new Headers(request.headers);
    headers.set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36");
    headers.delete("host");

    try {
      const response = await fetch(targetUrl, {
        method: request.method,
        headers: headers,
        body: ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer(),
        redirect: "follow"
      });

      const responseHeaders = new Headers(response.headers);
      responseHeaders.set("access-control-allow-origin", "*");
      responseHeaders.set("access-control-allow-headers", "*");
      responseHeaders.set("access-control-allow-methods", "GET, POST, OPTIONS, HEAD");

      return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers: responseHeaders
      });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), {
        status: 502,
        headers: { "content-type": "application/json" }
      });
    }
  }
};
