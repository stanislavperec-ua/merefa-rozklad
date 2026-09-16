/**
 * Cloudflare Worker: шлюз до сайту swrailway.gov.ua і автоматичне оновлення розкладу.
 *
 * Навіщо шлюз: сайт УЗ відкидає з'єднання з великих хмар (Amazon, Microsoft, Google), тому
 * ні GitHub Actions, ні Render до нього не дістають, навіть із Франкфурта. Мережа
 * Cloudflare проходить, а Workers безкоштовні (100 000 запитів на добу).
 *
 * Навіщо розклад за розкладом (Cron Triggers): сайт УЗ ще й обмежує частоту запитів за
 * адресою відправника, і мережа Render під це обмеження потрапляє: збірка розтягується
 * на десять і більше хвилин. Тому сторінки качає воркер (сайт пускає його миттєво), а бот
 * їх лише розбирає. Це той самий швидкий шлях, яким працює кнопка ↻ у Mini App.
 *
 * Обмеження безкоштовного плану, під які підлаштований код:
 *   * 50 підзапитів на один виклик воркера  → робота ділиться на порції через /batch,
 *     кожна порція це окремий виклик зі своїм лімітом;
 *   * 10 мс процесорного часу на виклик     → сторінки не розбираються і навіть не
 *     читаються в пам'ять: тіло відповіді сайту одразу ллється потоком у бота;
 *   * 15 хв на виконання Cron Trigger       → з запасом, повне оновлення триває секунди.
 *
 * Розгортання: редактор дашборда автоматизації не піддається, тому код заливається
 * через API: PUT /accounts/<acc>/workers/scripts/merefa-uz-gateway, multipart із metadata
 * (обов'язково "keep_bindings": ["secret_text"], інакше злетить секрет) і цим файлом.
 * Налаштування воркера: секрет FAST_TOKEN (той самий, що у бота на Render), прив'язки
 * SELF (service на самого себе) і SCHEDULER (Durable Object, клас Scheduler), а також
 * Cron Trigger кожні півгодини у вікні 02-21 UTC (05:00-00:30 за Києвом). Щопівгодини
 * воркер будить бота по канал УЗ (затримки) і питає, чи настав слот розкладу 06:00 / 13:00
 * за Києвом; якщо не настав, на цьому й виходить.
 *
 * Маршрути:
 *   GET  /            health-check
 *   GET  /fetch?url=  проксі сторінки УЗ (ним користуються Mini App, бот і ПК)
 *   POST /run         запустити оновлення розкладу негайно (потрібен X-Fast-Token);
 *                     ?via=do виконує роботу в Durable Object у Варшаві
 *   POST /batch       службовий: воркер викликає сам себе, щоб не впертись у 50 підзапитів
 */

const ALLOWED_HOSTS = ["swrailway.gov.ua", "www.swrailway.gov.ua"];
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 merefa-rozklad/2.0";

const BOT = "https://merefa-rozklad.onrender.com";
const SELF = "https://merefa-uz-gateway.stanislav-perec.workers.dev";
const HORIZON_DAYS = 14;       // бот розширює горизонт для автоматики сам (див. CRON_HORIZON_DAYS)
const PAGES_PER_CALL = 20;     // сторінок на один виклик /batch: 20 качань + 20 пересилань = 40 підзапитів
const AT_ONCE = 5;             // скільки сторінок качаємо одночасно, щоб не навантажувати сайт
const WAKE_TIMEOUT_MS = 120000; // бот на безкоштовному Render прокидається до хвилини

function withCors(resp) {
  const h = new Headers(resp.headers);
  h.set("Access-Control-Allow-Origin", "*");
  h.set("Access-Control-Allow-Headers", "Content-Type");
  h.set("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  return new Response(resp.body, { status: resp.status, headers: h });
}

function allowedTarget(target) {
  if (!target || !target.startsWith("https://")) return null;
  try {
    const host = new URL(target).hostname.toLowerCase();
    return ALLOWED_HOSTS.includes(host) ? target : null;
  } catch (e) {
    return null;
  }
}

function tokenOk(request, env) {
  const given = request.headers.get("X-Fast-Token") || "";
  return Boolean(env.FAST_TOKEN) && given === env.FAST_TOKEN;
}

function botUrl(path) {
  return BOT + path;
}

async function askBot(path, body, env) {
  const init = {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-Fast-Token": env.FAST_TOKEN || "" },
    signal: AbortSignal.timeout(WAKE_TIMEOUT_MS),
  };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const r = await fetch(botUrl(path), init);
  if (!r.ok) throw new Error(path + ": HTTP " + r.status);
  return r.json();
}

// Адреса, яку бот віддає у плані, веде через цей самий воркер: усередині йдемо прямо
function targetOf(task) {
  try {
    const url = new URL(task.url);
    return allowedTarget(url.searchParams.get("url") || task.url);
  } catch (e) {
    return null;
  }
}

// Сторінка УЗ переливається в бота потоком: воркер її не читає, тому процесорний час ~0
async function relay(task, session, env) {
  const target = targetOf(task);
  if (!target) return { id: task.id, ok: false };
  try {
    const page = await fetch(target, {
      headers: { "User-Agent": UA, "Accept": "text/html,*/*;q=0.8" },
      cf: { cacheTtl: 0, cacheEverything: false },
      signal: AbortSignal.timeout(45000),
    });
    if (!page.ok) return { id: task.id, ok: false };
    const sent = await fetch(
      botUrl("/fast/page?session=" + encodeURIComponent(session) + "&id=" + encodeURIComponent(task.id)),
      {
        method: "POST",
        headers: {
          "Content-Type": "text/html; charset=utf-8",
          "X-Fast-Token": env.FAST_TOKEN || "",
        },
        body: page.body,
        signal: AbortSignal.timeout(90000),
      });
    return { id: task.id, ok: sent.ok };
  } catch (e) {
    return { id: task.id, ok: false };
  }
}

// Один виклик /batch: до PAGES_PER_CALL сторінок, кожна качається і одразу віддається боту.
// Хвилями по AT_ONCE, щоб не стукати в сайт двома десятками запитів одночасно.
async function handleBatch(request, env) {
  if (!tokenOk(request, env)) return new Response("forbidden", { status: 403 });
  const { session, tasks } = await request.json();
  if (!session || !Array.isArray(tasks)) return new Response("bad request", { status: 400 });
  const wanted = tasks.slice(0, PAGES_PER_CALL);
  const done = [];
  for (let i = 0; i < wanted.length; i += AT_ONCE) {
    const wave = await Promise.all(wanted.slice(i, i + AT_ONCE).map(t => relay(t, session, env)));
    done.push(...wave);
  }
  return Response.json({
    ok: done.filter(d => d.ok).length,
    failed: done.filter(d => !d.ok).map(d => d.id),
    colo: await colo(),
  });
}

// Порція виконується окремим викликом воркера, щоб мати власні 50 підзапитів.
// Викликати себе за публічною адресою Cloudflare не дає (віддає 404), тому потрібна
// службова прив'язка воркера на самого себе (Settings → Bindings → Service: SELF).
async function runBatch(session, tasks, env) {
  const url = (env.SELF_URL || SELF) + "/batch";
  const init = {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Fast-Token": env.FAST_TOKEN || "" },
    signal: AbortSignal.timeout(300000),
    body: JSON.stringify({ session, tasks }),
  };
  const r = env.SELF ? await env.SELF.fetch(url, init) : await fetch(url, init);
  if (!r.ok) throw new Error("/batch: HTTP " + r.status);
  return r.json();
}

// Сайт УЗ пускає не кожен дата-центр Cloudflare, а воркер виконується поруч із тим, хто
// його покликав. Знати колокацію треба, щоб розуміти, чому сторінки не даються.
async function colo() {
  try {
    const trace = await fetch("https://www.cloudflare.com/cdn-cgi/trace").then(r => r.text());
    return (trace.match(/colo=(\w+)/) || [])[1] || "?";
  } catch (e) {
    return "?";
  }
}

// Повне оновлення: план у бота → сторінки порціями → збірка і коміт
async function runUpdate(env, force) {
  const place = await colo();
  const log = ["дата-центр " + place];
  if (!env.FAST_TOKEN) return { ok: false, reason: "не задано секрет FAST_TOKEN" };

  if (!force) {
    const due = await askBot("/due?from=" + place, undefined, env);
    log.push("due: " + due.due + " (" + due.reason + ")");
    if (!due.due) return { ok: true, skipped: true, log };
  }

  const start = await askBot("/fast/start", { days: HORIZON_DAYS, force: true, from: place }, env);
  if (start.status !== "started") {
    log.push("бот відповів: " + start.status);
    return { ok: start.status === "running", skipped: true, log };
  }
  log.push("сесія " + start.session + ", сторінок " + start.tasks.length);

  // Один прохід качає все, що бот просить; наступні проходи добирають те, що не вдалося,
  // і забирають завдання другої фази (сторінки поїздів зі «Змінами руху»).
  let tasks = start.tasks;
  let phase = null;
  for (let round = 0; round < 4 && tasks.length; round++) {
    for (let i = 0; i < tasks.length; i += PAGES_PER_CALL) {
      const part = await runBatch(start.session, tasks.slice(i, i + PAGES_PER_CALL), env);
      log.push("порція " + part.ok + " із " + Math.min(PAGES_PER_CALL, tasks.length - i) +
               " (" + (part.colo || "?") + ")");
    }
    phase = await askBot("/fast/state?session=" + encodeURIComponent(start.session), undefined, env);
    log.push("фаза " + phase.phase + ", лишилось " + phase.pending);
    if (phase.phase === "ready") break;
    tasks = phase.tasks || [];
  }

  // Сайт УЗ пускає не кожен дата-центр Cloudflare, а воркер виконується поруч із тим, хто
  // його покликав. Якщо сторінки не даються, збірку не завершуємо: краще лишити старий
  // розклад, ніж зіпсувати його недокачаними датами.
  if (phase && phase.phase !== "ready" && phase.pending > 3) {
    log.push("сторінки не даються, збірку не завершую");
    return { ok: false, reason: "не вдалося завантажити " + phase.pending + " сторінок", log };
  }

  const finish = await askBot("/fast/finish", { session: start.session }, env);
  log.push("finish: " + finish.status);
  return { ok: true, log };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return withCors(new Response(null, { status: 204 }));
    }

    if (url.pathname === "/" || url.pathname === "") {
      return withCors(new Response("OK", { headers: { "Content-Type": "text/plain; charset=utf-8" } }));
    }

    if (url.pathname === "/batch") {
      return handleBatch(request, env);
    }

    if (url.pathname === "/run") {
      if (!tokenOk(request, env)) return new Response("forbidden", { status: 403 });
      const force = url.searchParams.get("force") === "1";
      // via=do передає роботу Durable Object у Варшаві. Це потрібно, коли воркер кличе
      // хтось із неєвропейської мережі (наприклад, сам бот з Орегона): інакше сторінки
      // качалися б з американського дата-центру, який сайт УЗ майже не пускає.
      const viaDo = url.searchParams.get("via") === "do" && Boolean(env.SCHEDULER);
      try {
        if (viaDo) {
          const stub = env.SCHEDULER.get(env.SCHEDULER.idFromName("merefa"), { locationHint: "eeur" });
          const r = await stub.fetch("https://scheduler/tick" + (force ? "?force=1" : ""));
          return new Response(r.body, { status: r.status, headers: { "Content-Type": "application/json" } });
        }
        return Response.json(await runUpdate(env, force));
      } catch (e) {
        return Response.json({ ok: false, error: String(e) }, { status: 500 });
      }
    }

    if (url.pathname !== "/fetch") {
      return withCors(new Response("not found", { status: 404 }));
    }

    const target = allowedTarget(url.searchParams.get("url"));
    if (!target) {
      return withCors(new Response("bad url or host not allowed", { status: 400 }));
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

  // Cron Trigger: питає бота, чи настав слот оновлення, і якщо так, збирає розклад.
  // FORCE_CRON=1 у змінних воркера змушує збирати одразу: так перевіряють цей шлях.
  //
  // Сам Cron Trigger виконується там, де вирішить Cloudflare (перевірено: Сінгапур), а
  // сайт УЗ звідти майже не відповідає. Тому робота передається Durable Object, який
  // прив'язаний до Європи через locationHint і виконується у Варшаві.
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      // Спершу канал УЗ: затримки цінні свіжими, і читає їх сам бот (Telegram, на відміну
      // від сайту УЗ, доступний з мережі Render), тож дата-центр воркера тут не важливий.
      try {
        const live = await askBot("/live", {}, env);
        console.log("канал УЗ:", live.status, live.message || "");
      } catch (e) {
        console.log("канал УЗ не оновлено:", String(e).slice(0, 120));
      }
      try {
        const force = env.FORCE_CRON === "1";
        if (!env.SCHEDULER) {
          console.log("оновлення без DO:", JSON.stringify(await runUpdate(env, force)));
          return;
        }
        const stub = env.SCHEDULER.get(env.SCHEDULER.idFromName("merefa"), { locationHint: "eeur" });
        const r = await stub.fetch("https://scheduler/tick" + (force ? "?force=1" : ""));
        console.log("оновлення через DO:", await r.text());
      } catch (e) {
        console.log("оновлення не вдалося:", String(e));
      }
    })());
  },
};

/**
 * Виконавець у Європі. Durable Object живе там, де його створили, а locationHint прив'язує
 * його до Східної Європи, тож запити до сайту УЗ ідуть з дата-центру, який сайт пускає.
 */
export class Scheduler {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request) {
    const force = new URL(request.url).searchParams.get("force") === "1";
    try {
      return Response.json(await runUpdate(this.env, force));
    } catch (e) {
      return Response.json({ ok: false, error: String(e) }, { status: 500 });
    }
  }
}
