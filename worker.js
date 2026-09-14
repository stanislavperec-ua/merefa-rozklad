/**
 * Cloudflare Worker: шлюз до сайту swrailway.gov.ua.
 *
 * Навіщо: сайт УЗ відкидає з'єднання з великих хмар (Amazon, Microsoft, Google), тому
 * ні GitHub Actions, ні Render до нього не дістають, навіть із Франкфурта. Мережа
 * Cloudflare проходить, а Workers безкоштовні (100 000 запитів на добу).
 *
 * Розгортання: https://dash.cloudflare.com → Compute (Workers) → Create → Start from Hello World
 * → Deploy → Edit code → вставити цей файл → Deploy.
 *
 * Використання:
 *   GET /            health-check
 *   GET /fetch?url=<URL-encoded сторінка УЗ>   проксі сторінки
 */

const ALLOWED_HOSTS = ["swrailway.gov.ua", "www.swrailway.gov.ua"];
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 merefa-rozklad/2.0";

function withCors(resp) {
  const h = new Headers(resp.headers);
  h.set("Access-Control-Allow-Origin", "*");
  h.set("Access-Control-Allow-Headers", "Content-Type");
  h.set("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  return new Response(resp.body, { status: resp.status, headers: h });
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return withCors(new Response(null, { status: 204 }));
    }

    if (url.pathname === "/" || url.pathname === "") {
      return withCors(new Response("OK", { headers: { "Content-Type": "text/plain; charset=utf-8" } }));
    }

    if (url.pathname !== "/fetch") {
      return withCors(new Response("not found", { status: 404 }));
    }

    const target = url.searchParams.get("url");
    if (!target || !target.startsWith("https://")) {
      return withCors(new Response("bad url", { status: 400 }));
    }

    let host;
    try {
      host = new URL(target).hostname.toLowerCase();
    } catch (e) {
      return withCors(new Response("bad url", { status: 400 }));
    }
    if (!ALLOWED_HOSTS.includes(host)) {
      return withCors(new Response("host not allowed", { status: 403 }));
    }

    try {
      const upstream = await fetch(target, {
        headers: { "User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8" },
        cf: { cacheTtl: 0, cacheEverything: false },
      });
      const body = await upstream.arrayBuffer();
      return withCors(new Response(body, {
        status: upstream.status,
        headers: { "Content-Type": upstream.headers.get("Content-Type") || "text/html; charset=utf-8" },
      }));
    } catch (e) {
      return withCors(new Response("gateway error: " + e, { status: 502 }));
    }
  },
};
