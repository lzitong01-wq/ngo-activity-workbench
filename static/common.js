/**
 * 任务5 公共前端库
 * - bootGate(): 启动时校验登录态与账号状态（未登录/待审核/驳回/停用 → 跳登录页）
 * - 用户栏渲染 + 按权限显示/隐藏导航（data-perm）
 * - api(): fetch 封装，401 自动跳登录页
 */

const RAINBOW_LOGIN_URL = "/static/login.html";

/** 启动门禁：返回 user（未通过会跳转或给出提示，且 resolve(null)） */
async function bootGate(opts) {
    opts = opts || {};
    let me;
    try {
        const r = await fetch("/api/auth/me", { credentials: "same-origin" });
        me = (await r.json()).data || {};
    } catch (e) {
        location.href = RAINBOW_LOGIN_URL;
        return null;
    }
    if (!me.authenticated) {
        // 未登录或待审核/已驳回/停用
        if (me.status === "待审核") {
            document.body.innerHTML = gateScreen("⏳ 账号待审核", "你的注册申请已提交，管理员审核通过后才能使用系统功能。",
                [{ text: "返回登录页", href: RAINBOW_LOGIN_URL, cls: "bg-blue-600" }]);
        } else if (me.status === "已驳回") {
            document.body.innerHTML = gateScreen("🚫 注册申请已驳回",
                "驳回原因：" + (me.reject_reason || "不符合条件") + "。如有疑问请联系管理员。",
                [{ text: "返回登录页", href: RAINBOW_LOGIN_URL, cls: "bg-blue-600" }]);
        } else if (me.status && me.status !== "启用") {
            document.body.innerHTML = gateScreen("🚫 账号已停用", "账号状态：" + me.status + "，请联系管理员。",
                [{ text: "返回登录页", href: RAINBOW_LOGIN_URL, cls: "bg-blue-600" }]);
        } else {
            location.href = RAINBOW_LOGIN_URL + (opts.required === false ? "" : "?next=" + encodeURIComponent(location.pathname + location.search));
        }
        return null;
    }
    renderUserBar(me);
    applyNavPerms(me);
    if (opts.minPerm && !me.permissions[opts.minPerm]) {
        document.body.innerHTML = gateScreen("🔒 无权访问",
            `当前角色「${me.role_label}」没有「${opts.minPermLabel || opts.minPerm}」权限。`,
            [{ text: "返回登录页", href: RAINBOW_LOGIN_URL, cls: "bg-blue-600" }]);
        return null;
    }
    window.__me = me;
    return me;
}

function gateScreen(title, desc, btns) {
    return `<div class="min-h-screen flex items-center justify-center bg-gray-50">
        <div class="bg-white rounded-xl shadow-lg p-10 max-w-md text-center">
            <h2 class="text-xl font-bold text-gray-800 mb-2">${title}</h2>
            <p class="text-sm text-gray-500 mb-6">${desc}</p>
            <div class="flex justify-center gap-3">${(btns || []).map(b =>
                `<a href="${b.href}" class="px-5 py-2 rounded text-white text-sm ${b.cls}">${b.text}</a>`).join("")}</div>
        </div></div>`;
}

/** 顶部用户栏（追加到 #userbar 容器；没有则插入 body 顶部固定条） */
function renderUserBar(user) {
    const bar = document.createElement("div");
    bar.className = "rainbow-userbar";
    bar.innerHTML = `
        <div class="flex items-center gap-2 text-sm">
            <span class="inline-flex items-center justify-center w-7 h-7 rounded-full bg-blue-600 text-white text-xs font-bold">${(user.display_name || "用").slice(0, 1)}</span>
            <span class="font-semibold text-gray-700">${user.display_name || user.username}</span>
            <span class="px-2 py-0.5 rounded text-xs font-medium role-chip" data-role="${user.role}">${user.role_label}</span>
        </div>
        <button id="btn-logout" class="text-xs text-gray-500 hover:text-red-600 border border-gray-300 rounded px-2 py-1">退出登录</button>`;
    bar.querySelector("#btn-logout").onclick = async () => {
        await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
        location.href = RAINBOW_LOGIN_URL;
    };
    let host = document.getElementById("userbar");
    if (!host) {
        host = document.createElement("div");
        host.id = "userbar";
        host.className = "fixed top-3 right-4 z-50 bg-white/95 shadow-md rounded-lg px-4 py-2 flex items-center gap-3 border border-gray-100";
        document.body.appendChild(host);
    }
    host.innerHTML = "";
    host.appendChild(bar);
    // 角色徽标配色
    const chip = host.querySelector(".role-chip");
    const colors = { admin: "bg-purple-100 text-purple-700", social_worker: "bg-blue-100 text-blue-700", volunteer: "bg-green-100 text-green-700", parent: "bg-orange-100 text-orange-700" };
    chip.classList.add(...(colors[user.role] || "bg-gray-100 text-gray-600").split(" "));
}

/** 导航按权限显隐：所有带 data-perm 的 a；并对整体无权限的页给出提示 */
function applyNavPerms(user) {
    document.querySelectorAll("a[data-perm]").forEach(a => {
        const perm = a.dataset.perm;
        if (perm && user.permissions && !user.permissions[perm]) {
            a.style.display = "none";
        }
    });
}

/** fetch 封装：非 2xx 抛错（含 message），401 跳登录 */
async function api(url, options) {
    options = options || {};
    options.credentials = "same-origin";
    if (options.body && typeof options.body === "object") {
        options.headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
        options.body = JSON.stringify(options.body);
    }
    const r = await fetch(url, options);
    let body = {};
    try { body = await r.json(); } catch (e) { /* ignore */ }
    if (r.status === 401) { location.href = RAINBOW_LOGIN_URL; throw new Error("未登录"); }
    if (!r.ok) throw new Error(body.error || `请求失败（${r.status}）`);
    return body;
}

/** 轻提示 */
function toast(msg, ok) {
    let host = document.getElementById("rainbow-toast");
    if (!host) {
        host = document.createElement("div");
        host.id = "rainbow-toast";
        host.style.cssText = "position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:9999;";
        document.body.appendChild(host);
    }
    const el = document.createElement("div");
    el.className = `px-4 py-2 rounded-lg shadow-lg text-sm mb-2 ${ok === false ? "bg-red-600" : "bg-green-600"} text-white`;
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => el.remove(), 3000);
}

/** HTML 转义 */
function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** id 取元素 */
function $(id) { return document.getElementById(id); }
