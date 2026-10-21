"use strict";
const $ = (selector) => document.querySelector(selector);
const state = {
  user: null,
  products: [],
  cart: [],
  coupons: [],
  orders: [],
  tasks: new Map(),
  category: "Все товары",
  view: "catalog",
  adminTab: "system",
  admin: null,
  stream: null,
  register: false,
  pendingProduct: null,
  checkoutKey: null,
  appliedCoupon: null,
  couponRequest: 0,
};
const statusNames = {
  pending: "Ожидает отправки",
  queued: "В очереди",
  running: "Выполняется",
  completed: "Завершён",
  error: "Ошибка",
};
const loadNames = { light: "Лёгкая", medium: "Средняя", heavy: "Тяжёлая", mixed: "Смешанная" };
const opNames = {
  sum: "Сумма чека",
  sort: "Ранжирование",
  hash: "SHA-256",
  count_words: "Частотный анализ",
  basket_affinity: "Совместные покупки",
  fib: "Фибоначчи",
  factorial: "Факториал",
};
const money = (value) =>
  new Intl.NumberFormat("ru-BY", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(
    value,
  );
const date = (stamp) =>
  new Date(stamp * 1000).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const badge = (status) => el("span", `badge ${status}`, statusNames[status] || status);
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($("#toast").hidden = true), 4000);
}
async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers:
      body === undefined
        ? {}
        : { "Content-Type": "application/json", "X-BasketNet-Client": "browser" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Не удалось выполнить запрос");
  return data;
}
function clickButton(text, cls, action) {
  const b = el("button", cls, text);
  b.type = "button";
  b.addEventListener("click", () => Promise.resolve(action()).catch((e) => toast(e.message)));
  return b;
}
function empty(text) {
  return el("p", "empty", text);
}
function priceOf(p) {
  return (p.cents - Math.round((p.cents * p.discount) / 100)) / 100;
}
function setUser(user) {
  state.user = user;
  $("#login-button").textContent = user ? user.login : "Войти";
  $("#logout-button").hidden = !user;
  $("#orders-nav").hidden = !user;
  $("#admin-nav").hidden = user?.role !== "admin";
}
async function refreshAccount() {
  if (!state.user) {
    clearCoupon();
    state.cart = [];
    state.coupons = [];
    state.orders = [];
  } else {
    [state.cart, state.coupons, state.orders] = await Promise.all([
      api("/api/cart"),
      api("/api/coupons"),
      api("/api/orders"),
    ]);
  }
  renderCart();
  renderOrders();
}
function showView(view) {
  if (view !== "catalog" && !state.user) {
    openAuth();
    return;
  }
  if (view === "admin" && state.user.role !== "admin") return;
  state.view = view;
  for (const name of ["catalog", "cart", "orders", "admin"])
    $(`#${name}-view`).hidden = name !== view;
  document
    .querySelectorAll("[data-view]")
    .forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  if (view === "admin") refreshAdmin().catch((e) => toast(e.message));
  if (view === "cart" || view === "orders") refreshAccount().catch((e) => toast(e.message));
  window.scrollTo({ top: 0 });
}
function renderCatalog() {
  const categories = [
    "Все товары",
    ...new Set(state.products.map((p) => p.category)),
    "Со скидкой",
  ];
  $("#categories").replaceChildren(
    ...categories.map((name) =>
      clickButton(name, `chip ${state.category === name ? "active" : ""}`, () => {
        state.category = name;
        renderCatalog();
      }),
    ),
  );
  const search = $("#search").value.trim().toLocaleLowerCase();
  let products = state.products.filter(
    (p) =>
      (state.category === "Все товары" ||
        (state.category === "Со скидкой" ? p.discount > 0 : p.category === state.category)) &&
      p.name.toLocaleLowerCase().includes(search),
  );
  const sort = $("#sort").value;
  if (sort === "price") products.sort((a, b) => priceOf(a) - priceOf(b));
  if (sort === "expensive") products.sort((a, b) => priceOf(b) - priceOf(a));
  if (sort === "discount") products.sort((a, b) => b.discount - a.discount);
  $("#catalog-empty").hidden = products.length > 0;
  $("#catalog").replaceChildren(
    ...products.map((p) => {
      const card = el("article", "product-card"),
        art = el("div", "product-art", p.icon);
      art.setAttribute("aria-hidden", "true");
      if (p.discount) art.append(el("span", "sale-tag", `-${p.discount}%`));
      const copy = el("div", "product-copy");
      copy.append(
        el("div", "product-unit", p.unit),
        el("h3", "", p.name),
        el("p", "product-description", p.description),
      );
      const bottom = el("div", "product-bottom"),
        price = el("div", "price", money(priceOf(p)) + " ");
      price.append(el("small", "", "BYN"));
      if (p.discount) price.append(el("span", "old-price", money(p.cents / 100)));
      const button = clickButton("+", "add-button", () => addProduct(p.id));
      button.setAttribute("aria-label", `Добавить ${p.name} в корзину`);
      bottom.append(price, button);
      copy.append(bottom);
      card.append(art, copy);
      return card;
    }),
  );
}
async function addProduct(id) {
  if (!state.user) {
    state.pendingProduct = id;
    openAuth();
    return;
  }
  const old = state.cart.find((p) => p.id === id);
  state.cart = await api("/api/cart", { product_id: id, quantity: (old?.quantity || 0) + 1 });
  state.checkoutKey = null;
  renderCart();
  toast("Товар добавлен в корзину");
}
function renderCart() {
  $("#cart-count").textContent = state.cart.reduce((sum, p) => sum + p.quantity, 0);
  $("#personal-coupons").replaceChildren(...state.coupons.filter(c => c.available).map(c =>
    clickButton(`${c.code} · -${c.percent}%`, "chip", async () => {
      $("#coupon").value = c.code;
      await applyCoupon();
    })));
  $("#cart-items").replaceChildren(
    ...(state.cart.length
      ? state.cart.map((p) => {
          const row = el("div", "cart-row"),
            info = el("div", "cart-info");
          info.append(
            el("h3", "", p.name),
            el("small", "", `${p.unit}${p.active ? "" : " · Сейчас недоступен"}`),
          );
          const qty = el("div", "quantity");
          const change = async (delta) => {
            state.cart = await api("/api/cart", { product_id: p.id, quantity: p.quantity + delta });
            state.checkoutKey = null;
            renderCart();
          };
          const minus = clickButton("−", "", () => change(-1)),
            plus = clickButton("+", "", () => change(1));
          minus.setAttribute("aria-label", `Убрать один ${p.name}`);
          plus.setAttribute("aria-label", `Добавить один ${p.name}`);
          qty.append(minus, el("span", "", p.quantity), plus);
          row.append(
            el("span", "cart-icon", p.icon),
            info,
            qty,
            el(
              "span",
              "price",
              money(
                (p.cents * p.quantity - Math.round((p.cents * p.quantity * p.discount) / 100)) /
                  100,
              ),
            ),
          );
          return row;
        })
      : [empty("В корзине пока пусто. Загляните в каталог.")]),
  );
  renderTotals();
}
function renderTotals() {
  let gross = 0,
    discount = 0;
  for (const p of state.cart) {
    gross += p.cents * p.quantity;
    discount += Math.round((p.cents * p.quantity * p.discount) / 100);
  }
  const coupon = state.appliedCoupon,
    couponDiscount = Math.round(((gross - discount) * (coupon?.percent || 0)) / 100);
  $("#cart-totals").replaceChildren(
    ...[
      ["Товары", gross],
      ["Скидки на товары", -discount],
      ["Купон", -couponDiscount],
      ["Итого", gross - discount - couponDiscount],
    ].map(([name, value], index) => {
      const line = el("div", `total-line ${index === 3 ? "grand" : ""}`);
      line.append(el("span", "", name), el("span", "", money(value / 100) + " BYN"));
      return line;
    }),
  );
  const entered = $("#coupon").value.trim().toUpperCase();
  $("#checkout").disabled = !state.cart.length || state.cart.some((p) => !p.active) ||
    (!!entered && entered !== state.appliedCoupon?.code);
}

function clearCoupon() {
  state.couponRequest++;
  state.appliedCoupon = null;
  state.checkoutKey = null;
  $("#coupon").value = "";
  $("#coupon-message").textContent = "";
  $("#remove-coupon").hidden = true;
}

async function applyCoupon() {
  const code = $("#coupon").value.trim().toUpperCase();
  const request = ++state.couponRequest;
  state.appliedCoupon = null;
  state.checkoutKey = null;
  renderTotals();
  $("#coupon-message").className = "small muted";
  $("#coupon-message").textContent = "Проверяем купон…";
  $("#remove-coupon").hidden = !code;
  try {
    const coupon = await api("/api/coupons/check", {code});
    if (request !== state.couponRequest) return;
    state.appliedCoupon = coupon;
    $("#coupon").value = coupon.code;
    $("#coupon-message").className = "small receipt-discounts";
    $("#coupon-message").textContent = `Купон применён: скидка ${coupon.percent}%${coupon.expires_on ? " · по " + coupon.expires_on.split("-").reverse().join(".") : ""}`;
  } catch (error) {
    if (request !== state.couponRequest) return;
    $("#coupon-message").className = "small form-error";
    $("#coupon-message").textContent = error.message;
  }
  renderTotals();
}
function download(name, data) {
  const link = document.createElement("a"),
    url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
    );
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function orderCard(o, admin = false) {
  const card = el("article", "panel"),
    head = el("div", "order-head"),
    title = el("div");
  title.append(
    el("h3", "", `Заказ #${o.id.slice(0, 8)}`),
    el(
      "span",
      "muted",
      `${date(o.created_at)}${admin ? " · " + o.login + (o.batch_id ? " · Тестовый" : "") : ""}`,
    ),
  );
  head.append(title, badge(o.status));
  const items = el("div", "order-items");
  for (const p of o.items) {
    const line = el("div");
    line.append(
      el("span", "", `${p.name} × ${p.quantity}`),
      el("span", "", money(p.price * p.quantity) + " BYN"),
    );
    items.append(line);
  }
  card.append(head, items);
  if (o.error) card.append(el("p", "form-error", o.error));
  if (o.result) {
    const line = el("div", "order-footer"),
      summary = el("div");
    summary.append(
      el("strong", "", `Итого ${money(o.result.total)} BYN`),
      el(
        "div",
        "receipt-discounts small",
        `Скидка на товары ${money(o.result.item_discount)} + купон ${money(o.result.order_discount)} BYN`,
      ),
    );
    line.append(summary);
    if (o.status === "completed")
      line.append(
        clickButton("Скачать чек ↓", "button secondary", () =>
          download(`order-${o.id.slice(0, 8)}.json`, {
            id: o.id,
            created_at: o.created_at,
            items: o.items,
            coupon: o.coupon,
            totals: o.result,
            sha256: o.digest,
            fiscal_data: o.receipt_text,
          }),
        ),
      );
    card.append(line);
  } else
    card.append(
      el(
        "p",
        "muted small",
        o.status === "error" ? "Расчёт не завершён." : "Рассчитываем итоговую сумму и готовим чек.",
      ),
    );
  if (o.digest) card.append(el("p", "digest", `SHA-256 чека: ${o.digest}`));
  if (admin) {
    const details = el("details");
    details.append(
      el(
        "summary",
        "",
        `События обработки · приоритет ${o.priority} · ${loadNames[o.load]} нагрузка`,
      ),
    );
    details.append(
      el(
        "pre",
        "",
        o.history
          .map(
            (e) =>
              `${date(e.timestamp)} | ${e.phase} | ${statusNames[e.status]} | ${e.worker_id || "очередь"} | попытка ${e.attempt}${e.reason ? " | " + e.reason : ""}`,
          )
          .join("\n") || "Ожидает отправки брокеру",
      ),
    );
    card.append(details);
  }
  return card;
}
function renderOrders() {
  const coupons = state.coupons.map((c) =>
    el("span", "chip", `${c.code} · ${c.percent}%${c.available ? "" : " · " + c.reason}`),
  );
  $("#my-coupons").replaceChildren(...coupons);
  $("#orders-list").replaceChildren(
    ...(state.orders.length
      ? state.orders.map((o) => orderCard(o))
      : [empty("Здесь появится ваш первый заказ.")]),
  );
}
function connectEvents() {
  state.stream?.close();
  if (!state.user) return;
  const stream = new EventSource("/api/events");
  state.stream = stream;
  stream.onopen = () => {
    $("#event-status").textContent = "Статусы обновляются";
  };
  stream.onerror = () => {
    $("#event-status").textContent = "Восстанавливаем связь";
  };
  stream.addEventListener("orders", (e) => {
    state.orders = JSON.parse(e.data);
    renderOrders();
  });
  stream.addEventListener("snapshot", (e) => {
    for (const record of JSON.parse(e.data)) state.tasks.set(record.snapshot.task_id, record);
    renderTasks();
  });
  stream.addEventListener("task", (e) => {
    const record = JSON.parse(e.data);
    state.tasks.set(record.snapshot.task_id, record);
    renderTasks();
  });
}
function openAuth() {
  state.register = false;
  renderAuth();
  $("#auth-dialog").showModal();
}
function renderAuth() {
  const form = $("#auth-form");
  form.querySelector(".form-error").textContent = "";
  $("#auth-title").textContent = state.register ? "Давайте знакомиться" : "Рады видеть вас";
  $("#auth-submit").textContent = state.register ? "Создать аккаунт" : "Войти";
  $("#auth-switch").textContent = state.register
    ? "Уже есть аккаунт? Войти"
    : "Нет аккаунта? Зарегистрироваться";
  form.elements.password.autocomplete = state.register ? "new-password" : "current-password";
  form.elements.password.minLength = state.register ? 8 : 1;
}
async function loadSession() {
  const session = await api("/api/session");
  setUser(session.user);
  state.samples = session.samples;
  state.descriptions = session.descriptions;
  state.tasks.clear();
  for (const t of session.tasks) state.tasks.set(t.snapshot.task_id, t);
  $("#operation").replaceChildren(
    ...session.operations.map((op) => new Option(`${opNames[op]} (${op})`, op)),
  );
  if (session.operations.length) setOperation();
  await refreshAccount();
  connectEvents();
}
function setOperation() {
  const op = $("#operation").value;
  $("#task-data").value = JSON.stringify(state.samples[op], null, 2);
  $("#operation-description").textContent = state.descriptions[op] || "";
}
function table(headers, rows) {
  const t = el("table"),
    thead = el("thead"),
    tr = el("tr"),
    body = el("tbody");
  headers.forEach((h) => tr.append(el("th", "", h)));
  thead.append(tr);
  for (const row of rows) {
    const r = el("tr");
    row.forEach((value) => {
      const cell = el("td");
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value;
      r.append(cell);
    });
    body.append(r);
  }
  t.append(thead, body);
  return t;
}
function times(o) {
  let wait = 0,
    run = 0,
    queued = null,
    running = null;
  for (const e of o.history) {
    if (e.status === "queued") {
      if (running !== null) run += e.timestamp - running;
      running = null;
      queued = e.timestamp;
    }
    if (e.status === "running") {
      if (queued !== null) wait += e.timestamp - queued;
      queued = null;
      running = e.timestamp;
    }
    if (e.status === "completed" || e.status === "error") {
      if (running !== null) run += e.timestamp - running;
      running = null;
    }
  }
  return [wait.toFixed(2) + " с", run.toFixed(2) + " с"];
}
async function refreshAdmin() {
  if (state.user?.role !== "admin") return;
  const [admin, monitor] = await Promise.all([
    api("/api/admin/state"),
    api("/api/monitor").catch((e) => ({ error: e.message })),
  ]);
  state.admin = admin;
  $("#broker-status").textContent = monitor.error ? "Брокер недоступен" : "Брокер подключён";
  $("#broker-status").className = `badge ${monitor.error ? "error" : "completed"}`;
  if (!monitor.error) {
    $("#metrics").replaceChildren(
      ...["queued", "running", "completed", "error"].map((status) => {
        const card = el("div", "metric");
        card.append(
          el("small", "", statusNames[status] + " · задания"),
          el("strong", "", monitor.tasks[status] || 0),
        );
        return card;
      }),
    );
    $("#workers").replaceChildren(
      ...(monitor.workers.length
        ? monitor.workers.map((w) => {
            const row = el("div", "worker"),
              info = el("div");
            info.append(
              el("strong", "", w.worker_id),
              el(
                "small",
                "",
                `Выполнено: ${w.completed} · heartbeat: ${Number(w.heartbeat_age ?? 0).toFixed(1)} с назад`,
              ),
            );
            const label = { idle: "Свободен", busy: "Занят", dead: "Нет связи" };
            row.append(
              info,
              el(
                "span",
                `badge ${w.state === "dead" ? "error" : w.state === "busy" ? "running" : "completed"}`,
                label[w.state] || w.state,
              ),
            );
            return row;
          })
        : [empty("Нет подключённых воркеров")]),
    );
  } else {
    $("#workers").replaceChildren(empty(monitor.error));
    $("#metrics").replaceChildren();
  }
  renderAdminOrders();
  renderBatch();
  if (state.adminTab === "products") renderProducts();
}
function renderAdminOrders() {
  if (!state.admin) return;
  const orders = state.admin.orders.filter((o) => $("#show-generated").checked || !o.batch_id);
  const root = $("#admin-orders-list");
  const opened = new Set(
    [...root.querySelectorAll("article")]
      .filter((n) => n.querySelector("details[open]"))
      .map((n) => n.dataset.id),
  );
  root.replaceChildren(
    ...(orders.length
      ? orders.map((o) => {
          const card = orderCard(o, true);
          card.dataset.id = o.id;
          if (opened.has(o.id)) card.querySelector("details").open = true;
          return card;
        })
      : [empty("Заказов пока нет.")]),
  );
}
function renderBatch() {
  const batch = state.admin.batches[0];
  $("#stop-batch").hidden = true;
  if (!batch) return;
  const orders = state.admin.orders.filter((o) => o.batch_id === batch.id),
    done = orders.filter((o) => o.status === "completed").length,
    errors = orders.filter((o) => o.status === "error").length,
    active = orders.filter((o) => ["queued", "running"].includes(o.status)).length,
    pending = orders.filter((o) => o.status === "pending").length;
  const root = $("#batch-progress");
  root.replaceChildren(
    el(
      "p",
      "",
      `${batch.count} заказов · окно ${batch.concurrency} · приоритеты ${batch.priority_min}-${batch.priority_max}`,
    ),
  );
  const progress = el("progress", "progress");
  progress.max = batch.count;
  progress.value = done + errors;
  progress.setAttribute("aria-label", "Завершённые заказы серии");
  root.append(
    progress,
    el(
      "p",
      "small",
      `${done} завершено · ${errors} ошибок · ${active} в обработке · ${pending} ожидают подачи`,
    ),
  );
  const finished = done + errors === batch.count;
  const end = finished
    ? Math.max(...orders.map((o) => o.finished_at || o.created_at))
    : Date.now() / 1000;
  root.append(
    el(
      "p",
      "small",
      `Время серии: ${Math.max(0, end - batch.created_at).toFixed(1)} с${batch.stopped ? " · подача остановлена" : ""}`,
    ),
  );
  $("#stop-batch").hidden = !pending || batch.stopped;
  $("#batch-orders").replaceChildren(
    table(
      ["Заказ", "Приоритет", "Нагрузка", "Статус", "Воркер", "Попытка", "Ожидание*", "Расчёт*"],
      orders.map((o) => {
        const e = o.history.at(-1);
        return [
          o.id.slice(0, 8),
          o.priority,
          loadNames[o.load],
          badge(o.status),
          e?.worker_id || "-",
          e?.attempt || 0,
          ...times(o),
        ];
      }),
    ),
    el(
      "p",
      "muted small",
      "* Время накоплено по завершённым интервалам очереди и выполнения обоих этапов: sum и hash.",
    ),
  );
}
function renderProducts() {
  renderCoupons();
  const root = $("#admin-products-list");
  root.replaceChildren(
    table(
      ["Товар", "Цена, BYN", "Скидка", "Доступность", ""],
      state.admin.products.map((p) => [
        p.icon + " " + p.name,
        money(p.cents / 100),
        p.discount + "%",
        p.active ? "В продаже" : "Скрыт",
        clickButton("Изменить", "text-button", () => editProduct(p)),
      ]),
    ),
  );
}
function renderCoupons() {
  $("#admin-coupons-list").replaceChildren(table(
    ["Код", "Скидка", "Кому доступен", "Использовано", "В резерве", "Всего", "На покупателя", "По дату", "Состояние", ""],
    state.admin.coupons.map(c => [c.code, `${c.percent}%`, c.public ? "Всем по коду" : "Личный",
      c.used_count, c.reserved_count, c.max_uses ?? "Без лимита", c.per_user_limit ?? "Без лимита",
      c.expires_on ? c.expires_on.split("-").reverse().join(".") : "Без срока",
      c.available ? "Доступен" : c.reason,
      clickButton("Редактировать", "text-button", () => editCoupon(c))])
  ));
}
function couponAudience() {
  const form = $("#coupon-form"), personal = form.elements.audience.value === "personal";
  $("#coupon-login-field").hidden = !personal;
  form.elements.login.required = personal && !form.elements.original_code.value;
  if (!personal) form.elements.login.value = "";
}
function resetCouponForm() {
  const form = $("#coupon-form");
  form.reset();
  form.elements.original_code.value = "";
  form.elements.code.readOnly = false;
  $("#coupon-form-title").textContent = "Создать купон";
  $("#coupon-save").textContent = "Создать купон";
  $("#coupon-reset").hidden = true;
  form.querySelector(".form-error").textContent = "";
  couponAudience();
}
function editCoupon(coupon) {
  const form = $("#coupon-form");
  resetCouponForm();
  for (const key of ["code", "percent", "max_uses", "per_user_limit", "expires_on"])
    form.elements[key].value = coupon[key] ?? "";
  form.elements.original_code.value = coupon.code;
  form.elements.code.readOnly = true;
  form.elements.audience.value = coupon.public ? "public" : "personal";
  form.elements.active.checked = !!coupon.active;
  $("#coupon-form-title").textContent = `Редактировать ${coupon.code}`;
  $("#coupon-save").textContent = "Сохранить купон";
  $("#coupon-reset").hidden = false;
  couponAudience();
  form.scrollIntoView({behavior:"smooth", block:"center"});
}
function editProduct(p) {
  const f = $("#product-form");
  for (const key of ["id", "name", "category", "unit", "description", "discount"])
    f.elements[key].value = p[key];
  f.elements.price.value = (p.cents / 100).toFixed(2);
  f.elements.active.checked = !!p.active;
  f.querySelector(".form-error").textContent = "";
  $("#product-dialog").showModal();
}
function renderTasks() {
  const tasks = [...state.tasks.values()].reverse();
  $("#analysis-results").replaceChildren(
    ...(tasks.length
      ? tasks.map((record) => {
          const t = record.snapshot,
            box = el("div"),
            head = el("div", "result-title");
          head.append(el("h3", "", opNames[t.operation] || t.operation), badge(t.status));
          box.append(head);
          if (record.connection_error) box.append(el("p", "form-error", record.connection_error));
          if (t.error) box.append(el("p", "form-error", t.error));
          if (t.result !== null && t.result !== undefined) {
            if (t.operation === "basket_affinity") {
              box.append(
                el(
                  "p",
                  "muted small",
                  `${t.result.basket_count} чеков · ${t.result.unique_products} товаров`,
                ),
              );
              const wrap = el("div", "table-wrap");
              wrap.append(
                table(
                  ["Пара товаров", "Чеков", "Доля"],
                  t.result.pairs.map((p) => [
                    p.products.join(" + "),
                    p.count,
                    (p.support * 100).toFixed(1) + "%",
                  ]),
                ),
              );
              box.append(wrap);
            } else
              box.append(
                el(
                  "pre",
                  "",
                  typeof t.result === "string" ? t.result : JSON.stringify(t.result, null, 2),
                ),
              );
            box.append(
              clickButton("Скачать JSON ↓", "text-button", () =>
                download(`result-${t.task_id.slice(0, 8)}.json`, t),
              ),
            );
          }
          box.append(
            el(
              "p",
              "muted small",
              `${t.worker_id || "Ожидает воркера"} · приоритет ${t.priority} · попытка ${t.attempt}`,
            ),
          );
          return box;
        })
      : [empty("Выберите анализ или запустите расчёт.")]),
  );
}
async function formAction(form, action) {
  const submit = form.querySelector('button[type="submit"],button:not([type])');
  const error = form.querySelector(".form-error");
  if (error) error.textContent = "";
  if (submit) submit.disabled = true;
  try {
    await action();
  } catch (e) {
    if (error) error.textContent = e.message;
    else toast(e.message);
  } finally {
    if (submit) submit.disabled = false;
  }
}
document
  .querySelectorAll("[data-view]")
  .forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
$(".brand").addEventListener("click", (e) => {
  e.preventDefault();
  showView("catalog");
});
$("#login-button").addEventListener("click", () => (state.user ? showView("orders") : openAuth()));
$("#cart-button").addEventListener("click", () => showView("cart"));
$("#welcome-button").addEventListener("click", () => showView("orders"));
$("#sale-button").addEventListener("click", () => {
  state.category = "Со скидкой";
  renderCatalog();
  $(".catalog-heading").scrollIntoView({ behavior: "smooth" });
});
$("#search").addEventListener("input", renderCatalog);
$("#sort").addEventListener("change", renderCatalog);
$("#coupon").addEventListener("input", () => {
  state.couponRequest++;
  state.appliedCoupon = null;
  state.checkoutKey = null;
  $("#coupon-message").textContent = $("#coupon").value.trim() ? "Нажмите «Применить», чтобы проверить код" : "";
  $("#coupon-message").className = "small muted";
  $("#remove-coupon").hidden = !$("#coupon").value;
  renderTotals();
});
$("#coupon").addEventListener("keydown", e => {if(e.key === "Enter") {e.preventDefault(); applyCoupon();}});
$("#apply-coupon").addEventListener("click", applyCoupon);
$("#remove-coupon").addEventListener("click", () => {clearCoupon(); renderTotals();});
document.querySelectorAll("[data-close]").forEach((b) =>
  b.addEventListener("click", () => {
    document.getElementById(b.dataset.close).close();
    state.pendingProduct = null;
  }),
);
$("#auth-dialog").addEventListener("cancel", () => (state.pendingProduct = null));
$("#auth-switch").addEventListener("click", () => {
  state.register = !state.register;
  renderAuth();
});
$("#auth-form").addEventListener("submit", (e) => {
  e.preventDefault();
  formAction(e.target, async () => {
    const f = e.target;
    await api(`/api/auth/${state.register ? "register" : "login"}`, {
      login: f.elements.login.value,
      password: f.elements.password.value,
    });
    f.reset();
    $("#auth-dialog").close();
    await loadSession();
    const pending = state.pendingProduct;
    state.pendingProduct = null;
    if (pending) await addProduct(pending);
    toast("Вы вошли в аккаунт");
  });
});
$("#logout-button").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", {});
    state.stream?.close();
    state.admin = null;
    state.checkoutKey = null;
    state.tasks.clear();
    await loadSession();
    showView("catalog");
  } catch (e) {
    toast(e.message);
  }
});
$("#checkout").addEventListener("click", async () => {
  const button = $("#checkout");
  button.disabled = true;
  try {
    state.checkoutKey ||=
      globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    await api("/api/orders", { request_key: state.checkoutKey, coupon: state.appliedCoupon?.code || "" });
    clearCoupon();
    await refreshAccount();
    showView("orders");
    toast("Заказ принят. Готовим ваш чек.");
  } catch (e) {
    toast(e.message);
  } finally {
    renderTotals();
  }
});
document.querySelectorAll("[data-admin-tab]").forEach((b) =>
  b.addEventListener("click", () => {
    state.adminTab = b.dataset.adminTab;
    document.querySelectorAll("[data-admin-tab]").forEach((tab) => {
      tab.classList.toggle("active", tab === b);
      tab.setAttribute("aria-selected", String(tab === b));
    });
    document
      .querySelectorAll(".admin-page")
      .forEach((page) => (page.hidden = page.id !== "admin-" + state.adminTab));
    if (state.adminTab === "products" && state.admin) renderProducts();
  }),
);
$("#priority-mode").addEventListener("change", () => {
  const form = $("#generator-form");
  form.elements.priority_max.disabled = $("#priority-mode").value === "same";
});
$("#generator-form").addEventListener("submit", (e) => {
  e.preventDefault();
  formAction(e.target, async () => {
    const f = e.target;
    await api("/api/admin/generate", {
      count: Number(f.elements.count.value),
      concurrency: Number(f.elements.concurrency.value),
      load: f.elements.load.value,
      priority_min: Number(f.elements.priority_min.value),
      priority_max: Number(
        f.elements[$("#priority-mode").value === "same" ? "priority_min" : "priority_max"].value,
      ),
    });
    await refreshAdmin();
    toast("Серия запущена");
  });
});
$("#stop-batch").addEventListener("click", async () => {
  try {
    await api("/api/admin/stop", { batch_id: state.admin.batches[0].id });
    await refreshAdmin();
  } catch (e) {
    toast(e.message);
  }
});
$("#show-generated").addEventListener("change", renderAdminOrders);
$("#product-form").addEventListener("submit", (e) => {
  e.preventDefault();
  formAction(e.target, async () => {
    const data = Object.fromEntries(new FormData(e.target));
    data.id = Number(data.id);
    data.cents = Math.round(Number(data.price) * 100);
    data.discount = Number(data.discount);
    data.active = e.target.elements.active.checked;
    await api("/api/admin/product", data);
    $("#product-dialog").close();
    state.products = (await api("/api/catalog")).products;
    renderCatalog();
    await refreshAdmin();
    toast("Товар сохранён");
  });
});
$("#coupon-form").addEventListener("submit", (e) => {
  e.preventDefault();
  formAction(e.target, async () => {
    const data = Object.fromEntries(new FormData(e.target));
    data.percent = Number(data.percent);
    data.public = data.audience === "public";
    data.active = e.target.elements.active.checked;
    for (const key of ["max_uses", "per_user_limit"]) data[key] = data[key] === "" ? null : Number(data[key]);
    data.expires_on = data.expires_on || null;
    if (!data.original_code) delete data.original_code;
    await api("/api/admin/coupon", data);
    resetCouponForm();
    await refreshAdmin();
    toast("Купон сохранён");
  });
});
$("#coupon-reset").addEventListener("click", resetCouponForm);
$("#coupon-form").elements.audience.addEventListener("change", couponAudience);
$("#operation").addEventListener("change", setOperation);
$("#task-form").addEventListener("submit", (e) => {
  e.preventDefault();
  formAction(e.target, async () => {
    let data;
    try {
      data = JSON.parse($("#task-data").value, (_key, value) => {
        if (
          typeof value === "number" &&
          (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value)))
        )
          throw new Error();
        return value;
      });
    } catch {
      throw new Error("Проверьте JSON: синтаксис и допустимый размер чисел");
    }
    const record = await api("/api/tasks", {
      operation: $("#operation").value,
      data,
      priority: Number(e.target.elements.priority.value),
    });
    state.tasks.set(record.snapshot.task_id, record);
    renderTasks();
  });
});
document.querySelectorAll("[data-analysis]").forEach((b) =>
  b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const record = await api("/api/admin/analytics", {
        operation: b.dataset.analysis,
        include_generated: $("#analytics-generated").checked,
      });
      state.tasks.set(record.snapshot.task_id, record);
      renderTasks();
    } catch (e) {
      toast(e.message);
    } finally {
      b.disabled = false;
    }
  }),
);
async function pollAdmin() {
  try {
    if (state.view === "admin" && !document.hidden) await refreshAdmin();
  } catch (e) {
    $("#broker-status").textContent = e.message;
  } finally {
    setTimeout(pollAdmin, 1500);
  }
}
async function init() {
  try {
    state.products = (await api("/api/catalog")).products;
    renderCatalog();
    await loadSession();
    pollAdmin();
  } catch (e) {
    toast("Не удалось подключиться: " + e.message);
  }
}
init();
