/* ============================================================================
   Sabooq (سبوق) Full-Stack Platform Frontend
   Connected 100% to FastAPI + SQLite/PostgreSQL REST API & Real-time SSE
   ============================================================================ */

const STATE = {
  view: "home", // home | brand | track | customer | team | cashier | kitchen | manage | superadmin
  brands: [],
  brandCache: {},
  activeBrand: null, // { brand, branches, categories, products, qr_tags, payments }
  selectedBranchId: null,
  selectedCategoryId: "all",
  orderMode: "pickup", // table | drivethru | pickup | delivery
  tableNumber: "",
  carInfo: "",
  deliveryAddress: "",
  qrNfcCode: "",
  cart: [], // [{ product_id, name_ar, size_label, unit_price, quantity, notes }]
  customerToken: localStorage.getItem("sbq_cust_token") || "",
  customer: null,
  staffToken: localStorage.getItem("sbq_staff_token") || "",
  staff: null,
  activeOrderNumber: localStorage.getItem("sbq_last_order") || "",
  sse: null,
  manageTab: "branches",
  manageBrandId: null,
  opsFilter: "active",
  opsOrders: [],
  userCoords: null,
};

const ROLE_LABELS = {
  super_admin: "إدارة سبوق الرئيسية",
  brand_admin: "مدير براند",
  branch_manager: "مدير فرع",
  cashier: "كاشير",
  kitchen: "شاشة المطبخ",
  driver: "مندوب توصيل",
};

const STATUS_LABELS = {
  new: "طلب جديد",
  accepted: "تم القبول",
  preparing: "قيد التجهيز",
  ready: "جاهز للاستلام",
  out_for_delivery: "في الطريق",
  completed: "مكتمل",
  cancelled: "ملغي",
};

const MODE_LABELS = {
  table: "🪑 طاولة",
  drivethru: "🚗 درايف ثرو",
  pickup: "🛍️ استلام من الفرع",
  delivery: "🛵 توصيل",
};

const PAY_LABELS = {
  apple_pay: "Apple Pay",
  mada: "مدى (Mada)",
  card: "بطاقة ائتمانية",
  wallet: "محفظة سبوق",
  cash: "نقدي",
};

// ---------------------------------------------------------------------------
// API & Utilities
// ---------------------------------------------------------------------------

async function api(path, opts = {}, useStaff = false) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  const tok = useStaff ? STATE.staffToken : STATE.customerToken;
  if (tok) headers["Authorization"] = "Bearer " + tok;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    throw new Error(data.detail || `خطأ في الاتصال (${res.status})`);
  }
  return data;
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmt(n) {
  return Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
}

function toast(msg) {
  const el = document.getElementById("toastRoot");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
}

function playBeep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = "sine";
    o.frequency.value = 880;
    g.gain.value = 0.14;
    o.connect(g);
    g.connect(ctx.destination);
    o.start();
    o.stop(ctx.currentTime + 0.18);
  } catch (_) {}
}

function toggleTheme() {
  const root = document.documentElement;
  const next = root.dataset.theme === "light" ? "dark" : "light";
  root.dataset.theme = next;
  localStorage.setItem("sbq_theme", next);
  document.getElementById("themeBtn").textContent = next === "light" ? "🌙" : "☀️";
}

function applyBrandTheme(brand) {
  const r = document.documentElement.style;
  if (!brand) {
    ["--bg", "--bg2", "--card", "--tx", "--mut", "--ac", "--on"].forEach((k) => r.removeProperty(k));
    document.getElementById("hdrBrandTitle").textContent = "سبوق";
    return;
  }
  if (document.documentElement.dataset.theme !== "light") {
    r.setProperty("--bg", brand.theme_bg || "#04261d");
    r.setProperty("--bg2", brand.theme_bg2 || "#06342a");
    r.setProperty("--card", brand.theme_card || "#07382c");
    r.setProperty("--tx", brand.theme_tx || "#f4efdc");
    r.setProperty("--mut", brand.theme_mut || "#a8c9bd");
    r.setProperty("--ac", brand.theme_ac || "#4de3b6");
    r.setProperty("--on", brand.theme_on || "#03261c");
  }
  document.getElementById("hdrBrandTitle").textContent = `سبوق · ${brand.name_ar}`;
}

function closeSSE() {
  if (STATE.sse) {
    STATE.sse.close();
    STATE.sse = null;
  }
}

function openSSE(params, onOrderEvent) {
  closeSSE();
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v) qs.set(k, v); });
  const es = new EventSource(`/api/stream?${qs.toString()}`);
  es.addEventListener("order_created", (e) => {
    playBeep();
    onOrderEvent("created", JSON.parse(e.data));
  });
  es.addEventListener("order_updated", (e) => {
    onOrderEvent("updated", JSON.parse(e.data));
  });
  STATE.sse = es;
}

// Pure SVG deterministic QR visual matrix generator (zero external dependencies)
function renderQrSvg(text, size = 164) {
  const n = 21;
  let hash = 2166136261;
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  const cells = [];
  const isFinder = (r, c) =>
    (r < 7 && c < 7) || (r < 7 && c >= n - 7) || (r >= n - 7 && c < 7);
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) {
      let dark = false;
      if (isFinder(r, c)) {
        const lr = r < 7 ? r : r - (n - 7);
        const lc = c < 7 ? c : c - (n - 7);
        dark = lr === 0 || lr === 6 || lc === 0 || lc === 6 || (lr >= 2 && lr <= 4 && lc >= 2 && lc <= 4);
      } else {
        const v = Math.imul(hash ^ (r * 31 + c * 17 + text.charCodeAt((r + c) % text.length)), 2654435761);
        dark = ((v >>> 0) % 100) < 48;
      }
      if (dark) cells.push(`<rect x="${c}" y="${r}" width="1" height="1" fill="#04261d"/>`);
    }
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${n} ${n}" width="${size}" height="${size}" shape-rendering="crispEdges"><rect width="${n}" height="${n}" fill="#ffffff"/>${cells.join("")}</svg>`;
}

// ---------------------------------------------------------------------------
// Navigation & Header
// ---------------------------------------------------------------------------

function renderNav() {
  const nav = document.getElementById("mainNav");
  const btns = [
    `<button class="${STATE.view === "home" ? "active" : ""}" onclick="navigate('home')">🏠 الرئيسية</button>`,
    `<button class="${STATE.view === "customer" ? "active" : ""}" onclick="navigate('customer')">⭐ ${STATE.customer ? esc(STATE.customer.name) : "حسابي ونقاطي"}</button>`,
  ];
  if (STATE.activeOrderNumber) {
    btns.push(`<button class="${STATE.view === "track" ? "active" : ""}" onclick="navigate('track')">📦 تتبع طلبي</button>`);
  }
  if (STATE.staff) {
    if (STATE.staff.role === "super_admin") {
      btns.push(`<button class="${STATE.view === "superadmin" ? "active" : ""}" onclick="navigate('superadmin')">👑 لوحة سبوق</button>`);
    }
    if (["super_admin", "brand_admin", "branch_manager"].includes(STATE.staff.role)) {
      btns.push(`<button class="${STATE.view === "manage" ? "active" : ""}" onclick="navigate('manage')">⚙️ إدارة البراند</button>`);
    }
    if (["super_admin", "brand_admin", "branch_manager", "cashier"].includes(STATE.staff.role)) {
      btns.push(`<button class="${STATE.view === "cashier" ? "active" : ""}" onclick="navigate('cashier')">🧾 الكاشير</button>`);
    }
    if (["super_admin", "brand_admin", "branch_manager", "kitchen", "cashier"].includes(STATE.staff.role)) {
      btns.push(`<button class="${STATE.view === "kitchen" ? "active" : ""}" onclick="navigate('kitchen')">🍳 المطبخ</button>`);
    }
    btns.push(`<button onclick="staffLogout()">🚪 خروج (${esc(ROLE_LABELS[STATE.staff.role] || STATE.staff.role)})</button>`);
  } else {
    btns.push(`<button class="${STATE.view === "team" ? "active" : ""}" onclick="navigate('team')">🔐 دخول الفريق</button>`);
  }
  nav.innerHTML = btns.join("");
}

async function navigate(view, arg = null) {
  closeSSE();
  STATE.view = view;
  document.getElementById("cartBar").classList.add("hidden");
  if (view !== "brand") applyBrandTheme(null);
  renderNav();

  if (view === "home") await renderHome();
  else if (view === "brand") await renderBrandStorefront(arg || (STATE.activeBrand && STATE.activeBrand.brand.slug));
  else if (view === "track") await renderOrderTracking(arg || STATE.activeOrderNumber);
  else if (view === "customer") await renderCustomerView();
  else if (view === "team") renderTeamLoginView();
  else if (view === "cashier") await renderCashierView();
  else if (view === "kitchen") await renderKitchenView();
  else if (view === "manage") await renderBrandManageView(arg || STATE.manageBrandId || (STATE.staff && STATE.staff.brand_id) || (STATE.brands[0] && STATE.brands[0].id));
  else if (view === "superadmin") await renderSuperAdminView();
}

// ---------------------------------------------------------------------------
// 1. HOME PORTAL (Sabooq Main Directory)
// ---------------------------------------------------------------------------

async function renderHome(forceRefresh = false) {
  const root = document.getElementById("appRoot");
  if (!STATE.brands.length || forceRefresh) {
    const data = await api("/api/brands");
    STATE.brands = data.brands || [];
  }
  const totalBranches = STATE.brands.reduce((a, b) => a + (b.branches ? b.branches.length : 0), 0);
  const totalProducts = STATE.brands.reduce((a, b) => a + (b.products_count || 0), 0);

  root.innerHTML = `
    <section class="hero">
      <div class="wrap">
        <img class="big" src="/icon-192.png" alt="سبوق">
        <h1>سبوق — منصة الطلب والولاء الموحدة</h1>
        <p>اطلب من مقهاك ومطعمك المفضل، اجمع نقاط الولاء عبر جميع الفروع، واستلم طلبك من الطاولة أو السيارة أو التوصيل بكل سرعة.</p>
        <div class="cta">
          <button class="btn" onclick="document.getElementById('partnersSec').scrollIntoView({behavior:'smooth'})">🔍 اكتشف المحلات</button>
          <button class="btn o" onclick="openQrScannerModal()">📷 مسح QR / NFC</button>
          <button class="btn o" onclick="navigate('customer')">⭐ حسابي ونقاطي</button>
          <button class="btn o" onclick="navigate('${STATE.staff ? (STATE.staff.role === "super_admin" ? "superadmin" : "cashier") : "team"}')">🔐 لوحة الفريق والإدارة</button>
        </div>
        <div class="stats">
          <div><b>${STATE.brands.length}</b><span>براند مسجل</span></div>
          <div><b>${totalBranches}</b><span>فرع نشط</span></div>
          <div><b>${totalProducts}</b><span>منتج متاح للطلب</span></div>
        </div>
      </div>
    </section>

    <section class="s" id="partnersSec">
      <div class="wrap">
        <div class="between">
          <div>
            <h2>المحلات والمطاعم الشريكة</h2>
            <p class="sub">اختر البراند لتصفح القائمة والطلب الفوري — كل براند بهويته وألوانه المستقلة</p>
          </div>
          <button class="btn sm o" onclick="findNearestBranches()">📍 الأقرب لموقعي</button>
        </div>
        <input id="brandSearchInput" placeholder="ابحث باسم البراند أو التصنيف (مثال: حاف، برجر، قهوة، ماتشا)..." oninput="filterBrandsGrid(this.value)">
        <div class="grid" id="brandsGrid">${renderBrandsCards(STATE.brands)}</div>
      </div>
    </section>

    <section class="s">
      <div class="wrap">
        <div class="card">
          <h3 style="margin-top:0">📩 تواصل مع إدارة سبوق أو انضم كشريك</h3>
          <p class="sub">تصل رسالتك مباشرة إلى لوحة إدارة سبوق الرئيسية</p>
          <div class="grid-2">
            <div><label class="fl">الاسم</label><input id="tkName" placeholder="اسمك الكامل"></div>
            <div><label class="fl">رقم الجوال</label><input id="tkPhone" placeholder="05XXXXXXXX"></div>
          </div>
          <label class="fl">الموضوع</label>
          <select id="tkTopic">
            <option value="انضمام براند جديد">انضمام براند جديد</option>
            <option value="دعم فني للطلبات">دعم فني للطلبات</option>
            <option value="اقتراح أو ملاحظة">اقتراح أو ملاحظة</option>
          </select>
          <label class="fl">الرسالة</label>
          <textarea id="tkMsg" rows="3" placeholder="اكتب تفاصيل رسالتك..."></textarea>
          <button class="btn" onclick="submitSupportTicket()">إرسال إلى سبوق</button>
        </div>
      </div>
    </section>
  `;
}

function renderBrandsCards(list) {
  if (!list.length) return `<p class="mut">لا توجد نتائج مطابقة.</p>`;
  return list.map((b) => `
    <div class="bc" onclick="navigate('brand', '${esc(b.slug)}')">
      <div class="cov" style="background-image:url('${esc(b.cover_url)}'); background-color:${esc(b.theme_bg2)}">
        ${b.logo_url ? `<img class="brand-logo" src="${esc(b.logo_url)}" alt="${esc(b.name_ar)}">` : `<div class="brand-logo" style="display:flex;align-items:center;justify-content:center;font-weight:800;color:${esc(b.theme_ac)}">${esc(b.name_ar.slice(0,2))}</div>`}
      </div>
      <div class="body">
        <h3>
          <span>${esc(b.name_ar)} <small class="mut" style="font-size:13px">${esc(b.name_en)}</small></span>
          <span class="badge" style="color:${esc(b.theme_ac)};border-color:${esc(b.theme_ac)}">★ ${fmt(b.rating)}</span>
        </h3>
        <div class="meta">${esc(b.category_ar)} · ${(b.branches || []).length} فرع · ${b.products_count || 0} صنف</div>
        <div class="between" style="margin-top:auto">
          <span class="mut" style="font-size:12px">${b.branches && b.branches[0] ? esc(b.branches[0].name) : ""}</span>
          <span class="btn sm" style="background:${esc(b.theme_ac)};border-color:${esc(b.theme_ac)};color:${esc(b.theme_on)}">اطلب الآن</span>
        </div>
      </div>
    </div>
  `).join("");
}

function filterBrandsGrid(q) {
  const ql = (q || "").trim().toLowerCase();
  const filtered = STATE.brands.filter((b) =>
    `${b.name_ar} ${b.name_en} ${b.category_ar} ${b.slug}`.toLowerCase().includes(ql)
  );
  document.getElementById("brandsGrid").innerHTML = renderBrandsCards(filtered);
}

function findNearestBranches() {
  if (!navigator.geolocation) return toast("المتصفح لا يدعم تحديد الموقع");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      const { latitude, longitude } = pos.coords;
      const dist = (a, b) => Math.hypot(a - latitude, b - longitude);
      const sorted = [...STATE.brands].sort((x, y) => {
        const bx = (x.branches && x.branches[0]) || { lat: 24.46, lng: 39.61 };
        const by = (y.branches && y.branches[0]) || { lat: 24.46, lng: 39.61 };
        return dist(bx.lat, bx.lng) - dist(by.lat, by.lng);
      });
      document.getElementById("brandsGrid").innerHTML = renderBrandsCards(sorted);
      toast("تم ترتيب المحلات حسب الأقرب لموقعك 📍");
    },
    () => toast("تعذر الحصول على الموقع الجغرافي")
  );
}

async function submitSupportTicket() {
  const name = document.getElementById("tkName").value.trim();
  const phone = document.getElementById("tkPhone").value.trim();
  const topic = document.getElementById("tkTopic").value;
  const message = document.getElementById("tkMsg").value.trim();
  if (!name || !message) return toast("يرجى إدخال الاسم ونص الرسالة");
  try {
    await api("/api/support/tickets", { method: "POST", body: JSON.stringify({ name, phone, topic, message }) });
    toast("تم إرسال رسالتك بنجاح إلى إدارة سبوق ✅");
    document.getElementById("tkMsg").value = "";
  } catch (e) {
    toast(e.message);
  }
}

// ---------------------------------------------------------------------------
// 2. BRAND STOREFRONT, MENU, CART & CHECKOUT
// ---------------------------------------------------------------------------

async function renderBrandStorefront(slug) {
  const root = document.getElementById("appRoot");
  try {
    const data = STATE.brandCache[slug] || await api(`/api/brands/${encodeURIComponent(slug)}`);
    STATE.brandCache[slug] = data;
    STATE.activeBrand = data;
    applyBrandTheme(data.brand);
    if (!STATE.selectedBranchId || !data.branches.some((b) => b.id === STATE.selectedBranchId)) {
      STATE.selectedBranchId = data.branches[0] ? data.branches[0].id : null;
    }
    STATE.selectedCategoryId = "all";
    drawStorefront();
    updateCartBar();
  } catch (e) {
    root.innerHTML = `<div class="wrap"><div class="card"><p>${esc(e.message)}</p><button class="btn" onclick="navigate('home')">العودة للرئيسية</button></div></div>`;
  }
}

function drawStorefront() {
  const { brand, branches, categories, products } = STATE.activeBrand;
  const branch = branches.find((b) => b.id === STATE.selectedBranchId) || branches[0];
  const busyMap = {
    calm: { color: "#3fb36b", text: "الفرع هادي · تجهيز سريع" },
    medium: { color: "#e8b83a", text: "شبه مزدحم · وقت متوسط" },
    busy: { color: "#e0525f", text: "الفرع مزدحم حالياً" },
  };
  const busy = busyMap[(branch && branch.busy_level) || "calm"] || busyMap.calm;

  const filteredProds = products.filter((p) => {
    if (!p.is_available) return false;
    if (STATE.selectedCategoryId === "all") return true;
    return String(p.category_id) === String(STATE.selectedCategoryId);
  });

  document.getElementById("appRoot").innerHTML = `
    <div class="wrap">
      <div class="between" style="margin-top:14px">
        <button class="btn sm o" onclick="navigate('home')">→ العودة لجميع المحلات</button>
        ${STATE.qrNfcCode ? `<span class="badge">📡 متصل عبر ${esc(STATE.qrNfcCode)}</span>` : ""}
      </div>

      <div class="brand-hero" style="background-image:url('${esc(brand.cover_url)}')">
        <div class="brand-hero-inner">
          ${brand.logo_url ? `<img src="${esc(brand.logo_url)}" alt="${esc(brand.name_ar)}">` : ""}
          <div style="flex:1">
            <h1 style="margin:0;font-size:26px;color:#fff">${esc(brand.name_ar)} <small style="font-size:14px;opacity:.85">${esc(brand.name_en)}</small></h1>
            <div style="font-size:13px;color:#eee">${esc(brand.category_ar)} · ★ ${fmt(brand.rating)} (${brand.reviews_count}) · اكسب ${brand.loyalty_points_per_sar} نقطة لكل ريال</div>
          </div>
          <span class="badge" style="background:${busy.color}33;color:#fff;border-color:${busy.color}">${busy.text} (~${branch ? branch.prep_minutes : 10} د)</span>
        </div>
      </div>

      <div class="card">
        <div class="grid-2">
          <div>
            <label class="fl">اختر الفرع</label>
            <select onchange="STATE.selectedBranchId=Number(this.value);drawStorefront()">
              ${branches.map((br) => `<option value="${br.id}" ${br.id === (branch && branch.id) ? "selected" : ""}>${esc(br.name)} — ${br.is_open ? "مفتوح" : "مغلق"}</option>`).join("")}
            </select>
          </div>
          <div>
            <label class="fl">طريقة الاستلام</label>
            <div class="row">
              ${Object.entries(MODE_LABELS).map(([k, lbl]) => `
                <button class="tab ${STATE.orderMode === k ? "active" : ""}" onclick="STATE.orderMode='${k}';drawStorefront()">${lbl}</button>
              `).join("")}
            </div>
          </div>
        </div>
        ${STATE.orderMode === "table" ? `<div style="margin-top:8px"><label class="fl">رقم الطاولة</label><input value="${esc(STATE.tableNumber)}" oninput="STATE.tableNumber=this.value" placeholder="مثال: طاولة 5"></div>` : ""}
        ${STATE.orderMode === "drivethru" ? `<div style="margin-top:8px"><label class="fl">نوع ولون السيارة</label><input value="${esc(STATE.carInfo)}" oninput="STATE.carInfo=this.value" placeholder="مثال: كامري أبيض"></div>` : ""}
        ${STATE.orderMode === "delivery" ? `<div style="margin-top:8px"><label class="fl">عنوان التوصيل (رسوم التوصيل ${fmt(branch ? branch.delivery_fee : 12)} ر.س)</label><input value="${esc(STATE.deliveryAddress)}" oninput="STATE.deliveryAddress=this.value" placeholder="الحي، الشارع، رقم المبنى"></div>` : ""}
      </div>

      <div class="tabs">
        <button class="tab ${STATE.selectedCategoryId === "all" ? "active" : ""}" onclick="STATE.selectedCategoryId='all';drawStorefront()">الكل (${products.filter((p) => p.is_available).length})</button>
        ${categories.map((c) => `
          <button class="tab ${String(STATE.selectedCategoryId) === String(c.id) ? "active" : ""}" onclick="STATE.selectedCategoryId='${c.id}';drawStorefront()">${esc(c.name_ar)}</button>
        `).join("")}
      </div>

      <div class="prod-grid">
        ${filteredProds.map((p) => `
          <div class="pc">
            <div class="pimg">
              ${p.image_url ? `<img src="${esc(p.image_url)}" alt="${esc(p.name_ar)}" loading="lazy">` : `<div style="font-size:38px">☕</div>`}
              ${p.discount_percent ? `<span class="badge" style="position:absolute;top:10px;inset-inline-start:10px;background:var(--red);color:#fff;border:none">خصم ${p.discount_percent}%</span>` : ""}
            </div>
            <div class="pbody">
              <h4 class="pname">${esc(p.name_ar)}</h4>
              ${p.calories ? `<span class="mut" style="font-size:12px">🔥 ${p.calories} سعرة</span>` : ""}
              <div class="between" style="margin-top:auto;padding-top:6px">
                <span class="pprice">${fmt(p.price)} ر.س ${p.sizes && p.sizes.length ? '<small class="mut" style="font-size:11px">أحجام</small>' : ""}</span>
                <button class="btn sm" onclick="handleAddProduct(${p.id})">+ أضف</button>
              </div>
            </div>
          </div>
        `).join("")}
      </div>
    </div>
  `;
}

function handleAddProduct(prodId) {
  const prod = STATE.activeBrand.products.find((p) => p.id === prodId);
  if (!prod) return;
  if (prod.sizes && prod.sizes.length > 0) {
    openModal(
      `اختر الحجم — ${prod.name_ar}`,
      `<div class="grid-2">
        ${prod.sizes.map((sz, idx) => `
          <button class="card" style="cursor:pointer;text-align:center" onclick="addToCart(${prod.id}, ${idx});closeModal()">
            <b style="font-size:17px">${esc(sz.label)}</b><br>
            <span style="color:var(--ac);font-weight:800">${fmt(sz.price)} ر.س</span>
          </button>
        `).join("")}
      </div>`
    );
  } else {
    addToCart(prod.id, -1);
  }
}

function addToCart(prodId, sizeIdx) {
  const prod = STATE.activeBrand.products.find((p) => p.id === prodId);
  if (!prod) return;
  let sizeLabel = "";
  let unitPrice = Number(prod.price);
  if (sizeIdx >= 0 && prod.sizes && prod.sizes[sizeIdx]) {
    sizeLabel = prod.sizes[sizeIdx].label;
    unitPrice = Number(prod.sizes[sizeIdx].price);
  }
  if (prod.discount_percent > 0) {
    unitPrice = Math.round(unitPrice * (1 - prod.discount_percent / 100) * 100) / 100;
  }
  // Reset cart if switching brands
  if (STATE.cart.length && STATE.cart[0].brand_id !== STATE.activeBrand.brand.id) {
    STATE.cart = [];
  }
  const existing = STATE.cart.find((i) => i.product_id === prod.id && i.size_label === sizeLabel);
  if (existing) existing.quantity += 1;
  else {
    STATE.cart.push({
      brand_id: STATE.activeBrand.brand.id,
      product_id: prod.id,
      name_ar: prod.name_ar,
      size_label: sizeLabel,
      unit_price: unitPrice,
      quantity: 1,
      notes: "",
    });
  }
  updateCartBar();
  toast(`تمت إضافة "${prod.name_ar}" إلى السلة`);
}

function updateCartBar() {
  const bar = document.getElementById("cartBar");
  if (!STATE.cart.length || STATE.view !== "brand") {
    bar.classList.add("hidden");
    return;
  }
  const count = STATE.cart.reduce((a, i) => a + i.quantity, 0);
  const sum = STATE.cart.reduce((a, i) => a + i.unit_price * i.quantity, 0);
  bar.innerHTML = `<span>🛒 السلة (${count} صنف)</span><span>إتمام الطلب · ${fmt(sum)} ر.س ←</span>`;
  bar.classList.remove("hidden");
}

function openCartModal() {
  if (!STATE.cart.length) return toast("السلة فارغة");
  const { brand, branches, payments } = STATE.activeBrand;
  const branch = branches.find((b) => b.id === STATE.selectedBranchId) || branches[0];
  const subtotal = STATE.cart.reduce((a, i) => a + i.unit_price * i.quantity, 0);
  const deliveryFee = STATE.orderMode === "delivery" ? Number(branch.delivery_fee || 12) : 0;
  const total = subtotal + deliveryFee;

  openModal(
    `🛒 سلة المشتريات — ${brand.name_ar}`,
    `
    <div>
      ${STATE.cart.map((it, idx) => `
        <div class="between" style="padding:10px 0;border-bottom:1px solid var(--ln)">
          <div>
            <b>${esc(it.name_ar)}</b> ${it.size_label ? `<span class="badge">${esc(it.size_label)}</span>` : ""}
            <div class="mut" style="font-size:13px">${fmt(it.unit_price)} ر.س × ${it.quantity} = <b>${fmt(it.unit_price * it.quantity)} ر.س</b></div>
          </div>
          <div class="row">
            <button class="btn sm o" onclick="changeCartQty(${idx}, -1)">−</button>
            <b>${it.quantity}</b>
            <button class="btn sm o" onclick="changeCartQty(${idx}, 1)">+</button>
          </div>
        </div>
      `).join("")}

      <div style="margin-top:14px">
        <div class="grid-2">
          <div><label class="fl">الاسم</label><input id="ckName" value="${esc((STATE.customer && STATE.customer.name) || "")}" placeholder="اسم العميل"></div>
          <div><label class="fl">رقم الجوال (لتجميع النقاط)</label><input id="ckPhone" value="${esc((STATE.customer && STATE.customer.phone) || "")}" placeholder="05XXXXXXXX"></div>
        </div>

        <label class="fl">طريقة الدفع (البوابة مهيأة لمدى وApple Pay وميسر وتاب)</label>
        <select id="ckPay">
          ${(payments.supported_methods || []).map((m) => `<option value="${m.id}">${esc(m.label_ar)}</option>`).join("")}
        </select>

        ${STATE.customer && STATE.customer.total_points >= 10 ? `
          <label class="fl">استبدال نقاط الولاء (لديك ${STATE.customer.total_points} نقطة · كل 10 نقاط = 1 ر.س)</label>
          <input id="ckRedeem" type="number" min="0" max="${STATE.customer.total_points}" step="10" value="0">
        ` : `<input id="ckRedeem" type="hidden" value="0">`}

        <label class="fl">ملاحظات للمطبخ / الكاشير</label>
        <input id="ckNotes" placeholder="مثال: بدون سكر، زيادة صوص...">

        <div class="card" style="background:var(--bg2);margin-top:10px">
          <div class="between"><span>المجموع الفرعي</span><b>${fmt(subtotal)} ر.س</b></div>
          ${deliveryFee > 0 ? `<div class="between"><span>رسوم التوصيل</span><b>${fmt(deliveryFee)} ر.س</b></div>` : ""}
          <div class="between" style="font-size:18px;margin-top:6px;color:var(--ac)"><span>الإجمالي (شامل الضريبة)</span><b>${fmt(total)} ر.س</b></div>
        </div>

        <button class="btn" style="width:100%;padding:14px;font-size:16px" onclick="submitRealOrder()">✅ تأكيد وإرسال الطلب للكاشير والمطبخ</button>
      </div>
    </div>
    `
  );
}

function changeCartQty(idx, delta) {
  STATE.cart[idx].quantity += delta;
  if (STATE.cart[idx].quantity <= 0) STATE.cart.splice(idx, 1);
  updateCartBar();
  if (!STATE.cart.length) closeModal();
  else openCartModal();
}

async function submitRealOrder() {
  const { brand } = STATE.activeBrand;
  const payload = {
    brand_id: brand.id,
    branch_id: STATE.selectedBranchId,
    order_mode: STATE.orderMode,
    table_number: STATE.tableNumber,
    car_info: STATE.carInfo,
    delivery_address: STATE.deliveryAddress,
    qr_nfc_code: STATE.qrNfcCode,
    payment_method: document.getElementById("ckPay").value,
    customer_name: document.getElementById("ckName").value.trim(),
    customer_phone: document.getElementById("ckPhone").value.trim(),
    redeem_points: Number(document.getElementById("ckRedeem").value || 0),
    notes: document.getElementById("ckNotes").value.trim(),
    items: STATE.cart.map((i) => ({
      product_id: i.product_id,
      quantity: i.quantity,
      size_label: i.size_label,
      notes: i.notes,
    })),
  };
  try {
    const res = await api("/api/orders", { method: "POST", body: JSON.stringify(payload) });
    STATE.cart = [];
    STATE.activeOrderNumber = res.order.order_number;
    localStorage.setItem("sbq_last_order", res.order.order_number);
    closeModal();
    updateCartBar();
    toast(`تم إنشاء الطلب ${res.order.order_number} وإرساله مباشرة للكاشير والمطبخ 🎉`);
    if (STATE.customerToken) await refreshCustomerSession();
    navigate("track", res.order.order_number);
  } catch (e) {
    toast(e.message);
  }
}

// ---------------------------------------------------------------------------
// 3. LIVE ORDER TRACKING (Real-Time SSE)
// ---------------------------------------------------------------------------

async function renderOrderTracking(orderNumber) {
  const root = document.getElementById("appRoot");
  if (!orderNumber) {
    root.innerHTML = `<div class="wrap"><div class="card"><h3>تتبع الطلب</h3><input id="trSearch" placeholder="أدخل رقم الطلب مثال: SBQ-1001"><button class="btn" onclick="renderOrderTracking(document.getElementById('trSearch').value.trim())">تتبع</button></div></div>`;
    return;
  }
  try {
    const data = await api(`/api/orders/${encodeURIComponent(orderNumber)}`);
    drawOrderTrackCard(data.order);
    openSSE({ order_number: orderNumber }, (_ev, ord) => {
      if (ord.order_number === orderNumber) {
        drawOrderTrackCard(ord);
        toast(`تحديث مباشر للطلب ${ord.order_number}: ${STATUS_LABELS[ord.status] || ord.status}`);
      }
    });
  } catch (e) {
    root.innerHTML = `<div class="wrap"><div class="card"><p>${esc(e.message)}</p><button class="btn" onclick="navigate('home')">الرئيسية</button></div></div>`;
  }
}

function drawOrderTrackCard(o) {
  const stages = [
    { key: "new", icon: "📥", label: "استلام الطلب" },
    { key: "preparing", icon: "🔥", label: "قيد التجهيز" },
    { key: "ready", icon: "🛎️", label: "جاهز للاستلام" },
    { key: "completed", icon: "✅", label: "تم التسليم" },
  ];
  const rank = { new: 0, accepted: 0, preparing: 1, ready: 2, out_for_delivery: 2, completed: 3, cancelled: -1 };
  const curRank = rank[o.status] ?? 0;

  document.getElementById("appRoot").innerHTML = `
    <div class="wrap" style="padding-top:18px">
      <div class="card">
        <div class="between">
          <div>
            <h2 style="margin:0">طلب رقم ${esc(o.order_number)}</h2>
            <div class="mut">${esc(o.brand_name)} · ${esc(o.branch_name)} · ${MODE_LABELS[o.order_mode] || o.order_mode}</div>
          </div>
          <span class="badge ${esc(o.status)}">${STATUS_LABELS[o.status] || o.status}</span>
        </div>

        <div class="stepper">
          ${stages.map((st, idx) => `
            <div class="step-box ${curRank >= idx ? "done" : ""}">
              <b>${st.icon}</b>
              <span>${st.label}</span>
            </div>
          `).join("")}
        </div>

        <div class="card" style="background:var(--bg2)">
          <h4 style="margin:0 0 8px">🧾 تفاصيل الفاتورة</h4>
          ${(o.items || []).map((i) => `
            <div class="between" style="padding:4px 0">
              <span>${i.quantity} × ${esc(i.product_name)} ${i.size_label ? `(${esc(i.size_label)})` : ""}</span>
              <b>${fmt(i.line_total)} ر.س</b>
            </div>
          `).join("")}
          <hr style="border:0;border-top:1px solid var(--ln);margin:10px 0">
          <div class="between mut"><span>المجموع الفرعي</span><span>${fmt(o.subtotal)} ر.س</span></div>
          ${o.discount_amount > 0 ? `<div class="between" style="color:var(--ok)"><span>خصم نقاط الولاء (${o.points_redeemed} نقطة)</span><span>-${fmt(o.discount_amount)} ر.س</span></div>` : ""}
          ${o.delivery_fee > 0 ? `<div class="between mut"><span>رسوم التوصيل</span><span>${fmt(o.delivery_fee)} ر.س</span></div>` : ""}
          <div class="between mut"><span>ضريبة القيمة المضافة (15%)</span><span>${fmt(o.vat_amount)} ر.س</span></div>
          <div class="between" style="font-size:18px;margin-top:6px;color:var(--ac)"><span>الإجمالي</span><b>${fmt(o.total)} ر.س</b></div>
          <div class="between mut" style="margin-top:6px;font-size:13px">
            <span>الدفع: ${PAY_LABELS[o.payment_method] || o.payment_method} (${o.payment_status === "paid" ? "مدفوع ✅" : "بانتظار التحصيل"})</span>
            ${o.points_earned > 0 ? `<span style="color:var(--gd)">⭐ +${o.points_earned} نقطة ولاء</span>` : ""}
          </div>
        </div>

        ${!o.rating && o.status !== "cancelled" ? `
          <div class="card" style="background:var(--bg2)">
            <h4 style="margin:0 0 8px">⭐ قيّم تجربتك</h4>
            <div class="row" style="margin-bottom:8px">
              ${[5, 4, 3, 2, 1].map((s) => `<button class="btn sm o" onclick="submitOrderRating('${esc(o.order_number)}', ${s})">${s} ★</button>`).join("")}
            </div>
            <input id="rateComment" placeholder="ملاحظتك على الطلب (اختياري)">
          </div>
        ` : (o.rating ? `<p class="badge">شكراً لتقييمك: ${o.rating} ★</p>` : "")}

        <div class="row">
          <button class="btn" onclick="navigate('brand', '${esc(o.brand_slug)}')">اطلب مرة أخرى</button>
          ${o.status === "new" ? `<button class="btn danger" onclick="cancelMyOrder('${esc(o.order_number)}')">إلغاء الطلب</button>` : ""}
          <button class="btn o" onclick="navigate('home')">الرئيسية</button>
        </div>
      </div>
    </div>
  `;
}

async function submitOrderRating(orderNumber, stars) {
  const comment = (document.getElementById("rateComment") && document.getElementById("rateComment").value) || "";
  await api(`/api/orders/${encodeURIComponent(orderNumber)}/rate`, { method: "POST", body: JSON.stringify({ rating: stars, comment }) });
  toast("تم حفظ تقييمك بنجاح ⭐");
  renderOrderTracking(orderNumber);
}

async function cancelMyOrder(orderNumber) {
  try {
    await api(`/api/orders/${encodeURIComponent(orderNumber)}/cancel`, { method: "POST" });
    toast("تم إلغاء الطلب");
    renderOrderTracking(orderNumber);
  } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------------------
// 4. CUSTOMER ACCOUNT, LOYALTY POINTS, VISITS & WALLET
// ---------------------------------------------------------------------------

async function refreshCustomerSession() {
  if (!STATE.customerToken) { STATE.customer = null; return; }
  try {
    const data = await api("/api/customers/me");
    STATE.customer = data.customer;
    return data;
  } catch (_) {
    STATE.customerToken = "";
    localStorage.removeItem("sbq_cust_token");
    STATE.customer = null;
    return null;
  }
}

async function renderCustomerView() {
  const root = document.getElementById("appRoot");
  if (!STATE.customerToken) {
    root.innerHTML = `
      <div class="wrap" style="padding-top:24px;max-width:520px">
        <div class="card">
          <h2 style="margin-top:0">⭐ تسجيل دخول العميل (نظام الولاء والنقاط)</h2>
          <p class="sub">سجّل برقم جوالك لحفظ طلباتك وتجميع النقاط والزيارات في جميع براندات سبوق</p>
          <label class="fl">رقم الجوال</label>
          <input id="custPhone" value="0551234567" placeholder="05XXXXXXXX">
          <label class="fl">الاسم (للعملاء الجدد)</label>
          <input id="custName" placeholder="اسمك الكامل">
          <button class="btn" style="width:100%" onclick="requestCustOtp()">إرسال رمز التحقق</button>
          <div id="otpBox" class="hidden" style="margin-top:14px">
            <label class="fl">أدخل رمز التحقق (OTP)</label>
            <input id="custOtp" placeholder="4 أرقام">
            <button class="btn ok" style="width:100%" onclick="verifyCustOtp()">تأكيد الدخول</button>
          </div>
        </div>
      </div>
    `;
    return;
  }
  const data = await refreshCustomerSession();
  if (!data) return renderCustomerView();
  const c = data.customer;
  renderNav();

  root.innerHTML = `
    <div class="wrap" style="padding-top:20px">
      <div class="between" style="margin-bottom:14px">
        <div>
          <h2 style="margin:0">أهلاً ${esc(c.name)} 👋</h2>
          <div class="mut">${esc(c.phone)} · مستوى العضوية: <b style="color:var(--gd)">${esc(c.tier)}</b></div>
        </div>
        <div class="row">
          <button class="btn sm o" onclick="openQrScannerModal()">📷 تسجيل زيارة QR/NFC</button>
          <button class="btn sm danger" onclick="customerLogout()">تسجيل الخروج</button>
        </div>
      </div>

      <div class="stats" style="max-width:100%;margin-bottom:18px">
        <div><b>${fmt(c.total_points)}</b><span>إجمالي نقاط الولاء</span></div>
        <div><b>${fmt(c.total_visits)}</b><span>عدد الزيارات والطلبات</span></div>
        <div><b>${fmt(c.wallet_balance)} ر.س</b><span>رصيد محفظة سبوق</span></div>
        <div><b>${esc(c.tier)}</b><span>فئة العضوية</span></div>
      </div>

      <div class="grid-2">
        <div class="card">
          <h3 style="margin-top:0">💳 شحن محفظة سبوق</h3>
          <div class="row">
            <button class="btn sm" onclick="topupWallet(50)">+50 ر.س</button>
            <button class="btn sm" onclick="topupWallet(100)">+100 ر.س</button>
            <button class="btn sm" onclick="topupWallet(200)">+200 ر.س</button>
          </div>
        </div>
        <div class="card">
          <h3 style="margin-top:0">🏪 نقاطك وزياراتك حسب البراند</h3>
          ${data.brand_loyalty.length ? data.brand_loyalty.map((bl) => `
            <div class="between" style="padding:6px 0;border-bottom:1px solid var(--ln)">
              <b>${esc(bl.brand_name)}</b>
              <span>⭐ ${bl.points} نقطة · ${bl.visits} زيارة</span>
            </div>
          `).join("") : `<p class="mut">ابدأ أول طلب لتجميع النقاط في البراندات!</p>`}
        </div>
      </div>

      <div class="card">
        <h3 style="margin-top:0">📦 سجل طلباتي</h3>
        ${data.orders.length ? data.orders.map((o) => `
          <div class="between" style="padding:10px 0;border-bottom:1px solid var(--ln);cursor:pointer" onclick="navigate('track','${esc(o.order_number)}')">
            <div>
              <b>${esc(o.order_number)} — ${esc(o.brand_name)}</b>
              <div class="mut" style="font-size:13px">${esc(o.branch_name)} · ${fmt(o.total)} ر.س · ${MODE_LABELS[o.order_mode] || o.order_mode}</div>
            </div>
            <span class="badge ${esc(o.status)}">${STATUS_LABELS[o.status] || o.status}</span>
          </div>
        `).join("") : `<p class="mut">لا توجد طلبات سابقة بعد.</p>`}
      </div>
    </div>
  `;
}

async function requestCustOtp() {
  const phone = document.getElementById("custPhone").value.trim();
  const name = document.getElementById("custName").value.trim();
  try {
    const res = await api("/api/auth/customer/request-otp", { method: "POST", body: JSON.stringify({ phone, name }) });
    document.getElementById("otpBox").classList.remove("hidden");
    document.getElementById("custOtp").value = res.demo_otp;
    toast(res.message);
  } catch (e) { toast(e.message); }
}

async function verifyCustOtp() {
  const phone = document.getElementById("custPhone").value.trim();
  const code = document.getElementById("custOtp").value.trim();
  const name = document.getElementById("custName").value.trim();
  try {
    const res = await api("/api/auth/customer/verify-otp", { method: "POST", body: JSON.stringify({ phone, code, name }) });
    STATE.customerToken = res.token;
    STATE.customer = res.customer;
    localStorage.setItem("sbq_cust_token", res.token);
    toast(`أهلاً بك ${res.customer.name} 🎉`);
    renderCustomerView();
  } catch (e) { toast(e.message); }
}

async function topupWallet(amount) {
  try {
    await api("/api/customers/me/topup", { method: "POST", body: JSON.stringify({ amount }) });
    toast(`تم شحن المحفظة بمبلغ ${amount} ر.س ✅`);
    renderCustomerView();
  } catch (e) { toast(e.message); }
}

function customerLogout() {
  STATE.customerToken = "";
  STATE.customer = null;
  localStorage.removeItem("sbq_cust_token");
  renderNav();
  renderCustomerView();
}

// ---------------------------------------------------------------------------
// 5. TEAM LOGIN & RBAC PORTAL
// ---------------------------------------------------------------------------

function renderTeamLoginView() {
  document.getElementById("appRoot").innerHTML = `
    <div class="wrap" style="padding-top:24px;max-width:680px">
      <div class="card">
        <h2 style="margin-top:0">🔐 دخول فريق العمل والصلاحيات</h2>
        <p class="sub">كل حساب مربوط بصلاحيات منفصلة حسب الدور والبراند والفرع</p>
        <label class="fl">اسم المستخدم</label>
        <input id="stfUser" placeholder="مثال: 401 أو haaf_admin أو haaf_cashier أو haaf_kitchen">
        <label class="fl">رمز الدخول (PIN)</label>
        <input id="stfPin" type="password" placeholder="أدخل الرمز">
        <button class="btn" style="width:100%" onclick="doStaffLogin()">تسجيل الدخول</button>
      </div>

      <div class="card">
        <h4 style="margin-top:0">⚡ حسابات تجريبية جاهزة للاختبار السريع (اضغط للتعبئة)</h4>
        <div class="grid-2">
          <button class="btn sm o" onclick="fillStaff('401','0000')">👑 إدارة سبوق الرئيسية (401 / 0000)</button>
          <button class="btn sm o" onclick="fillStaff('haaf_admin','1111')">⚙️ مدير براند حاف (haaf_admin / 1111)</button>
          <button class="btn sm o" onclick="fillStaff('haaf_cashier','3333')">🧾 كاشير حاف (haaf_cashier / 3333)</button>
          <button class="btn sm o" onclick="fillStaff('haaf_kitchen','4444')">🍳 مطبخ حاف (haaf_kitchen / 4444)</button>
          <button class="btn sm o" onclick="fillStaff('chill_admin','1111')">🍔 مدير تشيل تشيل (chill_admin / 1111)</button>
          <button class="btn sm o" onclick="fillStaff('chill_cashier','3333')">🧾 كاشير تشيل تشيل (chill_cashier / 3333)</button>
        </div>
      </div>
    </div>
  `;
}

function fillStaff(u, p) {
  document.getElementById("stfUser").value = u;
  document.getElementById("stfPin").value = p;
}

async function doStaffLogin() {
  const username = document.getElementById("stfUser").value.trim();
  const pin = document.getElementById("stfPin").value.trim();
  try {
    const res = await api("/api/auth/staff/login", { method: "POST", body: JSON.stringify({ username, pin }) });
    STATE.staffToken = res.token;
    STATE.staff = res.staff;
    localStorage.setItem("sbq_staff_token", res.token);
    toast(`تم الدخول: ${res.staff.full_name} (${ROLE_LABELS[res.staff.role]})`);
    if (res.staff.role === "super_admin") navigate("superadmin");
    else if (res.staff.role === "kitchen") navigate("kitchen");
    else if (res.staff.role === "cashier") navigate("cashier");
    else navigate("manage", res.staff.brand_id);
  } catch (e) { toast(e.message); }
}

function staffLogout() {
  STATE.staffToken = "";
  STATE.staff = null;
  localStorage.removeItem("sbq_staff_token");
  navigate("home");
}

// ---------------------------------------------------------------------------
// 6. CASHIER POS PANEL (Real-Time SSE)
// ---------------------------------------------------------------------------

async function renderCashierView() {
  if (!STATE.staff) return navigate("team");
  const qs = new URLSearchParams({ status: STATE.opsFilter });
  if (STATE.staff.role === "super_admin" && STATE.manageBrandId) qs.set("brand_id", STATE.manageBrandId);
  const data = await api(`/api/ops/orders?${qs.toString()}`, {}, true);
  STATE.opsOrders = data.orders || [];
  drawCashierScreen();
  openSSE(
    { brand_id: STATE.staff.brand_id || "", branch_id: STATE.staff.branch_id || "" },
    async (ev, ord) => {
      if (ev === "created") toast(`🔔 طلب جديد وصل للكاشير: ${ord.order_number}`);
      const fresh = await api(`/api/ops/orders?${qs.toString()}`, {}, true);
      STATE.opsOrders = fresh.orders || [];
      drawCashierScreen();
    }
  );
}

function drawCashierScreen() {
  const s = STATE.staff;
  const scopeLabel = s.role === "super_admin" ? "جميع البراندات" : `${s.brand ? s.brand.name_ar : ""} ${s.branch ? "· " + s.branch.name : ""}`;
  document.getElementById("appRoot").innerHTML = `
    <div class="wrap wide" style="padding-top:18px">
      <div class="between" style="margin-bottom:12px">
        <div>
          <h2 style="margin:0">🧾 لوحة الكاشير المباشرة (POS)</h2>
          <div class="mut">${esc(s.full_name)} · النطاق: <b>${esc(scopeLabel)}</b> · <span style="color:var(--ok)">● متصل بث مباشر</span></div>
        </div>
        <div class="row">
          <button class="btn sm o" onclick="renderCashierView()">🔄 تحديث</button>
          <button class="btn sm" onclick="navigate('kitchen')">🍳 فتح شاشة المطبخ</button>
        </div>
      </div>

      <div class="tabs">
        ${[["active", "النشطة"], ["new", "جديد"], ["preparing", "قيد التجهيز"], ["ready", "جاهز"], ["completed", "مكتمل"], ["all", "الكل"]].map(([k, l]) => `
          <button class="tab ${STATE.opsFilter === k ? "active" : ""}" onclick="STATE.opsFilter='${k}';renderCashierView()">${l}</button>
        `).join("")}
      </div>

      <div class="kds-grid">
        ${STATE.opsOrders.length ? STATE.opsOrders.map((o) => `
          <div class="kds-ticket ${o.status === "new" ? "urgent" : (o.status === "ready" ? "ready-ticket" : "")}">
            <div class="between">
              <b style="font-size:18px">${esc(o.order_number)}</b>
              <span class="badge ${esc(o.status)}">${STATUS_LABELS[o.status] || o.status}</span>
            </div>
            <div class="mut" style="font-size:13px">
              ${esc(o.brand_name)} · ${esc(o.branch_name)}<br>
              ${MODE_LABELS[o.order_mode] || o.order_mode} ${o.table_number ? `(${esc(o.table_number)})` : ""} ${o.car_info ? `(${esc(o.car_info)})` : ""}<br>
              العميل: ${esc(o.customer_name)} ${o.customer_phone ? `· ${esc(o.customer_phone)}` : ""}
            </div>
            ${(o.items || []).map((i) => `
              <div class="kds-item-row">
                <span>${i.quantity} × ${esc(i.product_name)} ${i.size_label ? `<small class="mut">(${esc(i.size_label)})</small>` : ""}</span>
                <span>${fmt(i.line_total)} ر.س</span>
              </div>
            `).join("")}
            ${o.notes ? `<div class="badge warn">ملاحظة: ${esc(o.notes)}</div>` : ""}
            <div class="between" style="font-weight:800;color:var(--ac)">
              <span>الإجمالي: ${fmt(o.total)} ر.س</span>
              <span>${PAY_LABELS[o.payment_method] || o.payment_method} (${o.payment_status === "paid" ? "مدفوع ✅" : "تحصيل 💵"})</span>
            </div>
            <div class="row" style="margin-top:auto">
              ${o.status === "new" ? `<button class="btn sm" onclick="updateOrderStatus(${o.id}, {status:'accepted'})">قبول وإرسال للمطبخ</button>` : ""}
              ${["new", "accepted"].includes(o.status) ? `<button class="btn sm warn" onclick="updateOrderStatus(${o.id}, {status:'preparing', kitchen_status:'preparing'})">بدء التجهيز</button>` : ""}
              ${o.status === "preparing" ? `<button class="btn sm ok" onclick="updateOrderStatus(${o.id}, {status:'ready', kitchen_status:'ready'})">جاهز للاستلام</button>` : ""}
              ${["ready", "out_for_delivery"].includes(o.status) ? `<button class="btn sm ok" onclick="updateOrderStatus(${o.id}, {status:'completed'})">تم التسليم ✅</button>` : ""}
              ${o.status !== "completed" && o.status !== "cancelled" ? `<button class="btn sm danger" onclick="updateOrderStatus(${o.id}, {status:'cancelled'})">إلغاء</button>` : ""}
            </div>
          </div>
        `).join("") : `<div class="card"><p class="mut">لا توجد طلبات في هذا التصنيف حالياً. جرّب إنشاء طلب من واجهة العميل وسيظهر هنا فوراً!</p></div>`}
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// 7. KITCHEN DISPLAY SCREEN (KDS — Real-Time SSE)
// ---------------------------------------------------------------------------

async function renderKitchenView() {
  if (!STATE.staff) return navigate("team");
  const data = await api("/api/ops/orders?status=active", {}, true);
  STATE.opsOrders = (data.orders || []).filter((o) => ["new", "accepted", "preparing", "ready"].includes(o.status));
  drawKitchenScreen();
  openSSE(
    { brand_id: STATE.staff.brand_id || "", branch_id: STATE.staff.branch_id || "" },
    async (ev, ord) => {
      if (ev === "created") toast(`🔥 تذكرة مطبخ جديدة: ${ord.order_number}`);
      const fresh = await api("/api/ops/orders?status=active", {}, true);
      STATE.opsOrders = (fresh.orders || []).filter((o) => ["new", "accepted", "preparing", "ready"].includes(o.status));
      drawKitchenScreen();
    }
  );
}

function drawKitchenScreen() {
  const s = STATE.staff;
  document.getElementById("appRoot").innerHTML = `
    <div class="wrap wide" style="padding-top:18px">
      <div class="between" style="margin-bottom:14px">
        <div>
          <h2 style="margin:0">🍳 شاشة المطبخ المباشرة (KDS)</h2>
          <div class="mut">${esc(s.full_name)} · التذاكر النشطة: <b>${STATE.opsOrders.length}</b> · <span style="color:var(--ok)">● تحديث فوري</span></div>
        </div>
        <button class="btn sm o" onclick="renderKitchenView()">🔄 تحديث</button>
      </div>

      <div class="kds-grid">
        ${STATE.opsOrders.length ? STATE.opsOrders.map((o) => `
          <div class="kds-ticket ${o.kitchen_status === "pending" ? "urgent" : (o.kitchen_status === "ready" ? "ready-ticket" : "")}">
            <div class="between">
              <b style="font-size:20px">${esc(o.order_number)}</b>
              <span class="badge ${esc(o.status)}">${o.kitchen_status === "pending" ? "بانتظار التجهيز" : (o.kitchen_status === "preparing" ? "🔥 جاري التحضير" : "✅ جاهز")}</span>
            </div>
            <div class="mut" style="font-size:13px">${esc(o.brand_name)} · ${MODE_LABELS[o.order_mode] || o.order_mode} ${o.table_number ? `· طاولة ${esc(o.table_number)}` : ""}</div>
            ${(o.items || []).map((i) => `
              <div class="kds-item-row" style="font-size:16px">
                <span>${i.quantity} × ${esc(i.product_name)}</span>
                <span style="color:var(--ac)">${esc(i.size_label || "عادي")}</span>
              </div>
            `).join("")}
            ${o.notes ? `<div class="badge warn" style="font-size:13px">📝 ${esc(o.notes)}</div>` : ""}
            <div class="row" style="margin-top:auto">
              ${o.kitchen_status === "pending" ? `<button class="btn warn" style="flex:1" onclick="updateOrderStatus(${o.id}, {kitchen_status:'preparing'})">🔥 بدء التحضير</button>` : ""}
              ${o.kitchen_status !== "ready" ? `<button class="btn ok" style="flex:1" onclick="updateOrderStatus(${o.id}, {kitchen_status:'ready'})">✅ جاهز للتسليم</button>` : `<span class="badge ready" style="width:100%;justify-content:center;padding:8px">جاهز بانتظار الكاشير / العميل</span>`}
            </div>
          </div>
        `).join("") : `<div class="card"><p class="mut">المطبخ هادئ حالياً — لا توجد طلبات قيد التحضير.</p></div>`}
      </div>
    </div>
  `;
}

async function updateOrderStatus(orderId, patch) {
  try {
    await api(`/api/ops/orders/${orderId}/status`, { method: "PATCH", body: JSON.stringify(patch) }, true);
    toast("تم تحديث حالة الطلب ✅");
    if (STATE.view === "cashier") renderCashierView();
    else if (STATE.view === "kitchen") renderKitchenView();
  } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------------------
// 8. BRAND & BRANCH MANAGEMENT DASHBOARD (RBAC Scoped)
// ---------------------------------------------------------------------------

async function renderBrandManageView(brandId) {
  if (!STATE.staff) return navigate("team");
  STATE.manageBrandId = Number(brandId || STATE.staff.brand_id || 1);
  try {
    const data = await api(`/api/manage/brands/${STATE.manageBrandId}/dashboard`, {}, true);
    drawBrandManageDashboard(data);
  } catch (e) {
    document.getElementById("appRoot").innerHTML = `<div class="wrap"><div class="card"><p>${esc(e.message)}</p></div></div>`;
  }
}

function drawBrandManageDashboard(d) {
  const { brand, kpi, branches, categories, products, staff, qr_tags, customers } = d;
  const tab = STATE.manageTab;

  let bodyHtml = "";
  if (tab === "branches") {
    bodyHtml = `
      <div class="between" style="margin-bottom:10px">
        <h3 style="margin:0">🏢 إدارة الفروع (${branches.length})</h3>
        ${["super_admin", "brand_admin"].includes(STATE.staff.role) ? `<button class="btn sm" onclick="openAddBranchModal(${brand.id})">+ إضافة فرع جديد</button>` : ""}
      </div>
      <div class="grid-2">
        ${branches.map((br) => `
          <div class="card">
            <div class="between">
              <b>${esc(br.name)}</b>
              <span class="badge ${br.is_open ? "ready" : "cancelled"}">${br.is_open ? "مفتوح" : "مغلق"}</span>
            </div>
            <p class="mut" style="font-size:13px">${esc(br.address)} · ${esc(br.phone)} · التوصيل ${fmt(br.delivery_fee)} ر.س</p>
            <div class="row">
              <button class="btn sm o" onclick="toggleBranchOpen(${br.id}, ${br.is_open ? 0 : 1})">${br.is_open ? "إغلاق الفرع" : "فتح الفرع"}</button>
              <select style="width:auto;margin:0" onchange="setBranchBusy(${br.id}, this.value)">
                <option value="calm" ${br.busy_level === "calm" ? "selected" : ""}>🟢 هادي</option>
                <option value="medium" ${br.busy_level === "medium" ? "selected" : ""}>🟡 شبه مزدحم</option>
                <option value="busy" ${br.busy_level === "busy" ? "selected" : ""}>🔴 مزدحم</option>
              </select>
            </div>
          </div>
        `).join("")}
      </div>
    `;
  } else if (tab === "products") {
    bodyHtml = `
      <div class="between" style="margin-bottom:10px">
        <h3 style="margin:0">🍽️ المنتجات والتصنيفات (${products.length} منتج · ${categories.length} تصنيف)</h3>
        <div class="row">
          <button class="btn sm o" onclick="openAddCategoryModal(${brand.id})">+ تصنيف جديد</button>
          <button class="btn sm" onclick="openAddProductModal(${brand.id}, ${JSON.stringify(categories).replace(/"/g, "&quot;")})">+ إضافة منتج</button>
        </div>
      </div>
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>المنتج</th><th>السعر</th><th>الحالة</th><th>إجراءات</th></tr></thead>
        <tbody>
          ${products.map((p) => `
            <tr>
              <td><b>${esc(p.name_ar)}</b> ${p.calories ? `<small class="mut">(${p.calories} سعرة)</small>` : ""}</td>
              <td><b>${fmt(p.price)} ر.س</b></td>
              <td><span class="badge ${p.is_available ? "ready" : "cancelled"}">${p.is_available ? "متوفر" : "نفذ"}</span></td>
              <td class="row">
                <button class="btn sm o" onclick="quickEditPrice(${p.id}, ${p.price})">تعديل السعر</button>
                <button class="btn sm o" onclick="toggleProdAvail(${p.id}, ${p.is_available ? 0 : 1})">${p.is_available ? "إخفاء" : "تفعيل"}</button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table></div>
    `;
  } else if (tab === "staff") {
    bodyHtml = `
      <div class="between" style="margin-bottom:10px">
        <h3 style="margin:0">👥 الموظفين والكاشير والصلاحيات (${staff.length})</h3>
        <button class="btn sm" onclick="openAddStaffModal(${brand.id}, ${JSON.stringify(branches).replace(/"/g, "&quot;")})">+ إضافة موظف</button>
      </div>
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>الاسم</th><th>اسم المستخدم</th><th>الدور</th><th>الفرع</th><th>الحالة</th></tr></thead>
        <tbody>
          ${staff.map((st) => `
            <tr>
              <td><b>${esc(st.full_name)}</b></td>
              <td><code>${esc(st.username)}</code></td>
              <td><span class="badge">${ROLE_LABELS[st.role] || st.role}</span></td>
              <td>${esc(st.branch_name || "جميع الفروع")}</td>
              <td><button class="btn sm o" onclick="toggleStaffActive(${st.id}, ${st.is_active ? 0 : 1})">${st.is_active ? "نشط ✅" : "موقوف ⛔"}</button></td>
            </tr>
          `).join("")}
        </tbody>
      </table></div>
    `;
  } else if (tab === "qr") {
    bodyHtml = `
      <div class="between" style="margin-bottom:10px">
        <h3 style="margin:0">📡 رموز QR و NFC (${qr_tags.length})</h3>
        <button class="btn sm" onclick="openAddQrModal(${brand.id}, ${JSON.stringify(branches).replace(/"/g, "&quot;")})">+ إنشاء رمز QR/NFC</button>
      </div>
      <div class="grid-2">
        ${qr_tags.map((t) => {
          const url = `${location.origin}/t/${t.code}`;
          return `
            <div class="card">
              <div class="row" style="align-items:flex-start">
                <div class="qr-box">${renderQrSvg(url, 110)}</div>
                <div style="flex:1">
                  <b>${esc(t.label)}</b>
                  <div class="mut" style="font-size:13px">${esc(t.branch_name)} · النوع: ${esc(t.tag_type.toUpperCase())}</div>
                  <div style="margin:6px 0"><code>${esc(t.code)}</code> · المسحات: <b>${t.scan_count}</b></div>
                  <div class="row">
                    <button class="btn sm" onclick="simulateQrScan('${esc(t.code)}')">تجربة المسح</button>
                    <button class="btn sm o" onclick="writeNfcTag('${esc(url)}')">برمجة NFC</button>
                  </div>
                </div>
              </div>
            </div>
          `;
        }).join("")}
      </div>
    `;
  } else if (tab === "customers") {
    bodyHtml = `
      <h3 style="margin-top:0">⭐ عملاء البراند والولاء (${customers.length})</h3>
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>العميل</th><th>الجوال</th><th>الفئة</th><th>النقاط</th><th>الزيارات</th><th>الإنفاق</th><th>مكافأة</th></tr></thead>
        <tbody>
          ${customers.map((c) => `
            <tr>
              <td><b>${esc(c.name)}</b></td>
              <td>${esc(c.phone)}</td>
              <td><span class="badge">${esc(c.tier)}</span></td>
              <td><b>${c.total_points}</b></td>
              <td>${c.total_visits}</td>
              <td>${fmt(c.brand_spend)} ر.س</td>
              <td><button class="btn sm o" onclick="grantBonus(${brand.id}, ${c.id})">+50 نقطة</button></td>
            </tr>
          `).join("")}
        </tbody>
      </table></div>
    `;
  }

  document.getElementById("appRoot").innerHTML = `
    <div class="wrap wide" style="padding-top:18px">
      <div class="between" style="margin-bottom:14px">
        <div>
          <h2 style="margin:0">⚙️ لوحة إدارة براند: ${esc(brand.name_ar)}</h2>
          <div class="mut">صلاحيتك: <b>${ROLE_LABELS[STATE.staff.role]}</b> · <a href="#" style="color:var(--ac)" onclick="navigate('brand','${esc(brand.slug)}');return false">فتح متجر البراند ↗</a></div>
        </div>
        <div class="row">
          <button class="btn sm o" onclick="navigate('cashier')">🧾 الكاشير</button>
          <button class="btn sm o" onclick="navigate('kitchen')">🍳 المطبخ</button>
        </div>
      </div>

      <div class="stats" style="max-width:100%;margin-bottom:16px">
        <div><b>${fmt(kpi.total_sales)} ر.س</b><span>إجمالي المبيعات</span></div>
        <div><b>${kpi.orders_count}</b><span>عدد الطلبات</span></div>
        <div><b>${fmt(kpi.avg_ticket)} ر.س</b><span>متوسط الفاتورة</span></div>
        <div><b>${kpi.active_orders || 0}</b><span>طلبات نشطة الآن</span></div>
      </div>

      <div class="tabs">
        ${[["branches", "🏢 الفروع"], ["products", "🍽️ المنتجات والأسعار"], ["staff", "👥 الموظفين والصلاحيات"], ["qr", "📡 QR و NFC"], ["customers", "⭐ العملاء والنقاط"]].map(([k, l]) => `
          <button class="tab ${tab === k ? "active" : ""}" onclick="STATE.manageTab='${k}';renderBrandManageView(${brand.id})">${l}</button>
        `).join("")}
      </div>

      <div class="card">${bodyHtml}</div>
    </div>
  `;
}

// Brand Management Actions
function openAddBranchModal(brandId) {
  openModal("إضافة فرع جديد", `
    <label class="fl">اسم الفرع</label><input id="nbName" placeholder="مثال: فرع طريق الملك عبدالعزيز">
    <label class="fl">العنوان</label><input id="nbAddr" placeholder="الحي، الشارع">
    <label class="fl">رقم الهاتف</label><input id="nbPhone" placeholder="05XXXXXXXX">
    <label class="fl">رسوم التوصيل (ر.س)</label><input id="nbFee" type="number" value="12">
    <button class="btn" style="width:100%" onclick="saveNewBranch(${brandId})">حفظ الفرع</button>
  `);
}
async function saveNewBranch(brandId) {
  await api(`/api/manage/brands/${brandId}/branches`, {
    method: "POST",
    body: JSON.stringify({
      name: document.getElementById("nbName").value,
      address: document.getElementById("nbAddr").value,
      phone: document.getElementById("nbPhone").value,
      delivery_fee: Number(document.getElementById("nbFee").value || 12),
    }),
  }, true);
  closeModal(); toast("تمت إضافة الفرع بنجاح ✅"); renderBrandManageView(brandId);
}
async function toggleBranchOpen(branchId, isOpen) {
  await api(`/api/manage/branches/${branchId}`, { method: "PATCH", body: JSON.stringify({ is_open: isOpen }) }, true);
  toast("تم تحديث حالة الفرع"); renderBrandManageView(STATE.manageBrandId);
}
async function setBranchBusy(branchId, busy) {
  await api(`/api/manage/branches/${branchId}`, { method: "PATCH", body: JSON.stringify({ busy_level: busy }) }, true);
  toast("تم تحديث مستوى الازدحام");
}
function openAddCategoryModal(brandId) {
  openModal("إضافة تصنيف جديد", `
    <label class="fl">اسم التصنيف</label><input id="ncName" placeholder="مثال: مشروبات باردة">
    <label class="fl">رمز مختصر بالإنجليزي</label><input id="ncCode" placeholder="cold_drinks">
    <button class="btn" style="width:100%" onclick="saveNewCategory(${brandId})">حفظ التصنيف</button>
  `);
}
async function saveNewCategory(brandId) {
  await api(`/api/manage/brands/${brandId}/categories`, {
    method: "POST",
    body: JSON.stringify({ name_ar: document.getElementById("ncName").value, code: document.getElementById("ncCode").value }),
  }, true);
  closeModal(); toast("تمت إضافة التصنيف ✅"); renderBrandManageView(brandId);
}
function openAddProductModal(brandId, cats) {
  openModal("إضافة منتج جديد", `
    <label class="fl">التصنيف</label>
    <select id="npCat">${cats.map((c) => `<option value="${c.id}">${esc(c.name_ar)}</option>`).join("")}</select>
    <label class="fl">اسم المنتج</label><input id="npName" placeholder="مثال: سبانش لاتيه مثلج">
    <label class="fl">السعر (ر.س)</label><input id="npPrice" type="number" step="0.5" value="18">
    <label class="fl">السعرات الحرارية</label><input id="npCal" type="number" value="180">
    <button class="btn" style="width:100%" onclick="saveNewProduct(${brandId})">حفظ المنتج</button>
  `);
}
async function saveNewProduct(brandId) {
  await api(`/api/manage/brands/${brandId}/products`, {
    method: "POST",
    body: JSON.stringify({
      category_id: Number(document.getElementById("npCat").value) || null,
      name_ar: document.getElementById("npName").value,
      price: Number(document.getElementById("npPrice").value || 0),
      calories: Number(document.getElementById("npCal").value || 0),
    }),
  }, true);
  closeModal(); toast("تمت إضافة المنتج بنجاح ✅"); renderBrandManageView(brandId);
}
async function quickEditPrice(prodId, oldPrice) {
  const val = prompt("أدخل السعر الجديد (ر.س):", oldPrice);
  if (val === null || isNaN(Number(val))) return;
  await api(`/api/manage/products/${prodId}`, { method: "PATCH", body: JSON.stringify({ price: Number(val) }) }, true);
  toast("تم تحديث السعر ✅"); renderBrandManageView(STATE.manageBrandId);
}
async function toggleProdAvail(prodId, avail) {
  await api(`/api/manage/products/${prodId}`, { method: "PATCH", body: JSON.stringify({ is_available: avail }) }, true);
  toast("تم تحديث توفر المنتج"); renderBrandManageView(STATE.manageBrandId);
}
function openAddStaffModal(brandId, branches) {
  openModal("إضافة موظف / كاشير جديد", `
    <label class="fl">الاسم الكامل</label><input id="nsName" placeholder="مثال: أحمد الكاشير">
    <label class="fl">اسم المستخدم</label><input id="nsUser" placeholder="ahmed_cashier">
    <label class="fl">رمز الدخول (PIN)</label><input id="nsPin" value="3333">
    <label class="fl">الدور الوظيفي</label>
    <select id="nsRole">
      <option value="cashier">كاشير</option>
      <option value="kitchen">شاشة المطبخ</option>
      <option value="branch_manager">مدير فرع</option>
      <option value="brand_admin">مدير براند</option>
      <option value="driver">مندوب توصيل</option>
    </select>
    <label class="fl">الفرع</label>
    <select id="nsBranch">
      <option value="">جميع فروع البراند</option>
      ${branches.map((b) => `<option value="${b.id}">${esc(b.name)}</option>`).join("")}
    </select>
    <button class="btn" style="width:100%" onclick="saveNewStaff(${brandId})">حفظ الموظف</button>
  `);
}
async function saveNewStaff(brandId) {
  try {
    await api(`/api/manage/brands/${brandId}/staff`, {
      method: "POST",
      body: JSON.stringify({
        full_name: document.getElementById("nsName").value,
        username: document.getElementById("nsUser").value,
        pin: document.getElementById("nsPin").value,
        role: document.getElementById("nsRole").value,
        branch_id: Number(document.getElementById("nsBranch").value) || null,
      }),
    }, true);
    closeModal(); toast("تمت إضافة الموظف بنجاح ✅"); renderBrandManageView(brandId);
  } catch (e) { toast(e.message); }
}
async function toggleStaffActive(staffId, active) {
  await api(`/api/manage/staff/${staffId}`, { method: "PATCH", body: JSON.stringify({ is_active: active }) }, true);
  toast("تم تحديث حالة الموظف"); renderBrandManageView(STATE.manageBrandId);
}
function openAddQrModal(brandId, branches) {
  openModal("إنشاء رمز QR / NFC جديد", `
    <label class="fl">الفرع</label>
    <select id="nqBranch">${branches.map((b) => `<option value="${b.id}">${esc(b.name)}</option>`).join("")}</select>
    <label class="fl">التسمية</label><input id="nqLabel" placeholder="مثال: طاولة VIP 3">
    <label class="fl">الغرض</label>
    <select id="nqPurpose">
      <option value="table_order">طلب من الطاولة</option>
      <option value="branch_checkin">تسجيل حضور ونقاط ولاء (NFC/QR)</option>
      <option value="drivethru">طلب درايف ثرو</option>
    </select>
    <label class="fl">رقم الطاولة (اختياري)</label><input id="nqTable" placeholder="3">
    <button class="btn" style="width:100%" onclick="saveNewQr(${brandId})">إنشاء الرمز</button>
  `);
}
async function saveNewQr(brandId) {
  await api(`/api/manage/brands/${brandId}/qr-tags`, {
    method: "POST",
    body: JSON.stringify({
      branch_id: Number(document.getElementById("nqBranch").value),
      label: document.getElementById("nqLabel").value,
      purpose: document.getElementById("nqPurpose").value,
      table_number: document.getElementById("nqTable").value,
      tag_type: "both",
    }),
  }, true);
  closeModal(); toast("تم إنشاء رمز QR/NFC بنجاح ✅"); renderBrandManageView(brandId);
}
async function grantBonus(brandId, customerId) {
  await api(`/api/manage/brands/${brandId}/customers/${customerId}/bonus`, { method: "POST", body: JSON.stringify({ points: 50, note: "مكافأة ولاء من الإدارة" }) }, true);
  toast("تم منح العميل 50 نقطة إضافية ⭐"); renderBrandManageView(brandId);
}

// ---------------------------------------------------------------------------
// 9. SABOOQ SUPER ADMIN DASHBOARD (Multi-Brand Control)
// ---------------------------------------------------------------------------

async function renderSuperAdminView() {
  if (!STATE.staff || STATE.staff.role !== "super_admin") return navigate("team");
  const data = await api("/api/admin/overview", {}, true);
  const t = data.totals;

  document.getElementById("appRoot").innerHTML = `
    <div class="wrap wide" style="padding-top:18px">
      <div class="between" style="margin-bottom:14px">
        <div>
          <h2 style="margin:0">👑 لوحة إدارة منصة سبوق الرئيسية</h2>
          <div class="mut">التحكم المركزي بالبراندات، الفروع، الطلبات، الإيرادات، وتذاكر الدعم</div>
        </div>
        <button class="btn" onclick="openCreateBrandModal()">+ إنشاء براند جديد</button>
      </div>

      <div class="stats" style="max-width:100%;margin-bottom:18px">
        <div><b>${fmt(t.total_revenue)} ر.س</b><span>إجمالي مبيعات المنصة</span></div>
        <div><b>${t.total_orders}</b><span>إجمالي الطلبات</span></div>
        <div><b>${t.active_brands} / ${t.total_brands}</b><span>البراندات النشطة</span></div>
        <div><b>${t.total_branches}</b><span>إجمالي الفروع</span></div>
        <div><b>${t.total_products}</b><span>المنتجات</span></div>
        <div><b>${t.total_customers}</b><span>العملاء المسجلين</span></div>
      </div>

      <div class="card">
        <h3 style="margin-top:0">🏪 جميع البراندات المسجلة</h3>
        <div class="tbl-wrap"><table class="tbl">
          <thead><tr><th>البراند</th><th>التصنيف</th><th>الفروع</th><th>المنتجات</th><th>الطلبات</th><th>المبيعات</th><th>الحالة</th><th>إدارة</th></tr></thead>
          <tbody>
            ${data.brands.map((b) => `
              <tr>
                <td><b>${esc(b.name_ar)}</b> <small class="mut">(${esc(b.slug)})</small></td>
                <td>${esc(b.category_ar)}</td>
                <td>${b.branches_count}</td>
                <td>${b.products_count}</td>
                <td>${b.orders_count}</td>
                <td><b>${fmt(b.revenue)} ر.س</b></td>
                <td><span class="badge ${b.is_active ? "ready" : "cancelled"}">${b.is_active ? "نشط" : "موقوف"}</span></td>
                <td class="row">
                  <button class="btn sm" onclick="navigate('manage', ${b.id})">إدارة</button>
                  <button class="btn sm o" onclick="navigate('brand', '${esc(b.slug)}')">المتجر</button>
                  <button class="btn sm o" onclick="toggleBrandActive(${b.id}, ${b.is_active ? 0 : 1})">${b.is_active ? "إيقاف" : "تفعيل"}</button>
                </td>
              </tr>
            `).join("")}
          </tbody>
        </table></div>
      </div>

      <div class="grid-2">
        <div class="card">
          <h3 style="margin-top:0">📦 أحدث الطلبات عبر المنصة</h3>
          ${data.recent_orders.length ? data.recent_orders.map((o) => `
            <div class="between" style="padding:8px 0;border-bottom:1px solid var(--ln)">
              <div><b>${esc(o.order_number)}</b> · ${esc(o.brand_name)} <small class="mut">(${esc(o.customer_name)})</small></div>
              <div><b>${fmt(o.total)} ر.س</b> <span class="badge ${esc(o.status)}">${STATUS_LABELS[o.status]}</span></div>
            </div>
          `).join("") : `<p class="mut">لا توجد طلبات بعد.</p>`}
        </div>
        <div class="card">
          <h3 style="margin-top:0">📩 تذاكر الدعم (${t.open_tickets} مفتوحة)</h3>
          ${data.tickets.length ? data.tickets.map((tk) => `
            <div class="between" style="padding:8px 0;border-bottom:1px solid var(--ln)">
              <div><b>${esc(tk.name)}</b> (${esc(tk.topic)})<div class="mut" style="font-size:13px">${esc(tk.message)}</div></div>
              ${tk.status === "open" ? `<button class="btn sm o" onclick="closeTicket(${tk.id})">إغلاق</button>` : `<span class="badge completed">مغلق</span>`}
            </div>
          `).join("") : `<p class="mut">لا توجد تذاكر دعم حالياً.</p>`}
        </div>
      </div>
    </div>
  `;
}

function openCreateBrandModal() {
  openModal("➕ إنشاء براند جديد على منصة سبوق", `
    <div class="grid-2">
      <div><label class="fl">اسم البراند (عربي)</label><input id="cbAr" placeholder="مثال: محمصة نجد"></div>
      <div><label class="fl">الاسم (إنجليزي)</label><input id="cbEn" placeholder="Najd Roastery"></div>
    </div>
    <div class="grid-2">
      <div><label class="fl">المعرف المختصر (slug)</label><input id="cbSlug" placeholder="najd"></div>
      <div><label class="fl">التصنيف</label><input id="cbCat" value="قهوة مختصة وحلويات"></div>
    </div>
    <div class="grid-2">
      <div><label class="fl">لون الهوية الأساسي (Accent)</label><input id="cbAc" type="color" value="#4de3b6"></div>
      <div><label class="fl">لون الخلفية (BG)</label><input id="cbBg" type="color" value="#04261d"></div>
    </div>
    <div class="grid-2">
      <div><label class="fl">اسم الفرع الأول</label><input id="cbBr" value="الفرع الرئيسي"></div>
      <div><label class="fl">رمز دخول مدير البراند (PIN)</label><input id="cbPin" value="1111"></div>
    </div>
    <button class="btn" style="width:100%" onclick="saveNewBrand()">إنشاء البراند وحسابات فريقه تلقائياً</button>
  `);
}

async function saveNewBrand() {
  try {
    const res = await api("/api/admin/brands", {
      method: "POST",
      body: JSON.stringify({
        name_ar: document.getElementById("cbAr").value,
        name_en: document.getElementById("cbEn").value,
        slug: document.getElementById("cbSlug").value,
        category_ar: document.getElementById("cbCat").value,
        theme_ac: document.getElementById("cbAc").value,
        theme_bg: document.getElementById("cbBg").value,
        first_branch_name: document.getElementById("cbBr").value,
        admin_pin: document.getElementById("cbPin").value,
      }),
    }, true);
    closeModal();
    toast(`تم إنشاء براند "${res.brand.name_ar}" وحسابات الإدارة والكاشير والمطبخ بنجاح 🎉`);
    renderSuperAdminView();
  } catch (e) { toast(e.message); }
}

async function toggleBrandActive(brandId, isActive) {
  await api(`/api/admin/brands/${brandId}`, { method: "PATCH", body: JSON.stringify({ is_active: isActive }) }, true);
  toast("تم تحديث حالة البراند");
  renderSuperAdminView();
}

async function closeTicket(ticketId) {
  await api(`/api/admin/tickets/${ticketId}`, { method: "PATCH" }, true);
  toast("تم إغلاق التذكرة");
  renderSuperAdminView();
}

// ---------------------------------------------------------------------------
// 10. QR & NFC SCANNER / WRITER
// ---------------------------------------------------------------------------

function openQrScannerModal() {
  openModal("📷 مسح رمز QR أو بطاقة NFC", `
    <p class="sub">أدخل رمز الطاولة أو الفرع، أو استخدم قارئ NFC على الجوال مباشرة</p>
    <label class="fl">رمز QR / NFC</label>
    <input id="qrManualInput" placeholder="مثال: SBQ-HAAF-B1-T1 أو SBQ-HAAF-B1-NFC">
    <div class="row" style="margin-bottom:12px">
      <button class="btn" onclick="simulateQrScan(document.getElementById('qrManualInput').value.trim())">تفعيل الرمز</button>
      <button class="btn o" onclick="startWebNfcScan()">📡 قراءة بطاقة NFC</button>
    </div>
    <div class="mut" style="font-size:13px">رموز تجريبية سريعة:
      <a href="#" style="color:var(--ac)" onclick="simulateQrScan('SBQ-HAAF-B1-T1');return false">طاولة 1 (حاف)</a> ·
      <a href="#" style="color:var(--ac)" onclick="simulateQrScan('SBQ-HAAF-B1-NFC');return false">ولاء NFC (حاف)</a>
    </div>
  `);
}

async function simulateQrScan(code) {
  if (!code) return toast("أدخل رمز QR أو NFC");
  try {
    const tag = await api(`/api/qr/${encodeURIComponent(code)}`);
    closeModal();
    STATE.qrNfcCode = tag.code;
    STATE.selectedBranchId = tag.branch_id;
    if (tag.purpose === "table_order") {
      STATE.orderMode = "table";
      STATE.tableNumber = tag.table_number || "1";
    } else if (tag.purpose === "drivethru") {
      STATE.orderMode = "drivethru";
    }
    if (tag.checkin_awarded) {
      toast(`🎉 تم تسجيل حضورك عبر ${tag.label} وإضافة +${tag.bonus_points} نقطة ولاء!`);
    } else {
      toast(`📡 تم التعرف على ${tag.label} — ${tag.brand_name}`);
    }
    navigate("brand", tag.brand_slug);
  } catch (e) { toast(e.message); }
}

async function startWebNfcScan() {
  if (!("NDEFReader" in window)) {
    return toast("قراءة NFC المباشرة مدعومة على متصفح Chrome في أجهزة أندرويد (يمكنك إدخال الرمز يدوياً هنا)");
  }
  try {
    const ndef = new NDEFReader();
    await ndef.scan();
    toast("قرّب جوالك من بطاقة NFC الآن... 📡");
    ndef.onreading = (event) => {
      for (const rec of event.message.records) {
        const text = new TextDecoder().decode(rec.data);
        const match = text.match(/SBQ-[A-Z0-9-]+/i);
        if (match) simulateQrScan(match[0]);
      }
    };
  } catch (e) { toast("تعذر تفعيل قارئ NFC: " + e.message); }
}

async function writeNfcTag(url) {
  if (!("NDEFReader" in window)) {
    navigator.clipboard && navigator.clipboard.writeText(url);
    return toast("تم نسخ رابط الرمز (برمجة NFC المباشرة متاحة عبر Chrome على أندرويد)");
  }
  try {
    const ndef = new NDEFReader();
    await ndef.write({ records: [{ recordType: "url", data: url }] });
    toast("تمت برمجة بطاقة NFC بنجاح ✅");
  } catch (e) { toast("تعذر الكتابة على بطاقة NFC: " + e.message); }
}

// ---------------------------------------------------------------------------
// Modal Helpers & Bootstrap
// ---------------------------------------------------------------------------

function openModal(title, html) {
  document.getElementById("modalRoot").innerHTML = `
    <div class="modal-bg" onclick="if(event.target===this)closeModal()">
      <div class="modal">
        <div class="modal-hd">
          <h3>${esc(title)}</h3>
          <button class="icon-btn" onclick="closeModal()">✕</button>
        </div>
        ${html}
      </div>
    </div>
  `;
}

function closeModal() {
  document.getElementById("modalRoot").innerHTML = "";
}

async function bootstrap() {
  const savedTheme = localStorage.getItem("sbq_theme");
  if (savedTheme) {
    document.documentElement.dataset.theme = savedTheme;
    document.getElementById("themeBtn").textContent = savedTheme === "light" ? "🌙" : "☀️";
  }
  if (STATE.customerToken) await refreshCustomerSession();
  if (STATE.staffToken) {
    try {
      const s = await api("/api/auth/staff/me", {}, true);
      STATE.staff = s.staff;
    } catch (_) {
      STATE.staffToken = "";
      localStorage.removeItem("sbq_staff_token");
    }
  }

  // Handle QR/NFC deep-link URL params (?brand=haaf&branch=1&tag=SBQ-HAAF-B1-T1)
  const qs = new URLSearchParams(location.search);
  const tag = qs.get("tag");
  const brand = qs.get("brand");
  const branch = qs.get("branch");
  if (tag) {
    await simulateQrScan(tag);
  } else if (brand) {
    if (branch) STATE.selectedBranchId = Number(branch);
    await navigate("brand", brand);
  } else {
    await navigate("home");
  }
}

bootstrap();
