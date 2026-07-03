// proxy على Vercel — دالة catch-all تمرّر الطلبات إلى منصّات التداول التجريبية.
// تعمل في منطقة فرانكفورت (fra1) — راجع vercel.json — فيخرج الطلب بعنوان ألماني
// غير محجوب، بينما يبقى GitHub Actions أمريكياً. تمرير شفّاف: لا تغيّر التوقيع.
//
// مساران مستقلّان (يُختاران بالبادئة):
//   /api/*      → Bybit   (BYBIT_BASE_URL   = https://<proj>.vercel.app/api)
//   /binance/*  → Binance (BINANCE_BASE_URL = https://<proj>.vercel.app/binance)
//
// أداة تعليمية على حسابات افتراضية. ليست نصيحة مالية.

// لا تدع Vercel يفكّ الجسم (نحتاج البايتات الخام كما هي ليبقى التوقيع صحيحاً).
module.exports.config = { api: { bodyParser: false } };

// نطاقات Bybit للحساب التجريبي (Demo أولاً ثم Testnet الكلاسيكي كبديل).
const BYBIT_UPSTREAMS = [
  "https://api-demo.bybit.com",
  "https://api-testnet.bybit.com",
];

// نطاق Binance Spot Demo Mode (يتضمّن بادئة /api؛ نضيف /v3/... بعده).
const BINANCE_UPSTREAMS = [
  "https://demo-api.binance.com/api",
];

// قراءة الجسم الخام من دفق الطلب.
async function readRawBody(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(typeof chunk === "string" ? Buffer.from(chunk) : chunk);
  }
  return Buffer.concat(chunks);
}

module.exports = async function handler(req, res) {
  try {
    // اختيار المنصّة بالبادئة، ثم إزالة البادئة من المسار.
    let path = req.url || "/";
    let upstreams = BYBIT_UPSTREAMS;
    if (path.startsWith("/binance/")) {
      upstreams = BINANCE_UPSTREAMS;
      path = path.slice(8);                 // "/binance/v3/.." → "/v3/.."
    } else if (path === "/binance") {
      upstreams = BINANCE_UPSTREAMS;
      path = "/";
    } else if (path.startsWith("/api/")) {
      path = path.slice(4);                 // "/api/v5/.." → "/v5/.."
    } else if (path === "/api") {
      path = "/";
    }

    // إزالة معامل "path" الذي يحقنه rewrite في vercel.json — وإلا يُضاف إلى
    // سلسلة الاستعلام فيفسد التوقيع (Bybit 10004 / Binance -1022). نحافظ على
    // ترتيب بقية المعاملات كما هي حرفياً ليبقى التوقيع صحيحاً.
    const _qi = path.indexOf("?");
    if (_qi !== -1) {
      const _qs = path
        .slice(_qi + 1)
        .split("&")
        .filter((kv) => kv && kv.split("=")[0] !== "path")
        .join("&");
      path = path.slice(0, _qi) + (_qs ? "?" + _qs : "");
    }

    // الجسم الخام (لطلبات POST). GET بلا جسم.
    let body;
    const method = (req.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      body = await readRawBody(req);
      if ((!body || body.length === 0) && req.body != null) {
        body = Buffer.isBuffer(req.body)
          ? req.body
          : typeof req.body === "string"
          ? Buffer.from(req.body)
          : Buffer.from(JSON.stringify(req.body));
      }
    }

    // نمرّر فقط ترويسات التوقيع اللازمة (Bybit: x-bapi-*، Binance: x-mbx-apikey).
    const fwd = {};
    for (const [k, v] of Object.entries(req.headers || {})) {
      const kl = k.toLowerCase();
      if (kl.startsWith("x-bapi-") || kl.startsWith("x-mbx-") || kl === "content-type")
        fwd[kl] = v;
    }

    let lastErr;
    for (const base of upstreams) {
      try {
        const upstream = await fetch(base + path, { method, headers: fwd, body });
        const buf = Buffer.from(await upstream.arrayBuffer());
        res.status(upstream.status);
        res.setHeader(
          "content-type",
          upstream.headers.get("content-type") || "application/json"
        );
        res.setHeader("x-proxy-upstream", base);
        res.setHeader("x-proxy-region", process.env.VERCEL_REGION || "fra1");
        return res.send(buf);
      } catch (e) {
        lastErr = e;
      }
    }

    return res
      .status(502)
      .json({ retCode: -1, retMsg: "proxy_upstream_failed: " + String(lastErr) });
  } catch (e) {
    return res
      .status(500)
      .json({ retCode: -1, retMsg: "proxy_error: " + String(e) });
  }
};
