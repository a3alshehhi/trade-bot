// bybit-proxy على Vercel — دالة catch-all تمرّر كل الطلبات إلى Bybit Testnet.
// تعمل في منطقة فرانكفورت (fra1) — راجع vercel.json — فيخرج الطلب بعنوان ألماني
// غير محجوب، بينما يبقى GitHub Actions أمريكياً. تمرير شفّاف: لا تغيّر التوقيع.
//
// كيف يستدعيها البوت: BYBIT_BASE_URL = https://<اسم-المشروع>.vercel.app/api
// فيصبح مثلاً  /api/v5/market/time  →  https://api-testnet.bybit.com/v5/market/time
//
// أداة تعليمية على حساب افتراضي (Testnet). ليست نصيحة مالية.

// لا تدع Vercel يفكّ الجسم (نحتاج البايتات الخام كما هي ليبقى توقيع Bybit صحيحاً).
module.exports.config = { api: { bodyParser: false } };

// نطاقات Bybit التجريبية (نجرّب الأول ثم البديل).
const UPSTREAMS = [
  "https://api-testnet.bybit.com",
  "https://api-testnet.bytick.com",
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
    // المسار الأصلي مع الاستعلام، بعد إزالة بادئة /api.
    let path = req.url || "/";
    if (path.startsWith("/api/")) path = path.slice(4);      // يبقي "/v5/..."
    else if (path === "/api") path = "/";

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

    // نمرّر فقط ترويسات Bybit اللازمة (توقيع v5) ونوع المحتوى.
    const fwd = {};
    for (const [k, v] of Object.entries(req.headers || {})) {
      const kl = k.toLowerCase();
      if (kl.startsWith("x-bapi-") || kl === "content-type") fwd[kl] = v;
    }

    let lastErr;
    for (const base of UPSTREAMS) {
      try {
        const upstream = await fetch(base + path, {
          method,
          headers: fwd,
          body,
        });
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
