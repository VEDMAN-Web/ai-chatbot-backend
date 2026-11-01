// Tajlandia AI Frontend Application State
let currentMode = 'public'; // 'public' | 'admin'
let currentSessionId = '';
let currentLanguage = localStorage.getItem('tajlandia_language') || 'en'; // 'en' | 'pl'
let isTypingActive = false;
let activeUserPreferences = {};

// Multi-tenant branding state (tajlandia.com, thai.estate, varsoviaestate.com)
let activeTenantOrigin = 'varsoviaestate.com';
let activeTenantBrand = 'Varsovia Estate';
let activeBotName = 'Varsovia Estate Advisor';

async function initTenantBranding() {
  const host = window.location.hostname.toLowerCase();
  const params = new URLSearchParams(window.location.search);
  const requestedOrigin = params.get('site_origin') || host;

  try {
    const res = await fetch(`/api/v1/branding?site_origin=${encodeURIComponent(requestedOrigin)}`);
    if (res.ok) {
      const b = await res.json();
      activeTenantOrigin = b.site_origin;
      activeTenantBrand = b.brand_name;
      activeBotName = b.bot_name;

      const titleEl = document.querySelector('.brand-title');
      if (titleEl) titleEl.innerText = b.brand_name;
      const subEl = document.getElementById('brandSubtitle');
      if (subEl) subEl.innerText = b.brand_subtitle;
      document.title = b.brand_title;

      const heroEl = document.getElementById('heroTitle');
      if (heroEl) {
        heroEl.innerText = `Welcome to ${b.brand_name}`;
      }
      const discEl = document.getElementById('footerDisclaimer');
      if (discEl) {
        discEl.innerText = `${b.brand_name} can make mistakes. Check important information.`;
      }
    }
  } catch (e) {
    console.warn('Could not load tenant branding:', e);
  }
}

const UI_STRINGS = {
  en: {
    brandSubtitle: "Property Platform",
    newChat: "New chat",
    clientChat: "Client Chat",
    adminTeam: "Admin Team",
    langLabel: "Language / Język",
    langEnglish: "English",
    langPolish: "Polski",
    recentConversations: "Recent Conversations",
    statusOnline: "Online & Ready",
    topUserChatbot: "Test User Chatbot",
    topAdminChatbot: "Test Admin Side Chatbot",
    searchPlaceholder: "Search by city, project or property ID",
    reports: "Reports",
    addListing: "Add Listing",
    batchOps: "Bulk File Ops (6D Undo)",
    activeFocus: "Active Focus:",
    whatWeFetched: "Matching properties",
    tryAsking: "Try asking me:",
    resetPreferences: "🔄 Reset Preferences",
    disclaimer: "Tajlandia AI can make mistakes. Check important information.",
    heroTitlePublic: "Welcome to Tajlandia AI",
    heroSubtitlePublic: "Discover verified pool villas, luxury condominiums, and tropical estates across Phuket, Koh Samui, and Koh Phangan.",
    inputPlaceholderPublic: "Ask me anything about Thailand properties (e.g. 2-bed pool villa in Bang Tao)...",
    samplePromptsPublic: [
      "🏖️ Explore luxury pool villas and condominiums in Thailand",
      "📖 How does property ownership & buying work in Thailand?"
    ],
    heroTitleAdmin: "Tajlandia AI — Admin Intelligence",
    heroSubtitleAdmin: "Privileged access to confidential developer specs, structural materials, bulk file operations with 6-day undo, and staged write actions.",
    inputPlaceholderAdmin: "Ask internal intelligence (e.g. upload bulk file, generate PDF report, inventory stats)...",
    samplePromptsAdmin: [
      "📂 Upload property Excel/PDF to bulk update or remove listings",
      "🕒 Show batch operations history & rollback within 6-day window",
      "What are the private developer specs and roof material for 15 Sukhumvit Residences?",
      "Generate a comprehensive PDF report for 15 Sukhumvit Residences"
    ]
  },
  pl: {
    brandSubtitle: "Platforma Nieruchomości",
    newChat: "Nowy czat",
    clientChat: "Czat Klienta",
    adminTeam: "Zespół Admina",
    langLabel: "Język / Language",
    langEnglish: "Angielski",
    langPolish: "Polski",
    recentConversations: "Ostatnie Rozmowy",
    statusOnline: "Online i Gotowy",
    topUserChatbot: "Testuj Czat Klienta",
    topAdminChatbot: "Testuj Panel Admina",
    searchPlaceholder: "Szukaj po mieście, projekcie lub ID nieruchomości",
    reports: "Raporty",
    addListing: "Dodaj Ofertę",
    batchOps: "Operacje Masowe (6D Undo)",
    activeFocus: "Aktywne Filtry:",
    whatWeFetched: "Pasujące oferty",
    tryAsking: "Przykładowe pytania:",
    resetPreferences: "🔄 Zresetuj Preferencje",
    disclaimer: "Tajlandia AI może popełniać błędy. Sprawdź ważne informacje.",
    heroTitlePublic: "Witamy w Tajlandia AI",
    heroSubtitlePublic: "Odkryj zweryfikowane wille z basenem, luksusowe apartamenty i tropikalne rezydencje na Phuket, Koh Samui i Koh Phangan.",
    inputPlaceholderPublic: "Zapytaj o nieruchomości w Tajlandii (np. willa z 2 sypialniami w Bang Tao)...",
    samplePromptsPublic: [
      "🏖️ Przeglądaj luksusowe wille i apartamenty w Tajlandii",
      "📖 Jak wygląda zakup nieruchomości i prawo własności w Tajlandii?"
    ],
    heroTitleAdmin: "Tajlandia AI — Panel Administracyjny",
    heroSubtitleAdmin: "Poufny dostęp do specyfikacji deweloperskich, operacji masowych z 6-dniowym cofaniem zmian i analizy leadów.",
    inputPlaceholderAdmin: "Zapytaj asystenta zarządczego (np. wgraj plik Excel, raport PDF, stan portfolio)...",
    samplePromptsAdmin: [
      "📂 Wgraj plik Excel/PDF z nieruchomościami, aby zaktualizować lub usunąć oferty",
      "🕒 Pokaż historię operacji masowych i cofnij zmiany (okno 6 dni)",
      "Jakie są specyfikacje techniczne i materiał dachu dla 15 Sukhumvit Residences?",
      "Wygeneruj oficjalny raport PDF dla inwestora dla 15 Sukhumvit Residences"
    ]
  },
  th: {
    brandSubtitle: "แพลตฟอร์มอสังหาริมทรัพย์",
    newChat: "แชทใหม่",
    clientChat: "แชทลูกค้า",
    adminTeam: "ทีมผู้ดูแลระบบ",
    langLabel: "ภาษา / Language",
    langEnglish: "English",
    langPolish: "Polski",
    langThai: "ภาษาไทย",
    recentConversations: "ประวัติการสนทนา",
    statusOnline: "ออนไลน์พร้อมใช้งาน",
    topUserChatbot: "ทดสอบ แชทบอทลูกค้า",
    topAdminChatbot: "ทดสอบ แผงควบคุมแอดมิน",
    searchPlaceholder: "ค้นหาตามเมือง, ชื่อโครงการ หรือรหัสอสังหาฯ",
    reports: "รายงาน",
    addListing: "เพิ่มรายการ",
    batchOps: "จัดการไฟล์ชุดใหญ่ (ย้อนกลับได้ 6 วัน)",
    activeFocus: "เงื่อนไขที่เลือก:",
    whatWeFetched: "รายการที่ตรงกัน",
    tryAsking: "ลองสอบถาม:",
    resetPreferences: "🔄 รีเซ็ตเงื่อนไข",
    disclaimer: "Tajlandia AI อาจมีข้อผิดพลาดได้ กรุณาตรวจสอบข้อมูลสำคัญอีกครั้ง",
    heroTitlePublic: "ยินดีต้อนรับสู่ Tajlandia AI",
    heroSubtitlePublic: "ค้นพบพูลวิลล่า คอนโดมิเนียมหรู และบ้านพักตากอากาศที่ผ่านการตรวจสอบในภูเก็ต เกาะสมุย และเกาะพะงัน",
    inputPlaceholderPublic: "สอบถามข้อมูลอสังหาริมทรัพย์ในไทย (เช่น พูลวิลล่า 2 ห้องนอนในบางเทา)...",
    samplePromptsPublic: [
      "🏖️ ค้นหาพูลวิลล่าและคอนโดมิเนียมหรูในประเทศไทย",
      "📖 การซื้ออสังหาริมทรัพย์และกรรมสิทธิ์ในไทยทำงานอย่างไร?"
    ],
    heroTitleAdmin: "Tajlandia AI — ระบบข่าวกรองผู้ดูแลระบบ",
    heroSubtitleAdmin: "สิทธิ์เข้าถึงข้อมูลเชิงลึกของผู้พัฒนาโครงการ การจัดการไฟล์ชุดใหญ่พร้อมระบบย้อนกลับ 6 วัน และการออกรายงาน",
    inputPlaceholderAdmin: "สอบถามข้อมูลภายใน (เช่น อัปโหลดไฟล์ Excel, สร้างรายงาน PDF, สถิติอสังหาริมทรัพย์)...",
    samplePromptsAdmin: [
      "📂 อัปโหลดไฟล์ Excel/PDF เพื่ออัปเดตหรือลบรายการอสังหาริมทรัพย์แบบกลุ่ม",
      "🕒 แสดงประวัติการดำเนินการแบบกลุ่มและย้อนกลับข้อมูลภายใน 6 วัน",
      "สเปกเชิงลึกและวัสดุหลังคาของโครงการ 15 Sukhumvit Residences คืออะไร?",
      "สร้างรายงาน PDF สำหรับโครงการ 15 Sukhumvit Residences"
    ]
  }
};

function setAppLanguage(lang, updateUI = true) {
  currentLanguage = (lang === 'pl') ? 'pl' : ((lang === 'th') ? 'th' : 'en');
  localStorage.setItem('tajlandia_language', currentLanguage);

  // Update Top buttons
  const topEn = document.getElementById('topBtnLangEn');
  const topPl = document.getElementById('topBtnLangPl');
  const topTh = document.getElementById('topBtnLangTh');
  if (topEn) topEn.classList.toggle('active', currentLanguage === 'en');
  if (topPl) topPl.classList.toggle('active', currentLanguage === 'pl');
  if (topTh) topTh.classList.toggle('active', currentLanguage === 'th');

  // Update Sidebar buttons
  const sideEn = document.getElementById('sideBtnLangEn');
  const sidePl = document.getElementById('sideBtnLangPl');
  const sideTh = document.getElementById('sideBtnLangTh');
  if (sideEn) sideEn.classList.toggle('active', currentLanguage === 'en');
  if (sidePl) sidePl.classList.toggle('active', currentLanguage === 'pl');
  if (sideTh) sideTh.classList.toggle('active', currentLanguage === 'th');

  if (updateUI) {
    applyLanguageStrings();
  }
}

function applyLanguageStrings() {
  const s = UI_STRINGS[currentLanguage] || UI_STRINGS.en;

  const brandSub = document.getElementById('brandSubtitle');
  if (brandSub) brandSub.innerText = s.brandSubtitle;

  const newChatLabel = document.getElementById('btnNewChatLabel');
  if (newChatLabel) newChatLabel.innerText = s.newChat;

  const tabPub = document.getElementById('tabPublic');
  if (tabPub) tabPub.innerText = s.clientChat;

  const tabAdm = document.getElementById('tabAdmin');
  if (tabAdm) tabAdm.innerText = s.adminTeam;

  const sideLangLabel = document.getElementById('sidebarLangLabel');
  if (sideLangLabel) sideLangLabel.innerText = s.langLabel;

  const sideHist = document.getElementById('sidebarHistoryLabel');
  if (sideHist) sideHist.innerText = s.recentConversations;

  const agentStat = document.getElementById('agentStatusText');
  if (agentStat) agentStat.innerText = s.statusOnline;

  const topUser = document.getElementById('topBtnUserText');
  if (topUser) topUser.innerText = s.topUserChatbot;

  const topAdmin = document.getElementById('topBtnAdminText');
  if (topAdmin) topAdmin.innerText = s.topAdminChatbot;

  const searchInput = document.getElementById('topSearchInput');
  if (searchInput) searchInput.placeholder = s.searchPlaceholder;

  const reportLabel = document.getElementById('btnReportLabel');
  if (reportLabel) reportLabel.innerText = s.reports;

  const addListingLabel = document.getElementById('btnAddListingLabel');
  if (addListingLabel) addListingLabel.innerText = s.addListing;

  const batchOpsLabel = document.getElementById('btnBatchOpsLabel');
  if (batchOpsLabel) batchOpsLabel.innerText = s.batchOps || "Bulk File Ops (6D Undo)";

  const focusTitle = document.getElementById('focusBarTitle');
  if (focusTitle) focusTitle.innerText = s.activeFocus;

  const inspectFocusLabel = document.getElementById('btnInspectFocusLabel');
  if (inspectFocusLabel) inspectFocusLabel.innerText = s.whatWeFetched;

  const tryAsking = document.getElementById('tryAskingLabel');
  if (tryAsking) tryAsking.innerText = s.tryAsking;

  const disclaimer = document.getElementById('footerDisclaimer');
  if (disclaimer) disclaimer.innerText = s.disclaimer;

  // Update Hero & Input for current mode
  updateHeroAndInputUI();
}

function updateHeroAndInputUI() {
  const s = UI_STRINGS[currentLanguage] || UI_STRINGS.en;
  const heroTitle = document.querySelector('.hero-title');
  const heroSubtitle = document.querySelector('.hero-subtitle');
  const promptGrid = document.getElementById('promptCardsGrid');
  const chatInput = document.getElementById('chatInput');

  if (currentMode === 'public') {
    if (heroTitle) heroTitle.innerText = s.heroTitlePublic;
    if (heroSubtitle) heroSubtitle.innerText = s.heroSubtitlePublic;
    if (chatInput) chatInput.placeholder = s.inputPlaceholderPublic;
    if (promptGrid) {
      promptGrid.innerHTML = s.samplePromptsPublic.map(p => `
        <div class="prompt-card" onclick="sendSamplePrompt(${jsArg(p)})">
          <div class="prompt-card-text">${p}</div>
        </div>
      `).join('');
    }
  } else {
    if (heroTitle) heroTitle.innerText = s.heroTitleAdmin;
    if (heroSubtitle) heroSubtitle.innerText = s.heroSubtitleAdmin;
    if (chatInput) chatInput.placeholder = s.inputPlaceholderAdmin;
    if (promptGrid) {
      promptGrid.innerHTML = s.samplePromptsAdmin.map(p => `
        <div class="prompt-card" onclick="sendSamplePrompt(${jsArg(p)})">
          <div class="prompt-card-text">${p}</div>
        </div>
      `).join('');
    }
  }
}

const API_ROUTES = {
  publicChat: '/api/v1/chat/public',
  adminChat: '/api/v1/admin/chat',
  publicHistory: (id) => `/api/v1/chat/history/${id}`,
  adminHistory: (id) => `/api/v1/admin/chat/history/${id}`,
  confirmAction: (id) => `/api/v1/admin/actions/${id}/confirm`,
  inspectAction: (id) => `/api/v1/admin/actions/${id}`,
  rejectAction: (id) => `/api/v1/admin/actions/${id}/reject`,
  properties: '/api/v1/properties',
  login: '/api/v1/auth/login',
  logout: '/api/v1/auth/logout',
  queryLogSessions: '/api/v1/admin/query-logs/sessions',
  queryLogSession: (id) => `/api/v1/admin/query-logs/sessions/${encodeURIComponent(id)}`,
  queryLogStats: '/api/v1/admin/query-logs/stats',
  adminChatLogSessions: '/api/v1/admin/chat-logs/sessions',
  adminChatLogSession: (id) => `/api/v1/admin/chat-logs/sessions/${encodeURIComponent(id)}`,
  adminChatLogStats: '/api/v1/admin/chat-logs/stats',
};

const confirmationTokens = {};

function rememberConfirmationToken(actionId, token) {
  if (actionId && token) confirmationTokens[actionId] = token;
}

const ADMIN_TOKEN_KEY = 'tajlandia_admin_access_token';
const ADMIN_USER_KEY = 'tajlandia_admin_user';

function getAdminAccessToken() {
  return (localStorage.getItem(ADMIN_TOKEN_KEY) || '').trim();
}

function getAdminUser() {
  try {
    return JSON.parse(localStorage.getItem(ADMIN_USER_KEY) || 'null');
  } catch (e) {
    return null;
  }
}

function isAdminLoggedIn() {
  return Boolean(getAdminAccessToken());
}

function adminAuthHeaders(extra = {}) {
  const token = getAdminAccessToken();
  const headers = { ...extra };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}

// FastAPI sends `detail` as a string, an object or a list of validation errors. Never print it as "[object Object]".
function formatApiError(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  const one = (item) => {
    if (typeof item === 'string') return item;
    if (item && typeof item === 'object') {
      const where = Array.isArray(item.loc) ? item.loc.filter((p) => p !== 'body').join('.') : '';
      const text = item.msg || item.message || '';
      if (text) return where ? `${where}: ${text}` : text;
    }
    try { return JSON.stringify(item); } catch (e) { return fallback; }
  };
  return (Array.isArray(detail) ? detail.map(one).join('; ') : one(detail)) || fallback;
}

function isProtectedAdminDownloadUrl(url) {
  if (!url || typeof url !== 'string') return false;
  try {
    const path = url.startsWith('http') ? new URL(url).pathname : url;
    return /\/api\/v1\/admin\//i.test(path);
  } catch (_) {
    return /\/api\/v1\/admin\//i.test(url);
  }
}

function triggerBlobDownload(blob, filename) {
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objectUrl;
  a.download = filename || 'download';
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    try { URL.revokeObjectURL(objectUrl); } catch (_) {}
    try { a.remove(); } catch (_) {}
  }, 1500);
}

const LARGE_REPORT_BLOB_BYTES = 20 * 1024 * 1024;

/**
 * Authenticated admin file download (Bearer via fetch). Never opens a blank tab.
 * For large files, requests a short-lived signed URL instead of buffering a huge blob.
 */
async function downloadAdminProtectedFile(url, filename, buttonEl) {
  const originalLabel = buttonEl ? buttonEl.innerHTML : '';
  const setBusy = (busy, label) => {
    if (!buttonEl) return;
    buttonEl.disabled = !!busy;
    if (label != null) buttonEl.innerHTML = label;
  };
  const showErr = (msg) => {
    if (buttonEl) {
      buttonEl.innerHTML = msg;
      buttonEl.disabled = false;
      setTimeout(() => { buttonEl.innerHTML = originalLabel; }, 4000);
    } else {
      alert(msg);
    }
  };

  setBusy(true, 'Preparing download...');
  try {
    const path = (() => {
      try {
        return url.startsWith('http') ? new URL(url).pathname + new URL(url).search : url;
      } catch (_) {
        return url;
      }
    })();

    // Large-file path: signed link (no Authorization header needed on GET)
    const fname = filename || (path.split('/').pop() || 'report.pdf');
    const reportsMatch = path.match(/\/api\/v1\/admin\/reports\/([^/?#]+)/i);
    if (reportsMatch && reportsMatch[1] && reportsMatch[1].toLowerCase() !== 'download') {
      // Probe with HEAD/GET; if Content-Length is huge, use signed link
      const probe = await fetch(path, { method: 'GET', headers: adminAuthHeaders(), credentials: 'same-origin' });
      if (probe.status === 401 || probe.status === 403) {
        showErr('Please sign in again');
        return;
      }
      if (probe.status === 404 || probe.status === 410) {
        showErr('This report is no longer available - regenerate it');
        try { await probe.body?.cancel?.(); } catch (_) {}
        return;
      }
      if (!probe.ok) {
        showErr('Download failed — please try again');
        try { await probe.body?.cancel?.(); } catch (_) {}
        return;
      }
      const len = parseInt(probe.headers.get('Content-Length') || '0', 10);
      if (len > LARGE_REPORT_BLOB_BYTES) {
        try { await probe.body?.cancel?.(); } catch (_) {}
        const linkRes = await fetch(`/api/v1/admin/reports/${encodeURIComponent(reportsMatch[1])}/link`, {
          method: 'POST',
          headers: adminAuthHeaders({ 'Content-Type': 'application/json' }),
          credentials: 'same-origin',
        });
        if (linkRes.status === 401 || linkRes.status === 403) {
          showErr('Please sign in again');
          return;
        }
        if (linkRes.status === 404 || linkRes.status === 410) {
          showErr('This report is no longer available - regenerate it');
          return;
        }
        if (!linkRes.ok) {
          showErr('Download failed — please try again');
          return;
        }
        const linkData = await linkRes.json();
        if (!linkData.url) {
          showErr('Download failed — please try again');
          return;
        }
        // Same-window navigation to signed URL (no blank tab)
        const a = document.createElement('a');
        a.href = linkData.url;
        a.download = fname;
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setBusy(false, originalLabel);
        return;
      }
      const blob = await probe.blob();
      triggerBlobDownload(blob, fname);
      setBusy(false, originalLabel);
      return;
    }

    const res = await fetch(path, { method: 'GET', headers: adminAuthHeaders(), credentials: 'same-origin' });
    if (res.status === 401 || res.status === 403) {
      showErr('Please sign in again');
      return;
    }
    if (res.status === 404 || res.status === 410) {
      showErr('This report is no longer available - regenerate it');
      return;
    }
    if (!res.ok) {
      showErr('Download failed — please try again');
      return;
    }
    const blob = await res.blob();
    triggerBlobDownload(blob, fname);
    setBusy(false, originalLabel);
  } catch (err) {
    console.error('Admin download failed', err);
    showErr('Download failed — please try again');
  }
}

function updateAdminLoginUI() {
  const btn = document.getElementById('btnAdminLoginTop');
  const label = document.getElementById('btnAdminLoginTopLabel');
  const avatar = document.getElementById('staffAvatarBtn');
  const roleBadge = document.getElementById('staffRoleBadge');
  const btnSuperTop = document.getElementById('btnSuperAdminTop');
  const tabSuper = document.getElementById('tabSuperAdmin');
  const topBtnSuper = document.getElementById('topBtnSuperAdmin');
  const btnCrud = document.getElementById('btnManageProperties');
  const user = getAdminUser();
  const loggedIn = isAdminLoggedIn();
  const isSuper = loggedIn && user && (user.role === 'super_admin' || user.role === 'superadmin');
  const isAdmin = loggedIn && user && (user.role === 'admin' || isSuper);

  if (btn) {
    btn.style.display = (currentMode === 'admin' || currentMode === 'superadmin') ? 'inline-flex' : 'none';
    if (label) label.innerText = loggedIn ? (user?.email ? `Logout (${user.email.split('@')[0]})` : 'Logout') : 'Staff Login';
    btn.onclick = loggedIn ? () => logoutAdminStaff() : () => openAdminLoginModal();
  }

  if (roleBadge) {
    if (loggedIn && user) {
      roleBadge.style.display = 'inline-flex';
      if (isSuper) {
        roleBadge.className = 'role-badge superadmin';
        roleBadge.innerText = 'Super Admin';
      } else if (isAdmin) {
        roleBadge.className = 'role-badge admin';
        roleBadge.innerText = 'Admin';
      } else {
        roleBadge.className = 'role-badge staff';
        roleBadge.innerText = (user.role || 'Staff').toUpperCase();
      }
    } else {
      roleBadge.style.display = 'none';
    }
  }

  // Super Admin visibility
  if (btnSuperTop) {
    btnSuperTop.style.display = isSuper ? 'inline-flex' : 'none';
  }
  if (tabSuper) {
    tabSuper.style.display = isSuper ? 'inline-block' : 'none';
  }
  if (topBtnSuper) {
    topBtnSuper.style.display = isSuper ? 'inline-flex' : 'none';
  }

  // Manage listings visibility
  if (btnCrud) {
    btnCrud.style.display = (loggedIn && (currentMode === 'admin' || currentMode === 'superadmin')) ? 'inline-flex' : 'none';
  }

  if (avatar) {
    if (loggedIn && user) {
      const nameOrEmail = user.name || user.email || 'AD';
      const initials = String(nameOrEmail).trim().slice(0, 2).toUpperCase();
      avatar.innerText = initials;
      avatar.title = `${user.email || user.name} (${user.role || 'staff'})`;
    } else {
      avatar.innerText = 'TJ';
      avatar.title = 'Staff Account';
    }
  }
  updateVisitorLogsButtonVisibility();
}

function canViewVisitorLogs() {
  const role = String(getAdminUser()?.role || '').toLowerCase();
  return isAdminLoggedIn() && (role === 'admin' || role === 'super_admin');
}

function canViewAdminChatLogs() {
  return isAdminLoggedIn() && String(getAdminUser()?.role || '').toLowerCase() === 'super_admin';
}

function updateVisitorLogsButtonVisibility() {
  const btn = document.getElementById('btnVisitorLogs');
  if (!btn) return;
  btn.style.display = (currentMode === 'admin' && canViewVisitorLogs()) ? 'inline-flex' : 'none';
  const adminTab = document.getElementById('tabBtnAdminChatLogs');
  if (adminTab) adminTab.style.display = canViewAdminChatLogs() ? 'inline-flex' : 'none';
}

let visitorLogsKind = 'visitor';

function openVisitorLogsModal() {
  if (!canViewVisitorLogs()) {
    openAdminLoginModal();
    return;
  }
  const overlay = document.getElementById('visitorLogsOverlay');
  const modal = document.getElementById('visitorLogsModal');
  if (overlay) overlay.style.display = 'block';
  if (modal) modal.style.display = 'block';
  switchVisitorLogsTab(canViewAdminChatLogs() && visitorLogsKind === 'admin' ? 'admin' : 'visitor');
}

function closeVisitorLogsModal() {
  const overlay = document.getElementById('visitorLogsOverlay');
  const modal = document.getElementById('visitorLogsModal');
  if (overlay) overlay.style.display = 'none';
  if (modal) modal.style.display = 'none';
}

function switchVisitorLogsTab(kind) {
  visitorLogsKind = kind === 'admin' && canViewAdminChatLogs() ? 'admin' : 'visitor';
  const vBtn = document.getElementById('tabBtnVisitorSessions');
  const aBtn = document.getElementById('tabBtnAdminChatLogs');
  if (vBtn) vBtn.classList.toggle('active', visitorLogsKind === 'visitor');
  if (aBtn) aBtn.classList.toggle('active', visitorLogsKind === 'admin');
  loadVisitorLogs();
}

function visitorFilterQuery() {
  const params = new URLSearchParams();
  const site = document.getElementById('visitorFilterSite')?.value?.trim();
  const from = document.getElementById('visitorFilterFrom')?.value;
  const to = document.getElementById('visitorFilterTo')?.value;
  const q = document.getElementById('visitorFilterQ')?.value?.trim();
  const lang = document.getElementById('visitorFilterLang')?.value;
  const flagged = document.getElementById('visitorFilterFlagged')?.checked;
  if (site) params.set('site', site);
  if (from) params.set('date_from', from);
  if (to) params.set('date_to', to);
  if (q) params.set('q', q);
  if (lang) params.set('language', lang);
  if (flagged) params.set('flagged', 'true');
  params.set('limit', '50');
  return params.toString();
}

async function loadVisitorLogs() {
  const status = document.getElementById('visitorSessionsStatus');
  const table = document.getElementById('visitorSessionsTable');
  const empty = document.getElementById('visitorDetailEmpty');
  const content = document.getElementById('visitorDetailContent');
  if (status) status.innerText = 'Loading…';
  if (table) table.innerHTML = '';
  if (empty) empty.style.display = 'block';
  if (content) { content.style.display = 'none'; content.innerHTML = ''; }
  const sessionsUrl = visitorLogsKind === 'admin' ? API_ROUTES.adminChatLogSessions : API_ROUTES.queryLogSessions;
  const statsUrl = visitorLogsKind === 'admin' ? API_ROUTES.adminChatLogStats : API_ROUTES.queryLogStats;
  try {
    const [sessionsRes, statsRes] = await Promise.all([
      fetch(`${sessionsUrl}?${visitorFilterQuery()}`, { headers: adminAuthHeaders() }),
      fetch(statsUrl, { headers: adminAuthHeaders() }),
    ]);
    if (sessionsRes.status === 401 || sessionsRes.status === 403) {
      if (status) status.innerText = sessionsRes.status === 401 ? 'Sign in required.' : 'Not allowed for your role.';
      return;
    }
    const sessionsData = await sessionsRes.json().catch(() => ({}));
    const statsData = await statsRes.json().catch(() => ({}));
    renderVisitorStats(statsData);
    if (sessionsData.message && !(sessionsData.rows || []).length) {
      if (status) status.innerText = sessionsData.message;
      return;
    }
    const rows = sessionsData.rows || [];
    if (status) status.innerText = rows.length ? `${rows.length} session(s)` : 'No sessions match these filters.';
    if (!table) return;
    rows.forEach((row) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'visitor-session-row';
      btn.innerHTML = `<div class="visitor-session-meta"><span>${row.site_origin || '—'} · ${row.language || '—'}</span><span>${row.turns || 0} turns${row.flagged_turns ? ` · ${row.flagged_turns} flagged` : ''}</span></div><div class="visitor-session-q">${escapeHtml(row.last_question || '(no question)')}</div>`;
      btn.onclick = () => openVisitorSession(row.session_id, btn);
      table.appendChild(btn);
    });
  } catch (err) {
    if (status) status.innerText = 'Could not load logs right now.';
  }
}

function renderVisitorStats(stats) {
  const today = new Date().toISOString().slice(0, 10);
  const day = (stats.days || []).find((item) => item.date === today) || {};
  const turns = document.getElementById('visitorStatTurns');
  const sessions = document.getElementById('visitorStatSessions');
  const flagged = document.getElementById('visitorStatFlagged');
  if (turns) turns.innerText = day.turns != null ? day.turns : '0';
  if (sessions) sessions.innerText = day.sessions != null ? day.sessions : '0';
  if (flagged) flagged.innerText = day.flagged != null ? day.flagged : '0';
}

async function openVisitorSession(sessionId, rowBtn) {
  document.querySelectorAll('.visitor-session-row').forEach((el) => el.classList.remove('active'));
  if (rowBtn) rowBtn.classList.add('active');
  const empty = document.getElementById('visitorDetailEmpty');
  const content = document.getElementById('visitorDetailContent');
  if (empty) empty.style.display = 'none';
  if (content) {
    content.style.display = 'block';
    content.innerHTML = '<div class="visitor-status">Loading conversation…</div>';
  }
  const url = visitorLogsKind === 'admin'
    ? API_ROUTES.adminChatLogSession(sessionId)
    : API_ROUTES.queryLogSession(sessionId);
  try {
    const res = await fetch(url, { headers: adminAuthHeaders() });
    const data = await res.json().catch(() => ({}));
    if (!content) return;
    if (data.message && !(data.rows || []).length) {
      content.innerHTML = `<div class="visitor-status">${escapeHtml(data.message)}</div>`;
      return;
    }
    const turns = data.rows || [];
    content.innerHTML = turns.map((turn) => `
      <div class="visitor-turn">
        <div class="visitor-turn-meta">
          <span>${escapeHtml(turn.created_at || '')}</span>
          <span>cards: ${turn.cards_count || 0}</span>
          <span>fallback: ${escapeHtml(turn.fallback_type || 'none')}</span>
          ${turn.flagged ? '<span>flagged</span>' : ''}
        </div>
        <p class="visitor-turn-q"><strong>Q:</strong> ${escapeHtml(turn.user_message || '')}</p>
        <p class="visitor-turn-a"><strong>A:</strong> ${escapeHtml(turn.bot_reply || '')}</p>
        <button type="button" class="btn-secondary visitor-correct-btn" disabled title="Available in the next update">Correct this answer</button>
      </div>
    `).join('') || '<div class="visitor-status">No turns in this session.</div>';
  } catch (err) {
    if (content) content.innerHTML = '<div class="visitor-status">Could not load this conversation.</div>';
  }
}

function openAdminLoginModal() {
  const overlay = document.getElementById('adminLoginOverlay');
  const modal = document.getElementById('adminLoginModal');
  const err = document.getElementById('adminLoginError');
  const status = document.getElementById('adminLoginStatus');
  if (err) { err.style.display = 'none'; err.innerText = ''; }
  if (status) {
    if (isAdminLoggedIn()) {
      const u = getAdminUser();
      status.style.display = 'block';
      status.innerText = `Currently signed in as ${u?.email || 'staff'}. Sign in again to refresh token, or use Logout.`;
    } else {
      status.style.display = 'none';
    }
  }
  if (overlay) overlay.style.display = 'block';
  if (modal) modal.style.display = 'block';
  const emailEl = document.getElementById('adminLoginEmail');
  if (emailEl) setTimeout(() => emailEl.focus(), 50);
}

function closeAdminLoginModal() {
  const overlay = document.getElementById('adminLoginOverlay');
  const modal = document.getElementById('adminLoginModal');
  if (overlay) overlay.style.display = 'none';
  if (modal) modal.style.display = 'none';
}

async function submitAdminLogin() {
  const email = (document.getElementById('adminLoginEmail')?.value || '').trim();
  const password = document.getElementById('adminLoginPassword')?.value || '';
  const domain = document.getElementById('adminLoginDomain')?.value || 'tajlandia.com';
  const err = document.getElementById('adminLoginError');
  const btn = document.getElementById('btnAdminLogin');
  if (!email || !password) {
    if (err) { err.style.display = 'block'; err.innerText = 'Email and password are required.'; }
    return;
  }
  if (btn) { btn.disabled = true; btn.innerText = 'Signing in…'; }
  if (err) err.style.display = 'none';
  try {
    const res = await fetch(API_ROUTES.login, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, domain, site_origin: domain }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = formatApiError(data.detail || data.message, `Login failed (${res.status})`);
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    const token = data.access_token;
    if (!token) throw new Error('Login response missing access_token');
    localStorage.setItem(ADMIN_TOKEN_KEY, token);
    localStorage.setItem(ADMIN_USER_KEY, JSON.stringify(data.user || { email }));
    if (data.user?.domain) activeTenantOrigin = data.user.domain;
    else activeTenantOrigin = domain;
    updateAdminLoginUI();
    closeAdminLoginModal();
    appendMessageRow('assistant', `✅ Signed in as **${data.user?.email || email}** (${data.user?.role || 'staff'}). You can use Admin Team chat now.`);
  } catch (e) {
    if (err) {
      err.style.display = 'block';
      err.innerText = e.message || 'Login failed';
    }
  } finally {
    if (btn) { btn.disabled = false; btn.innerText = 'Sign in'; }
  }
}

async function logoutAdminStaff() {
  const token = getAdminAccessToken();
  try {
    if (token) {
      await fetch(API_ROUTES.logout, {
        method: 'POST',
        headers: adminAuthHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({}),
      });
    }
  } catch (e) { /* ignore */ }
  localStorage.removeItem(ADMIN_TOKEN_KEY);
  localStorage.removeItem(ADMIN_USER_KEY);
  updateAdminLoginUI();
  if (currentMode === 'admin') {
    appendMessageRow('assistant', 'You have been logged out of Admin Team. Sign in again to continue.');
    openAdminLoginModal();
  }
}

async function ensureAdminAuthOrPrompt() {
  if (isAdminLoggedIn()) return true;
  openAdminLoginModal();
  appendMessageRow('assistant', '⚠️ **Authentication required.** Please sign in with your staff email/password to use Admin Team chat.');
  return false;
}

const FALLBACK_VILLA_IMAGES = [
  'https://images.unsplash.com/photo-1580587771525-78b9dba3b914?auto=format&fit=crop&w=800&q=80',
  'https://images.unsplash.com/photo-1512917774080-9991f1c4c750?auto=format&fit=crop&w=800&q=80',
  'https://images.unsplash.com/photo-1613977257363-707ba9348227?auto=format&fit=crop&w=800&q=80',
  'https://images.unsplash.com/photo-1600585154340-be6161a56a0c?auto=format&fit=crop&w=800&q=80'
];

// In-memory property cache for instant modal data loading (Maps, Calendar, Email)
const propertyCache = {};

// Initialize Application
document.addEventListener('DOMContentLoaded', () => {
  initTenantBranding();
  initSession();
  setAppLanguage(currentLanguage, true);
  renderHistoryList();
  
  const newChatBtn = document.getElementById('btnNewChat');
  if (newChatBtn) {
    newChatBtn.addEventListener('click', () => startNewChat());
  }

  // Auto-resize textarea as user types
  const inputEl = document.getElementById('chatInput');
  if (inputEl) {
    inputEl.addEventListener('input', () => {
      inputEl.style.height = 'auto';
      inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
    });
  }

  // Batch dropzone drag and drop listeners
  initBatchDropzone();
  updateAdminLoginUI();

  // /admin-ui lands directly in Admin Team mode (+ login if needed)
  if (window.location.pathname.replace(/\/$/, '') === '/admin-ui') {
    setAgentMode('admin', false);
  }
});

function initSession() {
  const stored = localStorage.getItem(`tajlandia_${currentMode}_session`);
  if (stored) {
    currentSessionId = stored;
  } else {
    currentSessionId = `${currentMode}_sess_` + Math.random().toString(36).substring(2, 10);
    localStorage.setItem(`tajlandia_${currentMode}_session`, currentSessionId);
  }
}

function startNewChat(resetUI = true) {
  currentSessionId = `${currentMode}_sess_` + Math.random().toString(36).substring(2, 10);
  localStorage.setItem(`tajlandia_${currentMode}_session`, currentSessionId);
  activeUserPreferences = {};
  updateFocusBarUI();
  
  if (resetUI) {
    // Clear messages and reveal hero
    const messagesArea = document.getElementById('messagesArea');
    if (messagesArea) messagesArea.innerHTML = '';
    
    const hero = document.getElementById('welcomeHero');
    if (hero) hero.style.display = 'flex';

    const input = document.getElementById('chatInput');
    if (input) {
      input.value = '';
      input.style.height = 'auto';
      input.focus();
    }
  }

  renderHistoryList();
}

// Mode Switching (Test User Chatbot vs Test Admin Side Chatbot)
function setAgentMode(mode, createNew = true) {
  // Authorization check for superadmin mode
  if (mode === 'superadmin') {
    if (!isAdminLoggedIn()) {
      openAdminLoginModal();
      return;
    }
    const user = getAdminUser();
    if (!user || (user.role !== 'super_admin' && user.role !== 'superadmin')) {
      alert('Access restricted: Super Admin privileges required.');
      return;
    }
  }

  currentMode = mode;

  // Sync upper-side choice buttons
  const topBtnUser = document.getElementById('topBtnUser');
  const topBtnAdmin = document.getElementById('topBtnAdmin');
  const topBtnSuper = document.getElementById('topBtnSuperAdmin');
  if (topBtnUser) topBtnUser.classList.toggle('active', mode === 'public');
  if (topBtnAdmin) topBtnAdmin.classList.toggle('active', mode === 'admin');
  if (topBtnSuper) topBtnSuper.classList.toggle('active', mode === 'superadmin');

  // Sync sidebar tabs
  const tabPub = document.getElementById('tabPublic');
  const tabAdm = document.getElementById('tabAdmin');
  const tabSuper = document.getElementById('tabSuperAdmin');
  const modelLabel = document.getElementById('agentModelLabel');

  if (tabPub) tabPub.classList.toggle('active', mode === 'public');
  if (tabAdm) tabAdm.classList.toggle('active', mode === 'admin');
  if (tabSuper) tabSuper.classList.toggle('active', mode === 'superadmin');

  if (modelLabel) {
    modelLabel.innerText = mode === 'public' ? 'GPT-5.5' : (mode === 'superadmin' ? 'Claude 3.5 Sonnet (Super Admin)' : 'Claude Opus 5.5');
  }

  // Toggle Admin-only controls (Batch operations button & attachment paperclip & CRUD button)
  const btnBatchOps = document.getElementById('btnBatchOps');
  const btnAttachment = document.getElementById('btnAttachment');
  const btnCrud = document.getElementById('btnManageProperties');
  const btnPropertyImages = document.getElementById('btnPropertyImages');
  const isPrivileged = (mode === 'admin' || mode === 'superadmin');

  if (btnBatchOps) btnBatchOps.style.display = isPrivileged ? 'inline-flex' : 'none';
  if (btnAttachment) btnAttachment.style.display = isPrivileged ? 'inline-flex' : 'none';
  if (btnPropertyImages) btnPropertyImages.style.display = isPrivileged ? 'inline-flex' : 'none';
  if (btnCrud) btnCrud.style.display = isPrivileged ? 'inline-flex' : 'none';
  updateVisitorLogsButtonVisibility();
  if (!isPrivileged && typeof clearPropertyImageTray === 'function') clearPropertyImageTray();

  // Update Hero texts & prompt cards according to current mode & language
  updateHeroAndInputUI();
  updateAdminLoginUI();

  if (isPrivileged) {
    const bar = document.getElementById('userFocusBar');
    if (bar) bar.style.display = 'none';
    closeFocusDrawer();
    if (!isAdminLoggedIn()) {
      openAdminLoginModal();
    }
  } else {
    updateFocusBarUI();
  }

  if (createNew) {
    startNewChat();
  }
}

// Sample Prompts from Hero Section
function sendSamplePrompt(text) {
  if (text.includes('📂') || text.includes('Upload property Excel') || text.includes('Wgraj plik Excel') || text.includes('อัปโหลดไฟล์ Excel')) {
    openBatchOpsModal('upload');
    return;
  }
  if (text.includes('🕒') || text.includes('batch operations history') || text.includes('historię operacji') || text.includes('ประวัติการดำเนินการ')) {
    openBatchOpsModal('history');
    return;
  }
  const messagesArea = document.getElementById('messagesArea');
  if (messagesArea && messagesArea.children.length === 0) {
    activeUserPreferences = {};
    updateFocusBarUI();
  }
  const input = document.getElementById('chatInput');
  if (input) {
    input.value = text;
    handleSendMessage();
  }
}

// Topbar Quick Actions (Unified Handler)
function triggerQuickAction(action) {
  if (action === 'batch') {
    openBatchOpsModal('upload');
    return;
  }
  if (action === 'properties' || action === 'crud') {
    openPropertyCrudModal();
    return;
  }
  if (action === 'report') {
    if (currentMode !== 'admin' && currentMode !== 'superadmin') {
      setAgentMode('admin', false);
    }
    const input = document.getElementById('chatInput');
    if (input) {
      input.value = "Generate a comprehensive investment and structural dossier PDF report for 15 Sukhumvit Residences";
      handleSendMessage();
    }
    return;
  }
  if (action === 'listing') {
    if (isAdminLoggedIn()) {
      openPropertyFormModal('create');
    } else {
      if (currentMode !== 'admin') setAgentMode('admin', false);
      const input = document.getElementById('chatInput');
      if (input) {
        input.value = 'Propose adding a new luxury pool villa listing in Phuket';
        handleSendMessage();
      }
    }
    return;
  }
}

// Topbar Property Search
function executeTopSearch() {
  const searchInput = document.getElementById('topSearchInput');
  const query = searchInput.value.trim();
  if (!query) return;

  const input = document.getElementById('chatInput');
  if (input) {
    input.value = `Search properties in ${query}`;
    handleSendMessage();
    searchInput.value = '';
  }
}

// Second duplicate triggerQuickAction removed (unified above)

// Input Keydown (Enter to send, Shift+Enter for newline)
function handleInputKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    handleSendMessage();
  }
}

// Core Message Dispatcher
async function handleSendMessage() {
  if (isTypingActive) return; // Prevent overlapping requests while streaming
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  const stagedImages = currentMode === 'admin' ? pendingPropertyImages.slice() : [];
  if (!text && stagedImages.length === 0) return;

  if (currentMode === 'admin' && !isAdminLoggedIn()) {
    openAdminLoginModal();
    appendMessageRow('assistant', '⚠️ **Authentication required.** Please sign in with your staff email/password to use Admin Team chat.');
    return;
  }

  // Hide hero if visible
  const hero = document.getElementById('welcomeHero');
  if (hero) hero.style.display = 'none';

  let attachedUrls = [];
  if (stagedImages.length) {
    setPropertyImageStatus('Uploading images…');
    try {
      attachedUrls = await uploadPropertyImages(stagedImages);
    } catch (err) {
      setPropertyImageStatus('');
      showPropertyImageError(err.message || 'Image upload failed.');
      return;
    }
    clearPropertyImageTray();
  }

  // Render user message bubble
  appendMessageRow('user', text, attachedUrls);
  input.value = '';
  input.style.height = 'auto';

  // Render typing indicator
  const typingId = appendTypingIndicator();
  scrollToBottom();

  const endpoint = currentMode === 'public' ? API_ROUTES.publicChat : API_ROUTES.adminChat;
  const payload = {
    session_id: currentSessionId,
    message: text,
    site_origin: activeTenantOrigin,
    language: currentLanguage
  };
  if (currentMode === 'admin' && attachedUrls.length) {
    payload.attached_images = attachedUrls;
  }
  const pendingIds = Object.keys(confirmationTokens);
  const shortConfirm = /^(yes|ok|okay|confirm|apply)(\s+(confirm|yes|ok|apply))?$/i.test((text || '').trim());
  if (currentMode !== 'public' && shortConfirm && pendingIds.length) {
    const pendingId = pendingIds[pendingIds.length - 1];
    payload.action = 'confirm';
    payload.pending_action_id = pendingId;
    payload.confirmation_token = confirmationTokens[pendingId];
  }

  // Long client timeout (10 min) so multi-turn LLM + Firestore never shows premature "unavailable"
  const CHAT_TIMEOUT_MS = 600000;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), CHAT_TIMEOUT_MS);

  const fallbackByLang = {
    en: "I'm sorry — that information is not available right now from our verified production data. Please try again in a moment, or share a location/budget and I will search only verified listings.",
    pl: "Przepraszam — obecnie nie mogę pobrać zweryfikowanych danych produkcyjnych. Spróbuj ponownie za chwilę albo podaj lokalizację/budżet — wyszukam wyłącznie wśród dostępnych ofert.",
    th: "ขออภัยครับ ขณะนี้ยังดึงข้อมูลจากระบบ production ไม่ได้ชั่วคราว กรุณาลองใหม่อีกครั้ง หรือแจ้งทำเล/งบประมาณ เพื่อให้ผมค้นหาจากรายการที่ตรวจสอบแล้วเท่านั้นครับ"
  };

  try {
    const headers = currentMode === 'admin'
      ? adminAuthHeaders({ 'Content-Type': 'application/json' })
      : { 'Content-Type': 'application/json' };
    const res = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
      signal: controller.signal
    });

    removeTypingIndicator(typingId);

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      if (res.status === 401 && currentMode === 'admin') {
        localStorage.removeItem(ADMIN_TOKEN_KEY);
        localStorage.removeItem(ADMIN_USER_KEY);
        updateAdminLoginUI();
        openAdminLoginModal();
        appendMessageRow('assistant', '⚠️ **Session expired.** Please sign in again.');
        return;
      }
      const fb = fallbackByLang[currentLanguage] || fallbackByLang.en;
      appendMessageRow('assistant', errData.detail ? `⚠️ **Error (${res.status}):** ${formatApiError(errData.detail, 'Error')}` : fb);
      return;
    }

    const data = await res.json();
    saveToHistory(text, currentSessionId, currentMode);
    await appendAssistantResponse(data, true); // true = live streaming typewriter effect

  } catch (err) {
    removeTypingIndicator(typingId);
    const fb = fallbackByLang[currentLanguage] || fallbackByLang.en;
    if (err && err.name === 'AbortError') {
      appendMessageRow('assistant', fb);
    } else {
      appendMessageRow('assistant', fb);
    }
  } finally {
    clearTimeout(timeoutId);
    scrollToBottom();
  }
}

// Render Assistant Response with Live Typewriter Effect like GPT
async function appendAssistantResponse(data, animate = false) {
  const text = data.text || '';
  const properties = data.properties || [];
  const quickReplies = data.quick_replies || [];
  const requiresConfirm = data.requires_confirmation || false;
  const pendingActionId = data.pending_action_id || null;
  const confirmationToken = data.confirmation_token || '';
  const confirmViaChatOnly = data.confirm_via_chat_only === true;
  if (pendingActionId && confirmationToken) rememberConfirmationToken(pendingActionId, confirmationToken);

  // Sync active language if backend changed or confirmed language
  if (data.language && data.language !== currentLanguage) {
    setAppLanguage(data.language, true);
  }

  // PDF brochures are provided as direct clickable links in the chat message
  // and card, rather than forcing an automatic silent browser file download.

  // Append empty assistant message row
  const row = appendMessageRow('assistant', '');
  const bubble = row.querySelector('.message-bubble');

  // Update active user preferences from response
  if (data.preferences) {
    activeUserPreferences = data.preferences;
    updateFocusBarUI();
  }

  // If in Public User mode and property results were returned, display inline AI focus pill showing what was fetched
  const hasProperties = data.type === 'property_cards' || (data.properties && data.properties.length > 0);
  if (currentMode === 'public' && hasProperties && data.preferences && data.preferences.focus_validated && data.preferences.focus_summary && data.preferences.what_we_fetched && data.preferences.what_we_fetched.length > 0) {
    const focusPill = document.createElement('div');
    focusPill.className = 'inline-assistant-focus';
    focusPill.onclick = () => openFocusDrawer();
    focusPill.innerHTML = `
      <span class="inline-focus-icon">🎯</span>
      <span class="inline-focus-label">AI Focus:</span>
      <span class="inline-focus-text">${escapeHtml(data.preferences.focus_summary)}</span>
      <span class="inline-focus-action">Matching properties</span>
    `;
    bubble.appendChild(focusPill);
  }

  // If tools were used by Claude, prepend tool indicator pills
  const toolsUsed = data.tools_used || [];
  if (toolsUsed && toolsUsed.length > 0) {
    const toolContainer = document.createElement('div');
    toolContainer.className = 'tool-usage-container';
    toolsUsed.forEach(tc => {
      const pill = document.createElement('div');
      pill.className = 'tool-usage-pill';
      const argsStr = tc.args ? JSON.stringify(tc.args).replace(/"/g, '').replace(/^{|}$/g, '') : '';
      const preview = argsStr.length > 45 ? argsStr.substring(0, 45) + '...' : argsStr;
      pill.innerHTML = `<span>🛠️ Tool:</span> <span class="tool-name">${escapeHtml(tc.name || '')}</span> ${preview ? `<span style="color:var(--text-dim); font-size:11px;">(${escapeHtml(preview)})</span>` : ''}`;
      toolContainer.appendChild(pill);
    });
    bubble.appendChild(toolContainer);
  }

  const textContainer = document.createElement('div');
  bubble.appendChild(textContainer);

  if (animate && text) {
    isTypingActive = true;
    await streamTextToElement(textContainer, text, 14); // 14ms smooth word streaming
    isTypingActive = false;
  } else {
    textContainer.innerHTML = parseMarkdown(text);
  }

  // Detect if a report was generated and render prominent download card
  const directLink = data.download_link || null;
  const reportMatch = text.match(/\/api\/v1\/admin\/reports\/([a-zA-Z0-9_\-\.]+)/i) || 
                      text.match(/(tajlandia_(?:lead_)?report_[a-zA-Z0-9_]+\.(pdf|xlsx|docx|csv))/i);
  if (directLink || reportMatch) {
    let downloadUrl = directLink || (reportMatch[0].startsWith('/') ? reportMatch[0] : `/api/v1/admin/reports/${reportMatch[1]}`);
    // Strip signed-download tokens from display filename
    if (/\/reports\/download\//i.test(downloadUrl)) {
      downloadUrl = directLink;
    }
    const filename = (downloadUrl || '').split('/').pop().split('?')[0] || 'report.pdf';
    const isExcel = filename.endsWith('.xlsx');
    const isWord = filename.endsWith('.docx');
    const isCsv = filename.endsWith('.csv');
    const fileLabel = isExcel ? 'Excel Spreadsheet' : (isWord ? 'Word Document' : (isCsv ? 'CSV Dataset' : 'PDF Report'));
    const fileIcon = isExcel ? '📊' : (isWord ? '📝' : (isCsv ? '📑' : '📄'));

    const reportCard = document.createElement('div');
    reportCard.className = 'report-download-card';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-download-report';
    btn.innerHTML = `⬇ Download ${fileLabel}`;
    btn.addEventListener('click', () => downloadAdminProtectedFile(downloadUrl, filename, btn));
    reportCard.innerHTML = `
      <div class="report-download-left">
        <div class="report-file-icon">${fileIcon}</div>
        <div>
          <div class="report-file-title">Tajlandia Branded Property Intelligence Report</div>
          <div class="report-file-desc">${escapeHtml(filename)} (${fileLabel})</div>
        </div>
      </div>
    `;
    reportCard.appendChild(btn);
    bubble.appendChild(reportCard);
  }

  // Detect if a luxury brochure / dossier link was provided and render prominent action card
  const brochureLink = data.pdf_url || data.download_url || null;
  if (brochureLink && !directLink && !reportMatch) {
    const brochureCard = document.createElement('div');
    brochureCard.className = 'report-download-card';
    brochureCard.style.marginTop = '10px';
    const bTitle = data.property_name || (activeTenantBrand ? `${activeTenantBrand} Official Brochure` : 'Official Luxury Brochure');
    const bFilename = (brochureLink.split('/').pop() || 'brochure.pdf').split('?')[0];
    brochureCard.innerHTML = `
      <div class="report-download-left">
        <div class="report-file-icon">📄</div>
        <div>
          <div class="report-file-title">${escapeHtml(bTitle)}</div>
          <div class="report-file-desc">Official 8-Page Presentation Brochure (PDF)</div>
        </div>
      </div>
    `;
    if (isProtectedAdminDownloadUrl(brochureLink)) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn-download-report';
      btn.style.cssText = 'background:#0284C7; color:#ffffff; font-weight:600;';
      btn.innerHTML = '⬇ Download PDF';
      btn.addEventListener('click', () => downloadAdminProtectedFile(brochureLink, bFilename, btn));
      brochureCard.appendChild(btn);
    } else {
      const a = document.createElement('a');
      a.href = brochureLink;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.className = 'btn-download-report';
      a.style.cssText = 'background:#0284C7; color:#ffffff; font-weight:600;';
      a.innerHTML = '🔗 Open / Download PDF';
      brochureCard.appendChild(a);
    }
    bubble.appendChild(brochureCard);
  }

  // If property cards returned, render structured grid
  if (properties && properties.length > 0) {
    const grid = document.createElement('div');
    grid.className = 'properties-grid';

    properties.forEach((p, idx) => {
      const card = document.createElement('div');
      card.className = 'property-item-card';

      // Cache property in memory for instant modal lookups
      if (p.property_id) {
        propertyCache[p.property_id] = p;
      }

      // Pick image with robust fallback
      const rawImg = (p.images && p.images[0]) || (p.image_urls && p.image_urls[0]) || '';
      const fallbackImg = FALLBACK_VILLA_IMAGES[idx % FALLBACK_VILLA_IMAGES.length];
      const imgUrl = rawImg || fallbackImg;

      const name = p.name || p.property_name || 'Thailand Luxury Property';
      const price = p.price || p.price_display || 'Price on request';
      const loc = p.location || p.district || 'Thailand';
      const beds = (p.bedrooms && !/^beds:/i.test(String(p.bedrooms)))
        ? `${p.bedrooms} Beds`
        : (p.property_type || 'Beds: on request');
      const area = p.area || '';
      const photoCount = (p.images && p.images.length) ? `${p.images.length} Photos` : 'Verified';
      const propId = p.property_id || '';

      card.innerHTML = `
        <div class="property-item-img-wrap" onclick="openLightbox(${jsArg(imgUrl)}, ${jsArg(name + ' - ' + price)})">
          <img 
            src="${escapeHtml(imgUrl)}" 
            class="property-item-img" 
            alt="${escapeHtml(name)}" 
            onerror="this.onerror=null; this.src='${fallbackImg}';" 
          />
          <div class="property-badge-verified">Verified Listing</div>
          <div class="property-badge-photos">📷 ${photoCount}</div>
        </div>
        <div class="property-item-body">
          <div class="property-item-title">${escapeHtml(name)}</div>
          <div class="property-item-price">${escapeHtml(price)}</div>
          <div class="property-item-meta">
            <span class="meta-chip">📍 ${escapeHtml(loc)}</span>
            <span class="meta-chip">🛏️ ${escapeHtml(beds)}</span>
            ${area ? `<span class="meta-chip">📐 ${escapeHtml(area)}</span>` : ''}
          </div>
          <div class="property-item-actions">
            <button class="btn-card-action btn-card-map" title="View on Google Maps" onclick="openPropertyMap(${jsArg(propId)})">📍 Map</button>
            <button class="btn-card-action btn-card-cal" title="Request a viewing" onclick="openPropertyCalendar(${jsArg(propId)})">📅 Tour</button>
            <button class="btn-card-action btn-card-email" title="Request specs by email" onclick="openPropertyEmail(${jsArg(propId)})">✉️ Email</button>
            <button class="btn-card-action btn-card-secondary" title="Download Verified PDF" onclick="generatePdfForProperty(${jsArg(propId)})">📄 PDF</button>
            <button class="btn-card-action btn-card-primary" onclick="inquireProperty(${jsArg(propId || name)})">Inquire</button>
          </div>
        </div>
      `;
      grid.appendChild(card);
    });

    bubble.appendChild(grid);
  }

  // If two-step confirmation required (Admin Agent)
  if (requiresConfirm && pendingActionId && !confirmViaChatOnly) {
    const actionCard = document.createElement('div');
    actionCard.className = 'action-confirmation-card';
    actionCard.id = `action-card-${pendingActionId}`;
    if (confirmationToken) actionCard.dataset.confirmationToken = confirmationToken;

    actionCard.innerHTML = `
      <div class="action-card-header">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path></svg>
        Action Requires Confirmation
      </div>
      <div class="action-card-body">
        <strong>Staged Pending Action:</strong> <code>${pendingActionId}</code><br/>
        This write action has been safely staged in Firestore and will not execute until confirmed.
      </div>
      <div class="action-card-buttons">
        <button class="btn-confirm" onclick="confirmAction(${jsArg(pendingActionId)})">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"></polyline></svg>
          Confirm & Execute
        </button>
        <button class="btn-reject" onclick="rejectAction(${jsArg(pendingActionId)})">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
          Reject
        </button>
      </div>
    `;
    bubble.appendChild(actionCard);
  }

  // Quick replies
  if (quickReplies && quickReplies.length > 0) {
    const wrap = document.createElement('div');
    wrap.className = 'quick-replies-wrap';
    quickReplies.forEach(qr => {
      const chip = document.createElement('div');
      chip.className = 'quick-chip';
      chip.innerText = qr;
      chip.onclick = () => {
        const input = document.getElementById('chatInput');
        if (input) {
          input.value = qr;
          handleSendMessage();
        }
      };
      wrap.appendChild(chip);
    });
    bubble.appendChild(wrap);
  }

  scrollToBottom();
}

// Live Typewriter / Streaming Word Flow (like ChatGPT)
function streamTextToElement(element, fullText, speedMs = 14) {
  return new Promise((resolve) => {
    if (!fullText) {
      resolve();
      return;
    }

    const words = fullText.split(' ');
    let wordIndex = 0;
    element.innerHTML = '<span class="typing-cursor"></span>';

    const interval = setInterval(() => {
      if (wordIndex < words.length) {
        // Stream 1 to 2 words per tick for natural flow
        const currentSlice = words.slice(0, wordIndex + 1).join(' ');
        element.innerHTML = parseMarkdown(currentSlice) + '<span class="typing-cursor"></span>';
        wordIndex += 1;
        scrollToBottom();
      } else {
        clearInterval(interval);
        element.innerHTML = parseMarkdown(fullText); // Finalize clean HTML without cursor
        resolve();
      }
    }, speedMs);
  });
}

// Action Confirmation API Calls (Two-Step Admin Security)
async function confirmAction(actionId) {
  const card = document.getElementById(`action-card-${actionId}`);
  if (!isAdminLoggedIn()) {
    openAdminLoginModal();
    if (card) card.innerHTML = `<div style="color: var(--accent-red); font-size: 13px;">Sign in required before confirming.</div>`;
    return;
  }
  if (card) {
    card.innerHTML = `<div style="color: var(--gold); font-size: 13px;">Executing action...</div>`;
  }

  try {
    let token = confirmationTokens[actionId] || (card && card.dataset.confirmationToken) || '';
    if (!token) {
      const inspect = await fetch(API_ROUTES.inspectAction(actionId), {
        headers: adminAuthHeaders()
      });
      if (inspect.ok) {
        const inspected = await inspect.json();
        token = inspected.confirmation_token || '';
        if (token) rememberConfirmationToken(actionId, token);
      }
    }
    const res = await fetch(API_ROUTES.confirmAction(actionId), {
      method: 'POST',
      headers: adminAuthHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ confirmation_token: token })
    });

    const data = await res.json();
    if (res.ok) {
      if (card) {
        const toolName = data.tool_name || '';
        const auditId = data.audit_log_id || '';
        const auditDisabled = !!data.audit_logging_disabled || !auditId || String(auditId).startsWith('audit_ephemeral_');
        const realAudit = toolName && auditId && !auditDisabled;
        const auditLine = realAudit
          ? `Tool: <code>${escapeHtml(toolName)}</code> | Audit Log ID: <code>${escapeHtml(auditId)}</code>`
          : `Executed (audit logging disabled)${toolName ? ` — <code>${escapeHtml(toolName)}</code>` : ''}`;
        const verifyLine = data.verification_line
          ? `<div style="font-size: 12px; color: var(--text-muted); margin-top: 6px;">${escapeHtml(data.verification_line)}</div>`
          : '';
        card.innerHTML = `
          <div style="color: var(--accent-green); font-weight: 700; font-size: 13px; display: flex; align-items: center; gap: 6px;">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"></polyline></svg>
            Action Executed Successfully
          </div>
          <div style="font-size: 12px; color: var(--text-muted); margin-top: 6px;">
            ${auditLine}
          </div>
          ${verifyLine}
        `;
      }
    } else {
      if (card) {
        card.innerHTML = `<div style="color: var(--accent-red); font-size: 13px;">Execution failed: ${escapeHtml(formatApiError(data.detail, 'Error'))}</div>`;
      }
    }
  } catch (err) {
    if (card) card.innerHTML = `<div style="color: var(--accent-red);">Network error executing action.</div>`;
  }
}

async function rejectAction(actionId) {
  const card = document.getElementById(`action-card-${actionId}`);
  if (!isAdminLoggedIn()) {
    openAdminLoginModal();
    if (card) card.innerHTML = `<div style="color: var(--accent-red); font-size: 13px;">Sign in required before rejecting.</div>`;
    return;
  }
  try {
    const res = await fetch(API_ROUTES.rejectAction(actionId), {
      method: 'POST',
      headers: adminAuthHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ reason: 'Rejected by staff via UI' })
    });

    if (card) {
      card.innerHTML = `
        <div style="color: var(--accent-red); font-weight: 600; font-size: 13px;">
          ✕ Action Cancelled / Rejected
        </div>
      `;
    }
  } catch (err) {
    if (card) card.innerHTML = `<div style="color: var(--accent-red);">Network error rejecting action.</div>`;
  }
}

// ================= Inquire form (pop-up, no chat prompt) =================
let activeInquiryPropId = '';
let inquiryInfoCache = {};

function _inquiryInfo(lang) {
  const key = `${activeTenantOrigin}|${lang}`;
  if (inquiryInfoCache[key]) return Promise.resolve(inquiryInfoCache[key]);
  return fetch(`/api/v1/chat/contact-info?site_origin=${encodeURIComponent(activeTenantOrigin)}&language=${encodeURIComponent(lang)}`)
    .then((r) => (r.ok ? r.json() : null))
    .then((info) => {
      if (info) inquiryInfoCache[key] = info;
      return info;
    })
    .catch(() => null);
}

function inquireProperty(propId) {
  const p = propertyCache[propId] || {};
  activeInquiryPropId = propId;
  const modal = document.getElementById('inquiryModal');
  const overlay = document.getElementById('inquiryModalOverlay');
  if (!modal || !overlay) return;
  const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
  set('inquiryPropName', p.name || p.property_name || propId);
  ['inquiryError', 'inquiryResult'].forEach((id) => { const el = document.getElementById(id); if (el) el.style.display = 'none'; });
  const btn = document.getElementById('btnSubmitInquiry');
  if (btn) btn.disabled = false;
  overlay.style.display = 'block';
  modal.style.display = 'flex';

  _inquiryInfo(currentLanguage).then((info) => {
    if (!info) return;
    const c = info.inquiry_form || {};
    set('inquiryTitle', c.title); set('inquiryIntro', c.intro);
    set('inquiryNameLabel', c.name_label); set('inquiryEmailLabel', c.email_label);
    set('inquiryPhoneLabel', c.phone_label); set('inquiryMessageLabel', c.message_label);
    set('inquiryContactLine', c.contact_line);
    if (btn) btn.textContent = c.submit_label;
    const list = document.getElementById('inquiryContactList');
    if (list) {
      list.innerHTML = '';
      (info.contacts || []).forEach((ct) => {
        const row = document.createElement('div');
        const label = document.createElement('strong');
        label.textContent = `${ct.label}: `;
        const tel = document.createElement('a');
        tel.href = ct.tel_url; tel.textContent = `${c.call} ${ct.phone}`; tel.style.cssText = 'margin-right:14px; color:#e8c77a; font-weight:600; text-decoration:underline;';
        const wa = document.createElement('a');
        wa.href = ct.whatsapp_url; wa.target = '_blank'; wa.rel = 'noopener'; wa.textContent = c.whatsapp; wa.style.cssText = 'color:#6ee7a0; font-weight:600; text-decoration:underline;';
        row.append(label, tel, wa);
        list.appendChild(row);
      });
      const box = document.getElementById('inquiryContactBox');
      if (box) box.style.display = (info.contacts || []).length ? 'block' : 'none';
    }
  });
}

function closeInquiryModal() {
  const modal = document.getElementById('inquiryModal');
  const overlay = document.getElementById('inquiryModalOverlay');
  if (modal) modal.style.display = 'none';
  if (overlay) overlay.style.display = 'none';
}

function submitInquiry() {
  const val = (id) => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };
  const name = val('inquiryNameInput'), email = val('inquiryEmailInput'), phone = val('inquiryPhoneInput');
  const note = val('inquiryMessageInput');
  const showErr = (msg) => { const el = document.getElementById('inquiryError'); if (el) { el.textContent = msg; el.style.display = 'block'; } };
  const info = inquiryInfoCache[`${activeTenantOrigin}|${currentLanguage}`];
  const copy = (info && info.inquiry_form) || {};
  document.getElementById('inquiryError').style.display = 'none';
  if (!email && !phone) { showErr(copy.need_contact || 'Please enter an email address or a phone number so we can reach you.'); return; }
  if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { showErr(copy.bad_email || 'Please enter a valid email address.'); return; }

  const p = propertyCache[activeInquiryPropId] || {};
  const btn = document.getElementById('btnSubmitInquiry');
  if (btn) btn.disabled = true;
  fetch('/api/v1/chat/public', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: currentSessionId || `inquiry_${Date.now()}`,
      site_origin: activeTenantOrigin,
      language: currentLanguage,
      contact_name: name || null,
      contact_email: email || null,
      contact_phone: phone || null,
      property_id: activeInquiryPropId || null,
      notes: `Inquire form for ${p.name || p.property_name || activeInquiryPropId}${note ? ': ' + note : ''}`,
    }),
  })
    .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
    .then((data) => {
      const el = document.getElementById('inquiryResult');
      if (el) {
        el.textContent = data.lead_saved === false ? (copy.not_stored || 'Online requests are not switched on yet. Please call or WhatsApp us.') : (copy.success || 'Thank you! Our agent will reach out to you shortly.');
        el.style.display = 'block';
      }
    })
    .catch(() => { showErr('Something went wrong. Please try again, or call or WhatsApp us.'); if (btn) btn.disabled = false; });
}

function openPropertyMap(propId) {
  const p = propertyCache[propId] || {};
  const modal = document.getElementById('mapModal');
  const overlay = document.getElementById('mapModalOverlay');
  if (!modal || !overlay) return;

  const titleEl = document.getElementById('mapModalTitle');
  const subtitleEl = document.getElementById('mapModalSubtitle');
  const mapFrame = document.getElementById('mapFrame');
  const coordsText = document.getElementById('mapCoordsText');
  const locationText = document.getElementById('mapLocationText');
  const btnDirect = document.getElementById('btnGoogleMapsDirect');
  const proxGrid = document.getElementById('proximityGrid');

  const name = p.name || p.property_name || propId || 'Thailand Property';
  const displayAddress = p.address ? `${p.address}${p.location ? ' (' + p.location + ')' : ''}` : (p.location || p.district || 'Thailand');
  const loc = p.location || p.district || 'Thailand';
  const lat = p.latitude;
  const lng = p.longitude;

  if (titleEl) titleEl.textContent = `📍 ${name}`;
  if (subtitleEl) subtitleEl.textContent = `${displayAddress} • Ref: ${propId}`;
  
  if (lat && lng) {
    if (coordsText) coordsText.innerHTML = `<strong>${lat.toFixed(4)}° N, ${lng.toFixed(4)}° E</strong>`;
  } else {
    if (coordsText) coordsText.innerHTML = `<strong>${escapeHtml(displayAddress)}</strong>`;
  }
  if (locationText) locationText.textContent = displayAddress;

  // Set embed URL (dynamic Google Maps embed)
  const queryStr = (lat && lng) ? `${lat},${lng}` : `${name} ${p.address || loc} Thailand`;
  const embedUrl = p.maps_embed_url || `https://maps.google.com/maps?q=${encodeURIComponent(queryStr)}&t=&z=14&ie=UTF8&iwloc=&output=embed`;
  if (mapFrame) mapFrame.src = embedUrl;

  // Direct Google Maps navigation URL
  const directMapsUrl = p.maps_url || `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(queryStr)}`;
  if (btnDirect) btnDirect.href = directMapsUrl;

  // Render nearby landmarks
  if (proxGrid) {
    proxGrid.innerHTML = '';
    const landmarks = (p.nearby_landmarks && p.nearby_landmarks.length > 0) ? p.nearby_landmarks : [];
    const proxSection = proxGrid.closest('.proximity-section');
    if (proxSection) proxSection.style.display = landmarks.length ? '' : 'none';
    landmarks.forEach(lm => {
      const chip = document.createElement('div');
      chip.className = 'proximity-chip';
      chip.innerHTML = `
        <span class="proximity-chip-name">${escapeHtml(lm.name)}</span>
        <span class="proximity-chip-dist">⚡ ${lm.distance_km} km (~${lm.drive_time_mins} min)</span>
      `;
      proxGrid.appendChild(chip);
    });
  }

  overlay.style.display = 'block';
  modal.style.display = 'flex';
}

function closeMapModal() {
  const modal = document.getElementById('mapModal');
  const overlay = document.getElementById('mapModalOverlay');
  if (modal) modal.style.display = 'none';
  if (overlay) overlay.style.display = 'none';
  const mapFrame = document.getElementById('mapFrame');
  if (mapFrame) mapFrame.src = '';
}

// ================= Google Calendar Viewing Modal Handler =================
let activeCalendarPropId = '';

function openPropertyCalendar(propId) {
  const p = propertyCache[propId] || {};
  activeCalendarPropId = propId;
  const modal = document.getElementById('calendarModal');
  const overlay = document.getElementById('calendarModalOverlay');
  if (!modal || !overlay) return;

  const propNameEl = document.getElementById('calModalPropName');
  const propIdEl = document.getElementById('calModalPropId');
  const dateInput = document.getElementById('calDateInput');

  if (propNameEl) propNameEl.textContent = p.name || p.property_name || propId;
  if (propIdEl) propIdEl.textContent = propId;

  // Default date = tomorrow
  if (dateInput && !dateInput.value) {
    const tomorrow = new Date();
    tomorrow.setDate(tomorrow.getDate() + 1);
    dateInput.value = tomorrow.toISOString().split('T')[0];
  }

  overlay.style.display = 'block';
  modal.style.display = 'flex';
}

function closeCalendarModal() {
  const modal = document.getElementById('calendarModal');
  const overlay = document.getElementById('calendarModalOverlay');
  if (modal) modal.style.display = 'none';
  if (overlay) overlay.style.display = 'none';
}

function submitScheduleViewing() {
  const p = propertyCache[activeCalendarPropId] || {};
  const dateInput = document.getElementById('calDateInput');
  const timeInput = document.getElementById('calTimeInput');
  const nameInput = document.getElementById('calNameInput');
  const emailInput = document.getElementById('calEmailInput');
  const notesInput = document.getElementById('calNotesInput');

  const pDate = dateInput ? dateInput.value : '';
  const pTime = timeInput ? timeInput.value : '14:00';
  const cName = nameInput ? nameInput.value.trim() : '';
  const cEmail = emailInput ? emailInput.value.trim() : '';
  const cNotes = notesInput ? notesInput.value.trim() : '';

  if (!pDate) {
    alert('Please choose a preferred viewing date.');
    return;
  }
  if (!cEmail) {
    alert('Please enter your email so we can dispatch the Google Calendar invitation.');
    return;
  }

  closeCalendarModal();

  // If in admin mode, switch to public discovery
  if (currentMode !== 'public') setAgentMode('public', false);

  const input = document.getElementById('chatInput');
  if (input) {
    const pName = p.name || p.property_name || activeCalendarPropId;
    input.value = `Schedule a viewing for ${activeCalendarPropId} (${pName}) on ${pDate} at ${pTime}. My name is ${cName || 'Client'}, email is ${cEmail}. ${cNotes ? 'Note: ' + cNotes : ''}`;
    handleSendMessage();
  }
}

// ================= Gmail Property Brochure Modal Handler =================
let activeEmailPropId = '';

function openPropertyEmail(propId) {
  const p = propertyCache[propId] || {};
  activeEmailPropId = propId;
  const modal = document.getElementById('emailModal');
  const overlay = document.getElementById('emailModalOverlay');
  if (!modal || !overlay) return;

  const propNameEl = document.getElementById('emailModalPropName');
  const propIdEl = document.getElementById('emailModalPropId');

  if (propNameEl) propNameEl.textContent = p.name || p.property_name || propId;
  if (propIdEl) propIdEl.textContent = propId;

  overlay.style.display = 'block';
  modal.style.display = 'flex';
}

function closeEmailModal() {
  const modal = document.getElementById('emailModal');
  const overlay = document.getElementById('emailModalOverlay');
  if (modal) modal.style.display = 'none';
  if (overlay) overlay.style.display = 'none';
}

function submitSendBrochure() {
  const p = propertyCache[activeEmailPropId] || {};
  const emailInput = document.getElementById('emailBrochureInput');
  const nameInput = document.getElementById('emailClientNameInput');

  const email = emailInput ? emailInput.value.trim() : '';
  const name = nameInput ? nameInput.value.trim() : '';

  if (!email || !email.includes('@')) {
    alert('Please provide a valid email address.');
    return;
  }

  closeEmailModal();

  if (currentMode !== 'public') setAgentMode('public', false);

  const input = document.getElementById('chatInput');
  if (input) {
    const pName = p.name || p.property_name || activeEmailPropId;
    input.value = `Please email the verified brochure and floor plans for ${activeEmailPropId} (${pName}) to my email: ${email}${name ? ' (Name: ' + name + ')' : ''}.`;
    handleSendMessage();
  }
}

// Global escape key to close open modals
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeMapModal();
    closeCalendarModal();
    closeEmailModal();
    closeInquiryModal();
    closeBatchOpsModal();
    closeFocusDrawer();
    closeLightbox();
    closePropertyCrudModal();
    closePropertyFormModal();
    closePropertyDeleteModal();
    closeSuperAdminModal();
  }
});

// History Session Management & Full History Loading
function saveToHistory(firstQuery, sessionId, mode) {
  const key = `tajlandia_sessions_registry`;
  let sessions = JSON.parse(localStorage.getItem(key) || '[]');
  
  const existingIdx = sessions.findIndex(s => s.id === sessionId);
  const title = firstQuery.length > 32 ? firstQuery.substring(0, 32) + '...' : firstQuery;

  if (existingIdx >= 0) {
    sessions[existingIdx].lastUpdated = Date.now();
  } else {
    sessions.unshift({
      id: sessionId,
      title: title,
      mode: mode,
      lastUpdated: Date.now()
    });
  }

  // Keep up to 15 recent conversations
  if (sessions.length > 15) sessions = sessions.slice(0, 15);
  localStorage.setItem(key, JSON.stringify(sessions));
  renderHistoryList();
}

async function loadHistorySession(sessionId, mode) {
  currentSessionId = sessionId;
  localStorage.setItem(`tajlandia_${mode}_session`, sessionId);
  setAgentMode(mode, false);

  const hero = document.getElementById('welcomeHero');
  if (hero) hero.style.display = 'none';

  const messagesArea = document.getElementById('messagesArea');
  if (messagesArea) messagesArea.innerHTML = '';

  const endpoint = mode === 'public' 
    ? API_ROUTES.publicHistory(sessionId) 
    : API_ROUTES.adminHistory(sessionId);

  try {
    // The admin history endpoint is staff-only, so it needs the Bearer token like every other admin call.
    const res = await fetch(endpoint, mode === 'admin' ? { headers: adminAuthHeaders() } : undefined);
    if (!res.ok) {
      const reason = (res.status === 401 || res.status === 403)
        ? 'Your session has expired. Please sign in again to see this conversation.'
        : `Could not load this conversation (error ${res.status}).`;
      if (messagesArea) messagesArea.innerHTML = `<div class="batch-empty-placeholder" style="padding:16px;color:#f87171;">${escapeHtml(reason)}</div>`;
      return;
    }

    const data = await res.json();
    if (mode === 'public' && data.preferences) {
      activeUserPreferences = data.preferences;
      updateFocusBarUI();
    } else {
      activeUserPreferences = {};
      updateFocusBarUI();
    }

    const messages = data.messages || [];

    for (const msg of messages) {
      if (msg.role === 'user') {
        appendMessageRow('user', msg.content);
      } else if (msg.role === 'assistant') {
        const props = msg.properties || (msg.structured_data && msg.structured_data.properties) || [];
        if (Array.isArray(props)) {
          props.forEach(p => {
            if (p && typeof p === 'object' && p.property_id) {
              propertyCache[p.property_id] = p;
            }
          });
        }
        await appendAssistantResponse({
          text: msg.content,
          properties: Array.isArray(props) && props.length > 0 && typeof props[0] === 'object' ? props : [],
          tools_used: msg.tool_calls || [],
          requires_confirmation: false,
          download_link: msg.download_link || null,
          preferences: activeUserPreferences
        }, false);
      }
    }
  } catch (err) {
    console.warn('Could not load session history:', err);
  } finally {
    renderHistoryList();
    scrollToBottom();
  }
}

function deleteHistorySession(e, sessionId) {
  e.stopPropagation();
  const key = `tajlandia_sessions_registry`;
  let sessions = JSON.parse(localStorage.getItem(key) || '[]');
  sessions = sessions.filter(s => s.id !== sessionId);
  localStorage.setItem(key, JSON.stringify(sessions));

  if (currentSessionId === sessionId) {
    startNewChat();
  } else {
    renderHistoryList();
  }
}

function renderHistoryList() {
  const key = `tajlandia_sessions_registry`;
  const sessions = JSON.parse(localStorage.getItem(key) || '[]');
  const container = document.getElementById('historyList');
  if (!container) return;

  container.innerHTML = '';
  if (sessions.length === 0) {
    container.innerHTML = `<div style="font-size: 12px; color: var(--text-dim); padding: 8px;">No previous chats yet</div>`;
    return;
  }

  sessions.forEach((s) => {
    const isActive = s.id === currentSessionId;
    const div = document.createElement('div');
    div.className = `history-item ${isActive ? 'active' : ''}`;

    div.innerHTML = `
      <div class="history-item-left">
        <span style="font-size: 13px;">💬</span>
        <span class="history-item-title">${escapeHtml(s.title)}</span>
        <span class="history-item-badge ${s.mode === 'admin' ? 'admin' : ''}">
          ${s.mode === 'admin' ? 'Admin' : 'User'}
        </span>
      </div>
      <button class="history-item-delete" title="Delete conversation" onclick="deleteHistorySession(event, ${jsArg(s.id)})">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
      </button>
    `;

    div.onclick = () => loadHistorySession(s.id, s.mode);
    container.appendChild(div);
  });
}

// UI Helpers
function appendMessageRow(role, content, imageUrls) {
  const area = document.getElementById('messagesArea');
  const row = document.createElement('div');
  row.className = `message-row ${role}`;

  const avatar = document.createElement('div');
  avatar.className = 'message-avatar';
  avatar.innerHTML = role === 'user' 
    ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>`
    : `✦`;

  const bubble = document.createElement('div');
  bubble.className = 'message-bubble';
  if (role === 'user' && imageUrls && imageUrls.length) {
    const thumbs = document.createElement('div');
    thumbs.className = 'message-image-row';
    imageUrls.forEach((url) => {
      const img = document.createElement('img');
      img.src = url;
      img.alt = 'Attached property image';
      thumbs.appendChild(img);
    });
    bubble.appendChild(thumbs);
  }
  if (content) {
    const textNode = document.createElement('div');
    textNode.innerHTML = parseMarkdown(content);
    bubble.appendChild(textNode);
  }

  if (role === 'assistant') {
    row.appendChild(avatar);
    row.appendChild(bubble);
  } else {
    row.appendChild(bubble);
    row.appendChild(avatar);
  }

  area.appendChild(row);
  return row;
}

function appendTypingIndicator() {
  const area = document.getElementById('messagesArea');
  const id = 'typing-' + Date.now();
  const row = document.createElement('div');
  row.className = 'message-row assistant';
  row.id = id;

  row.innerHTML = `
    <div class="message-avatar">✦</div>
    <div class="message-bubble" style="padding: 10px 16px;">
      <div class="typing-dots">
        <span class="dot"></span>
        <span class="dot"></span>
        <span class="dot"></span>
      </div>
    </div>
  `;
  area.appendChild(row);
  return id;
}

function removeTypingIndicator(id) {
  const el = document.getElementById(id);
  if (el) el.remove();
}

function scrollToBottom() {
  const viewport = document.getElementById('chatViewport');
  if (viewport) {
    viewport.scrollTop = viewport.scrollHeight;
  }
}

// Lightweight Markdown Formatter
function parseMarkdown(md) {
  if (!md) return '';
  let html = escapeHtml(md);

  // Bold
  html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  // Italic
  html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
  // Headers
  html = html.replace(/^### (.*$)/gim, '<h4 style="color:var(--gold); margin: 10px 0 4px;">$1</h4>');
  html = html.replace(/^## (.*$)/gim, '<h3 style="color:#ffffff; margin: 14px 0 6px;">$1</h3>');
  html = html.replace(/^# (.*$)/gim, '<h2 style="color:#ffffff; margin: 16px 0 8px;">$1</h2>');
  // Unordered list items
  html = html.replace(/^\- (.*$)/gim, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>)/gim, '<ul>$1</ul>');
  // Line breaks
  html = html.replace(/\n\n/g, '</p><p>');
  html = html.replace(/\n/g, '<br/>');

  return `<p>${html}</p>`;
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

// Safe argument for an inline handler: onclick="fn(${jsArg(value)})".
// escapeHtml alone is NOT enough inside '...': the browser decodes &#039; back to ' before running the
// handler, so a crafted property id/name could break out and run script. JSON.stringify makes a proper
// JS string literal; escapeHtml then protects the attribute.
function jsArg(value) {
  return escapeHtml(JSON.stringify(String(value == null ? '' : value)));
}

// FastAPI `detail` may be a string, a validation-error array, or an object; never show "[object Object]".
function formatApiErrorDetail(detail) {
  if (detail == null) return '';
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map(d => (d && d.msg) ? `${(d.loc || []).slice(1).join('.') || 'request'}: ${d.msg}` : String(d)).join('; ');
  }
  if (typeof detail === 'object') {
    const parts = [detail.message].filter(Boolean);
    if (detail.fields && typeof detail.fields === 'object') {
      parts.push(Object.entries(detail.fields).map(([k, v]) => `${k}: ${v}`).join('; '));
    }
    return parts.join(' - ') || JSON.stringify(detail);
  }
  return String(detail);
}

// ==========================================================================
// User Preferences & Search Focus Dynamic Management
// ==========================================================================
function updateFocusBarUI() {
  const bar = document.getElementById('userFocusBar');
  const chipsContainer = document.getElementById('focusChipsContainer');
  const badge = document.getElementById('focusCountBadge');
  const summaryEl = document.getElementById('focusDrawerSummary');
  const fetchedListEl = document.getElementById('fetchedItemsList');
  const criteriaTableEl = document.getElementById('criteriaTable');

  if (!bar) return;

  // Only show in public mode when preferences exist
  const hasPrefs = activeUserPreferences && (
    activeUserPreferences.location ||
    activeUserPreferences.property_type ||
    activeUserPreferences.bedrooms ||
    activeUserPreferences.budget_display ||
    activeUserPreferences.purpose ||
    (activeUserPreferences.features && activeUserPreferences.features.length > 0)
  );

  if (currentMode !== 'public' || !hasPrefs) {
    bar.style.display = 'none';
    return;
  }

  bar.style.display = 'flex';

  // 1. Render chips in top focus bar
  if (chipsContainer) {
    chipsContainer.innerHTML = '';
    
    if (activeUserPreferences.location) {
      chipsContainer.appendChild(createFocusChip('📍', activeUserPreferences.location));
    }
    if (activeUserPreferences.property_type) {
      chipsContainer.appendChild(createFocusChip('🏡', activeUserPreferences.property_type));
    }
    if (activeUserPreferences.bedrooms) {
      chipsContainer.appendChild(createFocusChip('🛏️', activeUserPreferences.bedrooms));
    }
    if (activeUserPreferences.budget_display) {
      chipsContainer.appendChild(createFocusChip('💰', activeUserPreferences.budget_display));
    }
    if (activeUserPreferences.purpose) {
      chipsContainer.appendChild(createFocusChip('🎯', activeUserPreferences.purpose));
    }
    if (activeUserPreferences.features && Array.isArray(activeUserPreferences.features)) {
      activeUserPreferences.features.forEach(f => {
        chipsContainer.appendChild(createFocusChip('✨', f));
      });
    }
  }

  // 2. Count badge for "What We Fetched"
  const fetchedCount = activeUserPreferences.what_we_fetched ? activeUserPreferences.what_we_fetched.length : 0;
  if (badge) badge.innerText = fetchedCount;

  // 3. Drawer Summary
  if (summaryEl) {
    summaryEl.innerText = activeUserPreferences.focus_summary || 'Ready to discover your ideal Thailand property as you share your requirements.';
  }

  // 4. Drawer What We Fetched List
  if (fetchedListEl) {
    fetchedListEl.innerHTML = '';
    const items = activeUserPreferences.what_we_fetched || [];
    if (items.length === 0) {
      fetchedListEl.innerHTML = '<div class="empty-fetched-placeholder">No criteria mentioned yet. Tell me your preferred location, villa type, or budget!</div>';
    } else {
      items.forEach(item => {
        const row = document.createElement('div');
        row.className = 'fetched-item-row';
        row.innerText = item;
        fetchedListEl.appendChild(row);
      });
    }
  }

  // 5. Drawer Active Criteria Matcher Table
  if (criteriaTableEl) {
    criteriaTableEl.innerHTML = '';
    const fields = [
      { label: 'Target Destination', val: activeUserPreferences.location },
      { label: 'Property Type', val: activeUserPreferences.property_type },
      { label: 'Bedrooms', val: activeUserPreferences.bedrooms },
      { label: 'Budget Target', val: activeUserPreferences.budget_display },
      { label: 'Primary Purpose', val: activeUserPreferences.purpose },
      { label: 'Key Features', val: activeUserPreferences.features ? activeUserPreferences.features.join(', ') : null },
      { label: 'Timeline', val: activeUserPreferences.timeline }
    ];

    fields.forEach(f => {
      if (f.val) {
        const r = document.createElement('div');
        r.className = 'criteria-row';
        r.innerHTML = `
          <span class="criteria-label">${escapeHtml(f.label)}</span>
          <span class="criteria-val">${escapeHtml(f.val)}</span>
        `;
        criteriaTableEl.appendChild(r);
      }
    });
  }
}

function createFocusChip(icon, text) {
  const chip = document.createElement('span');
  chip.className = 'focus-chip';
  chip.innerHTML = `<span>${icon}</span> <span>${escapeHtml(text)}</span>`;
  chip.title = 'Click to inspect what was fetched from chat';
  chip.onclick = () => openFocusDrawer();
  return chip;
}

function toggleFocusDrawer() {
  const drawer = document.getElementById('focusDrawer');
  if (!drawer) return;

  if (drawer.style.display === 'none' || !drawer.style.display) {
    openFocusDrawer();
  } else {
    closeFocusDrawer();
  }
}

function openFocusDrawer() {
  const drawer = document.getElementById('focusDrawer');
  const overlay = document.getElementById('focusDrawerOverlay');
  if (drawer) drawer.style.display = 'block';
  if (overlay) overlay.style.display = 'block';
}

function closeFocusDrawer() {
  const drawer = document.getElementById('focusDrawer');
  const overlay = document.getElementById('focusDrawerOverlay');
  if (drawer) drawer.style.display = 'none';
  if (overlay) overlay.style.display = 'none';
}

function resetUserPreferences() {
  activeUserPreferences = {};
  updateFocusBarUI();
  closeFocusDrawer();
  const input = document.getElementById('chatInput');
  if (input) {
    input.value = 'Show all featured luxury properties in Thailand';
    handleSendMessage();
  }
}

// ==========================================================================
// Admin Bulk Property File Operations & 6-Day Undo Modal Logic
// ==========================================================================
let selectedBatchFile = null;
let selectedBatchActionType = 'update';

function initBatchDropzone() {
  const dropzone = document.getElementById('batchDropzone');
  if (!dropzone) return;

  ['dragenter', 'dragover'].forEach(name => {
    dropzone.addEventListener(name, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add('dragover');
    }, false);
  });

  ['dragleave', 'drop'].forEach(name => {
    dropzone.addEventListener(name, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove('dragover');
    }, false);
  });

  dropzone.addEventListener('drop', (e) => {
    const dt = e.dataTransfer;
    const files = dt.files;
    if (files && files.length > 0) {
      setBatchFile(files[0]);
    }
  }, false);
}

function openBatchOpsModal(initialTab = 'upload') {
  if (!isAdminLoggedIn()) {
    if (currentMode !== 'admin') setAgentMode('admin', false);
    openAdminLoginModal();
    appendMessageRow('assistant', '⚠️ **Authentication required.** Sign in as staff before using bulk operations.');
    return;
  }
  const modal = document.getElementById('batchModal');
  const overlay = document.getElementById('batchModalOverlay');
  if (modal) modal.style.display = 'flex';
  if (overlay) overlay.style.display = 'block';
  switchBatchTab(initialTab);
}

function closeBatchOpsModal() {
  const modal = document.getElementById('batchModal');
  const overlay = document.getElementById('batchModalOverlay');
  if (modal) modal.style.display = 'none';
  if (overlay) overlay.style.display = 'none';
}

function switchBatchTab(tabName) {
  const uploadTabBtn = document.getElementById('tabBtnBatchUpload');
  const historyTabBtn = document.getElementById('tabBtnBatchHistory');
  const uploadView = document.getElementById('batchTabUploadView');
  const historyView = document.getElementById('batchTabHistoryView');
  const btnExecute = document.getElementById('btnExecuteBatch');

  if (tabName === 'history') {
    if (uploadTabBtn) uploadTabBtn.classList.remove('active');
    if (historyTabBtn) historyTabBtn.classList.add('active');
    if (uploadView) uploadView.style.display = 'none';
    if (historyView) historyView.style.display = 'block';
    if (btnExecute) btnExecute.style.display = 'none';
    loadBatchHistory(false);
  } else {
    if (uploadTabBtn) uploadTabBtn.classList.add('active');
    if (historyTabBtn) historyTabBtn.classList.remove('active');
    if (uploadView) uploadView.style.display = 'block';
    if (historyView) historyView.style.display = 'none';
    if (btnExecute) btnExecute.style.display = 'inline-flex';
  }
}

function selectBatchAction(actionType) {
  selectedBatchActionType = actionType;
  ['update', 'delete', 'create'].forEach(type => {
    const card = document.getElementById(`cardAction${type.charAt(0).toUpperCase() + type.slice(1)}`);
    if (card) {
      card.classList.toggle('selected', type === actionType);
      const radio = card.querySelector('input[type="radio"]');
      if (radio) radio.checked = (type === actionType);
    }
  });

  const btnText = document.getElementById('btnExecuteBatchText');
  if (btnText) {
    // The backend stages batch changes for confirmation; nothing is applied until "Confirm".
    if (actionType === 'delete') btnText.innerText = 'Review Archive';
    else if (actionType === 'create') btnText.innerText = 'Review Create';
    else btnText.innerText = 'Review Update';
  }
}

function onBatchFileSelected(event) {
  const files = event.target.files;
  if (files && files.length > 0) {
    setBatchFile(files[0]);
  }
}

function setBatchFile(file) {
  selectedBatchFile = file;
  const dropzoneText = document.getElementById('batchDropzoneText');
  const chip = document.getElementById('batchFileSelectedChip');
  const fileNameEl = document.getElementById('batchFileName');
  const fileSizeEl = document.getElementById('batchFileSize');

  if (dropzoneText) dropzoneText.style.display = 'none';
  if (chip) chip.style.display = 'inline-flex';
  if (fileNameEl) fileNameEl.innerText = file.name;
  if (fileSizeEl) fileSizeEl.innerText = (file.size / 1024).toFixed(1) + ' KB';
}

function clearSelectedBatchFile(event) {
  if (event) event.stopPropagation();
  selectedBatchFile = null;
  const fileInput = document.getElementById('batchFileInput');
  if (fileInput) fileInput.value = '';
  const dropzoneText = document.getElementById('batchDropzoneText');
  const chip = document.getElementById('batchFileSelectedChip');
  if (dropzoneText) dropzoneText.style.display = 'block';
  if (chip) chip.style.display = 'none';
}

// --- Add/Create from an offer document: preview, then confirm -------------------------------------------
let pendingDocumentImport = null;

function renderBatchDocumentPreview(data) {
  const resultBox = document.getElementById('batchExecutionResult');
  if (!resultBox) return;
  pendingDocumentImport = { id: data.pending_action_id, token: data.confirmation_token };
  const num = (v) => (v === null || v === undefined || v === '') ? '—' : Number(v).toLocaleString('en-US');
  const range = (a, b) => (a === null || a === undefined) ? '—' : (b !== null && b !== undefined && b !== a ? `${num(a)}–${num(b)}` : num(a));
  const rows = (data.listings || []).map((l) => `
    <tr>
      <td>${escapeHtml(l.name || '')}</td>
      <td>${escapeHtml(l.province || '—')}</td>
      <td>${escapeHtml(l.property_type || '—')}</td>
      <td>${escapeHtml(l.status || '—')}</td>
      <td>${range(l.price_min, l.price_max)} THB</td>
      <td>${range(l.bedrooms_min, l.bedrooms_max)}</td>
      <td>${range(l.area_sqm_min, l.area_sqm_max)}</td>
      <td style="color:${l.ready ? '#4ade80' : '#fbbf24'};">${escapeHtml(l.outcome || '')}</td>
    </tr>`).join('');
  resultBox.className = 'batch-result-box';
  resultBox.style.display = 'flex';
  resultBox.style.flexDirection = 'column';
  resultBox.innerHTML = `
    <div style="font-weight: 700; font-size: 13.5px;">📄 Review before creating</div>
    <div style="font-size: 12px;">${escapeHtml(data.message || '')}</div>
    <div style="font-size: 12px;"><strong>Target website(s):</strong> ${escapeHtml((data.websites || []).join(', ') || 'none selected')}</div>
    <div style="overflow-x:auto;">
      <table style="width:100%; font-size:11.5px; border-collapse:collapse;">
        <thead><tr style="text-align:left; opacity:.8;">
          <th>Name</th><th>Province</th><th>Type</th><th>Status</th><th>Price</th><th>Beds</th><th>m²</th><th>Result</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div style="font-size: 11.5px; opacity: 0.85;">Only values written in the document are used. Nothing is saved until you confirm.</div>
    <div style="margin-top: 6px; display: flex; gap: 8px;">
      <button class="btn-refresh-history" id="btnConfirmDocImport" ${data.ready_count ? '' : 'disabled'}
        onclick="confirmBatchDocumentImport()">Confirm &amp; create ${Number(data.ready_count || 0)}</button>
      <button class="btn-refresh-history" onclick="discardBatchDocumentImport()">Discard</button>
    </div>`;
}

async function confirmBatchDocumentImport() {
  const resultBox = document.getElementById('batchExecutionResult');
  if (!pendingDocumentImport || !resultBox) return;
  const { id, token } = pendingDocumentImport;
  const btn = document.getElementById('btnConfirmDocImport');
  if (btn) { btn.disabled = true; btn.textContent = 'Creating…'; }
  try {
    const res = await fetch(`/api/v1/admin/actions/${encodeURIComponent(id)}/confirm`, {
      method: 'POST',
      headers: adminAuthHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ confirmation_token: token })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(formatApiError(data.detail, 'Could not create the listings'));
    const out = data.execution_result || {};
    const created = (out.created || []).map((c) =>
      `<li><strong>${escapeHtml(c.name)}</strong> <code>${escapeHtml(c.property_id)}</code> — undo id <code>${escapeHtml(c.operation_id || '')}</code></li>`).join('');
    const skipped = (out.skipped || []).map((s) =>
      `<li>${escapeHtml(s.name)}: ${escapeHtml(s.reason)}</li>`).join('');
    pendingDocumentImport = null;
    resultBox.className = 'batch-result-box success';
    resultBox.innerHTML = `
      <div style="font-weight: 700; font-size: 13.5px;">✅ ${(out.created || []).length} listing(s) created and verified in the database</div>
      <ul style="margin: 4px 0 0 18px; font-size: 12px;">${created}</ul>
      ${skipped ? `<div style="font-weight:600; font-size:12px; margin-top:6px;">Not created</div><ul style="margin: 2px 0 0 18px; font-size: 12px;">${skipped}</ul>` : ''}`;
    clearSelectedBatchFile();
    loadBatchHistory(false);
  } catch (err) {
    console.error('Document import confirm error:', err);
    resultBox.className = 'batch-result-box error';
    resultBox.innerHTML = `
      <div style="font-weight: 700;">❌ Nothing was created</div>
      <div>${escapeHtml(err.message || 'Unknown error')}</div>`;
    pendingDocumentImport = null;
  }
}

async function discardBatchDocumentImport() {
  const resultBox = document.getElementById('batchExecutionResult');
  const current = pendingDocumentImport;
  pendingDocumentImport = null;
  if (current) {
    try {
      await fetch(`/api/v1/admin/actions/${encodeURIComponent(current.id)}/reject`, {
        method: 'POST',
        headers: adminAuthHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ reason: 'Discarded from the bulk upload preview' })
      });
    } catch (e) { /* the pending action expires on its own */ }
  }
  if (resultBox) { resultBox.style.display = 'none'; resultBox.innerHTML = ''; }
}

async function submitBatchOperation() {
  if (!selectedBatchFile) {
    alert('Please select or drop a property file (.xlsx, .xls, .csv, .pdf, .docx, .json).');
    return;
  }

  const btn = document.getElementById('btnExecuteBatch');
  const resultBox = document.getElementById('batchExecutionResult');
  const notesInput = document.getElementById('batchNotesInput');

  if (selectedBatchActionType === 'delete' &&
      !confirm(`Archive every listing in "${selectedBatchFile.name}"?\n\nArchived listings disappear from the websites and the chatbot. You will review the list and can undo for 6 days.`)) {
    return;
  }

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span>⏳</span> <span>Preparing proposal...</span>`;
  }
  if (resultBox) {
    resultBox.style.display = 'none';
    resultBox.className = 'batch-result-box';
  }

  const formData = new FormData();
  formData.append('file', selectedBatchFile);
  // The backend form field is named `action`.
  formData.append('action', selectedBatchActionType);
  if (notesInput && notesInput.value.trim()) {
    formData.append('notes', notesInput.value.trim());
  }

  try {
    if (!isAdminLoggedIn()) {
      openAdminLoginModal();
      throw new Error('Sign in required for batch operations');
    }
    const res = await fetch('/api/v1/admin/properties/batch-file', {
      method: 'POST',
      headers: adminAuthHeaders(),
      body: formData
    });

    const data = await res.json();
    if (!res.ok) {
      throw new Error((typeof formatApiErrorDetail === 'function' ? formatApiErrorDetail(data.detail) : null) || (typeof formatApiError === 'function' ? formatApiError(data.detail, 'Batch operation failed') : 'Batch operation failed'));
    }

    if (data.status === 'PREVIEW') {
      renderBatchDocumentPreview(data);
      return;
    }

    const pendingId = data.pending_action_id || data.action_id || '';
    if (resultBox) {
      resultBox.className = 'batch-result-box success';
      resultBox.style.display = 'flex';
      resultBox.innerHTML = `
        ${pendingId ? `
        <div style="font-weight: 700; font-size: 13.5px;">📝 Change proposed - nothing has been applied yet</div>
        <div style="white-space: pre-wrap;">${escapeHtml(data.message || data.text || '')}</div>
        <div id="action-card-${escapeHtml(pendingId)}" style="margin-top: 6px; display: flex; gap: 8px;">
          <button class="btn-confirm" onclick="confirmAction(${jsArg(pendingId)})">Confirm</button>
          <button class="btn-reject" onclick="rejectAction(${jsArg(pendingId)})">Reject</button>
          <button class="btn-refresh-history" onclick="switchBatchTab('history')">History & Undo</button>
        </div>` : `
        <div style="font-weight: 700; font-size: 13.5px;">⚡ Operation Applied Instantly!</div>
        <div><strong>Operation ID:</strong> <code>${escapeHtml(data.operation_id)}</code></div>
        <div><strong>Items Affected:</strong> ${data.items_affected || 0} properties modified in Firestore.</div>
        <div style="font-size: 11.5px; opacity: 0.9;">
          🛡️ <strong>6-Day Rollback Guaranteed:</strong> Pre-modification snapshot saved until <strong>${new Date(data.expires_at).toLocaleString()}</strong>.
        </div>
        <div style="margin-top: 6px; display: flex; gap: 8px;">
          <button class="btn-refresh-history" onclick="switchBatchTab('history')">View in History & Undo</button>
        </div>`}
        </div>
      `;
    }

    // Clear selected file after success
    clearSelectedBatchFile();
    if (notesInput) notesInput.value = '';

    // Refresh history counter in badge
    loadBatchHistory(false);

  } catch (err) {
    console.error('Batch error:', err);
    if (resultBox) {
      resultBox.className = 'batch-result-box error';
      resultBox.style.display = 'flex';
      resultBox.innerHTML = `
        <div style="font-weight: 700;">❌ Operation Failed</div>
        <div>${escapeHtml(err.message || 'Unknown error executing batch')}</div>
      `;
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"></path></svg>
        <span id="btnExecuteBatchText">Execute Instantly</span>
      `;
      selectBatchAction(selectedBatchActionType);
    }
  }
}

async function loadBatchHistory(manual = false) {
  const listEl = document.getElementById('batchHistoryList');
  const badge = document.getElementById('batchHistoryCountBadge');

  if (manual && listEl) {
    listEl.innerHTML = '<div class="batch-loading-placeholder">Refreshing operations from database...</div>';
  }

  try {
    if (!isAdminLoggedIn()) {
      if (listEl) listEl.innerHTML = '<div class="batch-empty-placeholder">Sign in as staff to view batch history.</div>';
      return;
    }
    const res = await fetch('/api/v1/admin/operations/history?limit=50', {
      headers: adminAuthHeaders()
    });

    if (!res.ok) throw new Error('Failed to fetch history');

    const data = await res.json();
    const operations = data.operations || [];

    if (badge) badge.innerText = operations.length;

    if (!listEl) return;

    if (operations.length === 0) {
      listEl.innerHTML = '<div class="batch-empty-placeholder">No batch operations recorded yet. Upload an Excel or PDF above to execute your first batch operation!</div>';
      return;
    }

    listEl.innerHTML = '';
    operations.forEach(op => {
      const item = document.createElement('div');
      item.className = 'batch-history-item';

      const isExpired = op.is_expired;
      const isUndone = op.status === 'undone' || op.status === 'REVERTED';
      const canUndo = !isExpired && !isUndone && op.can_undo !== false;
      const changes = Array.isArray(op.changes) ? op.changes : [];
      const changeLines = changes.slice(0, 5).map(c =>
        `<li>${c.property_name ? `<strong>${escapeHtml(c.property_name)}</strong> — ` : ''}${escapeHtml(c.text || '')}</li>`).join('');
      const moreChanges = changes.length > 5 ? `<li>+${changes.length - 5} more changes</li>` : '';
      const names = Array.isArray(op.property_names) ? op.property_names : [];

      let badgeClass = 'active';
      if (isExpired) badgeClass = 'expired';
      else if (op.time_remaining && op.time_remaining.includes('h remaining') && !op.time_remaining.includes('d')) badgeClass = 'expiring';

      item.innerHTML = `
        <div class="history-item-top">
          <div style="display: flex; align-items: center; gap: 8px;">
            <span class="history-op-badge ${escapeHtml(op.operation_type)}">${escapeHtml(op.operation_label || op.operation_type)}</span>
            <span class="history-op-summary">${escapeHtml(op.summary || '')}</span>
          </div>
          <span class="countdown-badge ${badgeClass}">
            ${isExpired ? '❌ Expired' : '⏳ ' + escapeHtml(op.time_remaining || 'Active')}
          </span>
        </div>

        <div class="history-item-meta">
          <span>📄 <strong>File:</strong> ${escapeHtml(op.file_name || 'batch')}</span>
          <span>🎯 <strong>Affected:</strong> ${op.items_affected || 0} listings${names.length ? ': ' + escapeHtml(names.join(', ')) : ''}</span>
          <span>📅 <strong>Executed:</strong> ${new Date(op.created_at).toLocaleString()}${op.actor ? ' by ' + escapeHtml(op.actor) : ''}</span>
          <span class="history-op-id">${escapeHtml(op.operation_id)}</span>
        </div>
        ${changeLines ? `<ul class="history-op-changes">${changeLines}${moreChanges}</ul>` : ''}

        <div style="display: flex; align-items: center; justify-content: space-between; margin-top: 4px; padding-top: 6px; border-top: 1px solid rgba(255,255,255,0.05);">
          <span style="font-size: 11px; color: var(--text-dim);">
            Expires: ${new Date(op.expires_at).toLocaleDateString()}
          </span>
          ${isUndone 
            ? `<span class="status-undone-tag">Restored ✓</span>` 
            : (canUndo 
                ? `<button class="btn-undo" onclick="executeBatchUndo(${jsArg(op.operation_id)}, ${Number(op.items_affected) || 0})">
                     ↺ Rollback & Undo
                   </button>` 
                : `<span style="font-size: 11.5px; color: var(--text-dim);">Expired (> 6 days)</span>`
              )
          }
        </div>
      `;

      listEl.appendChild(item);
    });

  } catch (err) {
    console.error('History load error:', err);
    if (listEl) {
      listEl.innerHTML = `<div class="batch-empty-placeholder" style="color: #f87171;">Failed to load history: ${escapeHtml(err.message)}</div>`;
    }
  }
}

async function executeBatchUndo(operationId, count) {
  const ok = confirm(`Are you sure you want to rollback batch operation ${operationId}?\n\nThis will instantly restore ${count} properties to their exact pre-change snapshot.`);
  if (!ok) return;

  try {
    if (!isAdminLoggedIn()) {
      openAdminLoginModal();
      throw new Error('Sign in required to undo batch operations');
    }
    const res = await fetch(`/api/v1/admin/operations/${encodeURIComponent(operationId)}/undo`, {
      method: 'POST',
      headers: adminAuthHeaders()
    });

    const data = await res.json();
    if (!res.ok) {
      throw new Error(formatApiError(data.detail, 'Rollback failed'));
    }

    alert(`✅ Success: Rollback completed!\n${data.restored_count || count} properties have been restored to their pre-change snapshot.`);
    loadBatchHistory(true);

  } catch (err) {
    console.error('Undo error:', err);
    alert(`❌ Rollback error: ${err.message || 'Could not complete rollback'}`);
  }
}

const pendingPropertyImages = [];
const PROPERTY_IMAGE_MAX_BYTES = 10 * 1024 * 1024;
const PROPERTY_IMAGE_EXT = /\.(jpe?g|png|webp)$/i;

function openPropertyImagePicker() {
  if (currentMode !== 'admin') return;
  const input = document.getElementById('propertyImageInput');
  if (input) input.click();
}

function clearPropertyImageTray() {
  pendingPropertyImages.splice(0, pendingPropertyImages.length);
  renderPropertyImageTray();
}

function setPropertyImageStatus(text) {
  const tray = document.getElementById('imageAttachTray');
  if (!tray) return;
  let note = tray.querySelector('.image-thumb-status');
  if (!text) {
    if (note) note.remove();
    return;
  }
  if (!note) {
    note = document.createElement('div');
    note.className = 'image-thumb-status';
    tray.appendChild(note);
  }
  note.textContent = text;
}

function showPropertyImageError(text) {
  const tray = document.getElementById('imageAttachTray');
  if (!tray) return;
  tray.style.display = 'flex';
  let note = tray.querySelector('.image-attach-error');
  if (!note) {
    note = document.createElement('div');
    note.className = 'image-attach-error';
    tray.appendChild(note);
  }
  note.textContent = text;
}

function renderPropertyImageTray() {
  const tray = document.getElementById('imageAttachTray');
  if (!tray) return;
  tray.innerHTML = '';
  if (!pendingPropertyImages.length) {
    tray.style.display = 'none';
    return;
  }
  tray.style.display = 'flex';
  pendingPropertyImages.forEach((item, index) => {
    const thumb = document.createElement('div');
    thumb.className = 'image-thumb';
    const img = document.createElement('img');
    img.src = item.previewUrl;
    img.alt = item.name || 'Selected image';
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'image-thumb-remove';
    remove.textContent = '×';
    remove.title = 'Remove image';
    remove.onclick = () => {
      pendingPropertyImages.splice(index, 1);
      renderPropertyImageTray();
    };
    thumb.appendChild(img);
    thumb.appendChild(remove);
    tray.appendChild(thumb);
  });
}

function addPropertyImageFiles(fileList) {
  if (currentMode !== 'admin') return;
  const incoming = Array.from(fileList || []);
  if (pendingPropertyImages.length + incoming.length > 10) {
    showPropertyImageError('Upload between 1 and 10 images.');
    return;
  }
  let error = '';
  incoming.forEach((file) => {
    const name = file.name || '';
    if (!PROPERTY_IMAGE_EXT.test(name)) {
      error = 'Only jpg, jpeg, png, and webp images are allowed.';
      return;
    }
    if (file.size > PROPERTY_IMAGE_MAX_BYTES) {
      error = 'Each image must be 10 MB or smaller.';
      return;
    }
    pendingPropertyImages.push({
      file,
      name,
      previewUrl: URL.createObjectURL(file),
    });
  });
  renderPropertyImageTray();
  if (error) showPropertyImageError(error);
}

async function uploadPropertyImages(items) {
  const body = new FormData();
  items.forEach((item) => body.append('files', item.file, item.name));
  const res = await fetch('/api/v1/admin/property-images/upload', {
    method: 'POST',
    headers: adminAuthHeaders(),
    body,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = formatApiError(data.detail, 'Image upload failed.');
    throw new Error(typeof detail === 'string' ? detail : 'Image upload failed.');
  }
  const files = data.files || [];
  if (!files.length) throw new Error('Image upload failed.');
  return files.map((file) => file.url).filter(Boolean);
}

function initPropertyImageAttach() {
  const input = document.getElementById('propertyImageInput');
  if (input) {
    input.addEventListener('change', () => {
      addPropertyImageFiles(input.files);
      input.value = '';
    });
  }
  const dropTarget = document.getElementById('bottomInputContainer');
  if (!dropTarget) return;
  ['dragenter', 'dragover'].forEach((name) => {
    dropTarget.addEventListener(name, (event) => {
      if (currentMode !== 'admin') return;
      event.preventDefault();
      dropTarget.classList.add('drag-over');
    });
  });
  ['dragleave', 'drop'].forEach((name) => {
    dropTarget.addEventListener(name, (event) => {
      if (currentMode !== 'admin') return;
      event.preventDefault();
      dropTarget.classList.remove('drag-over');
    });
  });
  dropTarget.addEventListener('drop', (event) => {
    if (currentMode !== 'admin') return;
    addPropertyImageFiles(event.dataTransfer && event.dataTransfer.files);
  });
}

initPropertyImageAttach();




// ==========================================================================
// 1. Image Lightbox Modal Handlers
// ==========================================================================
function openLightbox(src, caption) {
  const modal = document.getElementById('lightboxModal');
  const img = document.getElementById('lightboxImg');
  const cap = document.getElementById('lightboxCaption');
  if (!modal || !img) return;
  img.src = src || '';
  if (cap) cap.innerText = caption || '';
  modal.style.display = 'flex';
}

function closeLightbox() {
  const modal = document.getElementById('lightboxModal');
  if (modal) modal.style.display = 'none';
  const img = document.getElementById('lightboxImg');
  if (img) img.src = '';
}

// ==========================================================================
// 2. Verified Property Brochure PDF Generation
// ==========================================================================
async function generatePdfForProperty(propId, btnEl) {
  if (!propId) {
    alert('Property ID is required to generate brochure PDF.');
    return;
  }
  const btn = btnEl || (window.event && window.event.target ? window.event.target.closest('button') : null);
  const originalLabel = btn ? btn.innerHTML : '📄 PDF';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '⏳ Generating...';
  }

  try {
    const url = `/api/v1/chat/brochure/${encodeURIComponent(propId)}?site_origin=${encodeURIComponent(activeTenantOrigin)}&lang=${encodeURIComponent(currentLanguage)}&session_id=${encodeURIComponent(currentSessionId || '')}`;
    const res = await fetch(url);
    if (!res.ok) {
      throw new Error(`Failed to generate brochure PDF (${res.status})`);
    }
    const blob = await res.blob();
    const filename = `Brochure_${propId}.pdf`;
    triggerBlobDownload(blob, filename);
  } catch (err) {
    console.error('Brochure generation error:', err);
    alert(`❌ Could not generate PDF: ${err.message || 'Unknown error'}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = originalLabel;
    }
  }
}

// ==========================================================================
// 3. Property Portfolio CRUD Management
// ==========================================================================
let crudCurrentPage = 0;
const crudPageSize = 10;
let crudTotalCount = 0;
let crudPropertiesCache = [];
let activeDeletePropertyId = '';
let activeDeletePropertyName = '';
let selectedDeleteType = 'soft';

function openPropertyCrudModal() {
  if (!isAdminLoggedIn()) {
    openAdminLoginModal();
    appendMessageRow('assistant', '⚠️ **Authentication required.** Sign in as staff before accessing Property CRUD portfolio.');
    return;
  }
  const modal = document.getElementById('propertyCrudModal');
  const overlay = document.getElementById('propertyCrudModalOverlay');
  if (modal) modal.style.display = 'flex';
  if (overlay) overlay.style.display = 'block';
  loadCrudProperties(0);
}

function closePropertyCrudModal() {
  const modal = document.getElementById('propertyCrudModal');
  const overlay = document.getElementById('propertyCrudModalOverlay');
  if (modal) modal.style.display = 'none';
  if (overlay) overlay.style.display = 'none';
}

async function loadCrudProperties(page = 0) {
  crudCurrentPage = Math.max(0, page);
  const tableBody = document.getElementById('crudPropertiesTableBody');
  const errBanner = document.getElementById('crudErrorBanner');
  const searchInput = document.getElementById('crudFilterSearch');
  const typeSelect = document.getElementById('crudFilterType');
  const statusSelect = document.getElementById('crudFilterStatus');
  const locInput = document.getElementById('crudFilterLocation');
  const pageBadge = document.getElementById('crudPageBadge');
  const paginationInfo = document.getElementById('crudPaginationInfo');
  const countSummary = document.getElementById('crudCountSummary');
  const prevBtn = document.getElementById('crudPrevBtn');
  const nextBtn = document.getElementById('crudNextBtn');

  if (errBanner) errBanner.style.display = 'none';
  if (tableBody) {
    tableBody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 30px; color: var(--text-dim);">Loading listings from database...</td></tr>`;
  }

  const offset = crudCurrentPage * crudPageSize;
  const params = new URLSearchParams({
    limit: String(crudPageSize),
    offset: String(offset)
  });

  if (typeSelect && typeSelect.value) params.set('property_type', typeSelect.value);
  if (statusSelect && statusSelect.value) params.set('status', statusSelect.value);
  if (locInput && locInput.value.trim()) params.set('location', locInput.value.trim());

  try {
    const res = await fetch(`/api/v1/admin/properties?${params.toString()}`, {
      headers: adminAuthHeaders()
    });

    if (!res.ok) {
      if (res.status === 401) {
        openAdminLoginModal();
        throw new Error('Your session has expired. Please sign in again.');
      }
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || `Server error (${res.status})`);
    }

    const data = await res.json();
    let props = data.properties || [];
    crudTotalCount = data.count || props.length;

    // Optional client-side search query filter
    const query = (searchInput?.value || '').trim().toLowerCase();
    if (query) {
      props = props.filter(p => {
        const idMatch = (p.property_id || '').toLowerCase().includes(query);
        const nameMatch = (p.property_name || p.project_name || '').toLowerCase().includes(query);
        const typeMatch = (p.property_type || '').toLowerCase().includes(query);
        return idMatch || nameMatch || typeMatch;
      });
    }

    crudPropertiesCache = props;

    // Cache properties for instant modal lookups
    props.forEach(p => {
      if (p.property_id) propertyCache[p.property_id] = p;
    });

    if (countSummary) countSummary.innerText = `${crudTotalCount} total listings`;
    if (pageBadge) pageBadge.innerText = `Page ${crudCurrentPage + 1}`;
    if (paginationInfo) paginationInfo.innerText = `Showing ${props.length} listings (Page ${crudCurrentPage + 1})`;
    if (prevBtn) prevBtn.disabled = (crudCurrentPage === 0);
    if (nextBtn) nextBtn.disabled = (props.length < crudPageSize);

    if (!tableBody) return;

    if (props.length === 0) {
      tableBody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 40px; color: var(--text-dim);">No properties found matching current filters.</td></tr>`;
      return;
    }

    tableBody.innerHTML = '';
    props.forEach(p => {
      const tr = document.createElement('tr');
      const pid = p.property_id || '';
      const name = p.property_name || p.project_name || 'Listing';
      const sub = p.project_name || '';
      const ptype = p.property_type || 'Villa';
      const loc = p.district || p.province || p.location || 'Thailand';
      const price = p.price_display || p.price || 'Contact';
      const st = p.status || 'active';

      tr.innerHTML = `
        <td><span class="crud-prop-id">${escapeHtml(pid)}</span></td>
        <td>
          <div class="crud-prop-name">${escapeHtml(name)}</div>
          ${sub && sub !== name ? `<div class="crud-prop-sub">${escapeHtml(sub)}</div>` : ''}
        </td>
        <td>${escapeHtml(ptype)}</td>
        <td>${escapeHtml(loc)}</td>
        <td style="font-weight: 600; color: var(--gold);">${escapeHtml(price)}</td>
        <td><span class="status-pill ${escapeHtml(st)}">${escapeHtml(st)}</span></td>
        <td style="text-align: right;">
          <div class="crud-btn-group" style="justify-content: flex-end;">
            <button class="btn-table-action btn-table-edit" onclick="openPropertyFormModal('edit', ${jsArg(pid)})" title="Edit property">Edit</button>
            <button class="btn-table-action btn-table-delete" onclick="openPropertyDeleteModal(${jsArg(pid)}, ${jsArg(name)})" title="Delete property">Delete</button>
            <button class="btn-table-action btn-table-view" onclick="openPropertyMap(${jsArg(pid)})" title="Map & amenities">Map</button>
          </div>
        </td>
      `;
      tableBody.appendChild(tr);
    });

  } catch (err) {
    console.error('Error loading CRUD properties:', err);
    if (errBanner) {
      errBanner.style.display = 'block';
      errBanner.innerText = `Failed to load properties: ${err.message}`;
    }
    if (tableBody) {
      tableBody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 30px; color: #f87171;">Failed to load properties: ${escapeHtml(err.message)}</td></tr>`;
    }
  }
}

function navigateCrudPage(delta) {
  loadCrudProperties(crudCurrentPage + delta);
}

function resetCrudFilters() {
  const searchInput = document.getElementById('crudFilterSearch');
  const typeSelect = document.getElementById('crudFilterType');
  const statusSelect = document.getElementById('crudFilterStatus');
  const locInput = document.getElementById('crudFilterLocation');
  if (searchInput) searchInput.value = '';
  if (typeSelect) typeSelect.value = '';
  if (statusSelect) statusSelect.value = '';
  if (locInput) locInput.value = '';
  loadCrudProperties(0);
}

// Create / Edit Property Form
function openPropertyFormModal(mode = 'create', propId = '') {
  if (!isAdminLoggedIn()) {
    openAdminLoginModal();
    return;
  }
  const modal = document.getElementById('propertyFormModal');
  const overlay = document.getElementById('propertyFormModalOverlay');
  const modeInput = document.getElementById('formPropMode');
  const origIdInput = document.getElementById('formPropOriginalId');
  const idInput = document.getElementById('formPropId');
  const iconEl = document.getElementById('formModalIcon');
  const titleEl = document.getElementById('formModalTitle');
  const btnText = document.getElementById('btnSavePropertyText');
  const errEl = document.getElementById('formModalError');

  if (errEl) errEl.style.display = 'none';
  if (modeInput) modeInput.value = mode;
  if (origIdInput) origIdInput.value = propId;

  if (mode === 'edit') {
    if (iconEl) iconEl.innerText = '✏️';
    if (titleEl) titleEl.innerText = `Edit Listing (${propId})`;
    if (btnText) btnText.innerText = 'Save Changes';
    if (idInput) {
      idInput.value = propId;
      idInput.disabled = true;
    }

    const p = propertyCache[propId] || crudPropertiesCache.find(x => x.property_id === propId) || {};
    const nameEn = document.getElementById('formPropNameEn');
    const namePl = document.getElementById('formPropNamePl');
    const ptype = document.getElementById('formPropType');
    const city = document.getElementById('formPropCity');
    const country = document.getElementById('formPropCountry');
    const pFrom = document.getElementById('formPropPriceFrom');
    const pTo = document.getElementById('formPropPriceTo');
    const curr = document.getElementById('formPropCurrency');
    const beds = document.getElementById('formPropBedrooms');
    const baths = document.getElementById('formPropBathrooms');
    const area = document.getElementById('formPropArea');
    const pool = document.getElementById('formPropPool');
    const statusEl = document.getElementById('formPropStatus');
    const imgEl = document.getElementById('formPropImage');

    if (nameEn) nameEn.value = p.property_name || p.project_name || '';
    if (namePl) namePl.value = p.project_name_pl || p.property_name || p.project_name || '';
    if (ptype && p.property_type) ptype.value = p.property_type;
    if (city) city.value = p.district || p.province || p.city || 'Phuket';
    if (country) country.value = p.country || 'Thailand';
    if (pFrom) pFrom.value = p.min_price || p.price_from || '';
    if (pTo) pTo.value = p.max_price || p.price_to || '';
    if (curr && p.currency) curr.value = p.currency;
    if (beds && p.bedrooms) beds.value = parseInt(p.bedrooms, 10) || '';
    if (baths && p.bathrooms) baths.value = parseInt(p.bathrooms, 10) || '';
    if (area) area.value = p.area || '';
    if (pool) pool.checked = Boolean(p.has_pool);
    if (statusEl && p.status) statusEl.value = p.status;
    if (imgEl) imgEl.value = (p.images && p.images[0]) || p.cover_image || '';

  } else {
    // Mode: Create
    if (iconEl) iconEl.innerText = '➕';
    if (titleEl) titleEl.innerText = 'Add New Property Listing';
    if (btnText) btnText.innerText = 'Create Property';
    if (idInput) {
      idInput.value = '';
      idInput.disabled = false;
    }

    ['formPropNameEn', 'formPropNamePl', 'formPropCity', 'formPropPriceFrom', 'formPropPriceTo', 'formPropBedrooms', 'formPropBathrooms', 'formPropArea', 'formPropImage'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
    const pool = document.getElementById('formPropPool');
    if (pool) pool.checked = false;
  }

  if (modal) modal.style.display = 'flex';
  if (overlay) overlay.style.display = 'block';
}

function closePropertyFormModal() {
  const modal = document.getElementById('propertyFormModal');
  const overlay = document.getElementById('propertyFormModalOverlay');
  if (modal) modal.style.display = 'none';
  if (overlay) overlay.style.display = 'none';
}

async function submitPropertyForm() {
  const mode = document.getElementById('formPropMode')?.value || 'create';
  const origId = document.getElementById('formPropOriginalId')?.value || '';
  const customId = (document.getElementById('formPropId')?.value || '').trim();
  const nameEn = (document.getElementById('formPropNameEn')?.value || '').trim();
  const namePl = (document.getElementById('formPropNamePl')?.value || '').trim();
  const ptype = document.getElementById('formPropType')?.value || 'Villa';
  const city = (document.getElementById('formPropCity')?.value || '').trim();
  const country = (document.getElementById('formPropCountry')?.value || 'Thailand').trim();
  const pFrom = document.getElementById('formPropPriceFrom')?.value;
  const pTo = document.getElementById('formPropPriceTo')?.value;
  const curr = document.getElementById('formPropCurrency')?.value || 'THB';
  const beds = document.getElementById('formPropBedrooms')?.value;
  const baths = document.getElementById('formPropBathrooms')?.value;
  const area = (document.getElementById('formPropArea')?.value || '').trim();
  const pool = document.getElementById('formPropPool')?.checked || false;
  const statusVal = document.getElementById('formPropStatus')?.value || 'Available';
  const imgUrl = (document.getElementById('formPropImage')?.value || '').trim();
  const errEl = document.getElementById('formModalError');
  const btn = document.getElementById('btnSaveProperty');

  if (errEl) errEl.style.display = 'none';

  if (!nameEn || !namePl || !city || !pFrom) {
    if (errEl) {
      errEl.style.display = 'block';
      errEl.innerText = 'Please fill all required fields: Project Name (EN), Project Name (PL), City, and Price From.';
    }
    return;
  }

  const selectedWebsites = [];
  document.querySelectorAll('.form-site-chk:checked').forEach(chk => {
    selectedWebsites.push(chk.value);
  });
  if (selectedWebsites.length === 0) selectedWebsites.push('tajlandia.com');

  const payload = {
    project_name_en: nameEn,
    project_name_pl: namePl,
    property_type: ptype,
    city: city,
    country: country,
    price_from: parseFloat(pFrom),
    price_to: pTo ? parseFloat(pTo) : null,
    currency: curr,
    bedrooms: beds ? parseInt(beds, 10) : null,
    bathrooms: baths ? parseInt(baths, 10) : null,
    area: area || null,
    has_pool: pool,
    status: statusVal,
    cover_image: imgUrl || null,
    websites: selectedWebsites
  };

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span>Saving...</span>';
  }

  try {
    let url = '/api/v1/admin/properties';
    let method = 'POST';

    if (mode === 'create') {
      if (customId) payload.property_id = customId;
    } else {
      url = `/api/v1/admin/properties/${encodeURIComponent(origId)}`;
      method = 'PUT';
    }

    const res = await fetch(url, {
      method: method,
      headers: adminAuthHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(payload)
    });

    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(formatApiErrorDetail(data.detail) || `Save failed (${res.status})`);
    }

    closePropertyFormModal();
    loadCrudProperties(crudCurrentPage);
    appendMessageRow('assistant', `✅ **Property Saved:** ${escapeHtml(nameEn)} (${data.property_id || origId || customId}) has been successfully ${mode === 'create' ? 'created' : 'updated'} across PostgreSQL and Firestore catalogs.`);

  } catch (err) {
    console.error('Save property error:', err);
    if (errEl) {
      errEl.style.display = 'block';
      errEl.innerText = err.message || 'Failed to save property';
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<span id="btnSavePropertyText">${mode === 'create' ? 'Create Property' : 'Save Changes'}</span>`;
    }
  }
}

// Delete Property Confirmation Dialog
function openPropertyDeleteModal(propId, propName) {
  if (!isAdminLoggedIn()) {
    openAdminLoginModal();
    return;
  }
  activeDeletePropertyId = propId;
  activeDeletePropertyName = propName || propId;

  const modal = document.getElementById('propertyDeleteModal');
  const overlay = document.getElementById('propertyDeleteModalOverlay');
  const nameEl = document.getElementById('deleteModalPropName');
  const idEl = document.getElementById('deleteModalPropId');
  const targetIdEl = document.getElementById('deleteConfirmTargetId');
  const inputEl = document.getElementById('deleteConfirmInput');
  const errEl = document.getElementById('deleteModalError');

  if (nameEl) nameEl.innerText = activeDeletePropertyName;
  if (idEl) idEl.innerText = activeDeletePropertyId;
  if (targetIdEl) targetIdEl.innerText = activeDeletePropertyId;
  if (inputEl) inputEl.value = '';
  if (errEl) errEl.style.display = 'none';

  selectDeleteType('soft');

  if (modal) modal.style.display = 'flex';
  if (overlay) overlay.style.display = 'block';
}

function closePropertyDeleteModal() {
  const modal = document.getElementById('propertyDeleteModal');
  const overlay = document.getElementById('propertyDeleteModalOverlay');
  if (modal) modal.style.display = 'none';
  if (overlay) overlay.style.display = 'none';
}

function selectDeleteType(type) {
  selectedDeleteType = type;
  const cardSoft = document.getElementById('cardSoftDelete');
  const cardHard = document.getElementById('cardHardDelete');
  const confirmBox = document.getElementById('hardDeleteConfirmBox');
  const btn = document.getElementById('btnExecuteDelete');

  if (type === 'soft') {
    if (cardSoft) {
      cardSoft.classList.add('selected');
      const r = cardSoft.querySelector('input');
      if (r) r.checked = true;
    }
    if (cardHard) {
      cardHard.classList.remove('selected');
      const r = cardHard.querySelector('input');
      if (r) r.checked = false;
    }
    if (confirmBox) confirmBox.style.display = 'none';
    if (btn) {
      btn.className = 'btn-modal-warning';
      btn.innerHTML = '<span>Archive Listing (Soft Delete)</span>';
      btn.disabled = false;
    }
  } else {
    if (cardHard) {
      cardHard.classList.add('selected');
      const r = cardHard.querySelector('input');
      if (r) r.checked = true;
    }
    if (cardSoft) {
      cardSoft.classList.remove('selected');
      const r = cardSoft.querySelector('input');
      if (r) r.checked = false;
    }
    if (confirmBox) confirmBox.style.display = 'block';
    if (btn) {
      btn.className = 'btn-modal-danger';
      btn.innerHTML = '<span>Permanently Purge Listing</span>';
      btn.disabled = true;
    }
    validateDeleteConfirmInput();
    const input = document.getElementById('deleteConfirmInput');
    if (input) setTimeout(() => input.focus(), 50);
  }
}

function validateDeleteConfirmInput() {
  const input = document.getElementById('deleteConfirmInput');
  const btn = document.getElementById('btnExecuteDelete');
  if (selectedDeleteType === 'hard' && btn) {
    const val = (input?.value || '').trim();
    btn.disabled = (val !== activeDeletePropertyId);
  }
}

async function submitDeleteProperty() {
  if (!activeDeletePropertyId) return;

  const btn = document.getElementById('btnExecuteDelete');
  const errEl = document.getElementById('deleteModalError');
  if (errEl) errEl.style.display = 'none';

  const isHard = (selectedDeleteType === 'hard');

  if (isHard) {
    const input = (document.getElementById('deleteConfirmInput')?.value || '').trim();
    if (input !== activeDeletePropertyId) {
      if (errEl) {
        errEl.style.display = 'block';
        errEl.innerText = `Confirmation failed: you must type exact ID "${activeDeletePropertyId}".`;
      }
      return;
    }
  }

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span>Deleting...</span>';
  }

  try {
    let url = `/api/v1/admin/properties/${encodeURIComponent(activeDeletePropertyId)}?hard_delete=${isHard ? 'true' : 'false'}`;
    if (isHard) {
      url += `&confirm_property_id=${encodeURIComponent(activeDeletePropertyId)}`;
    }

    const res = await fetch(url, {
      method: 'DELETE',
      headers: adminAuthHeaders()
    });

    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(formatApiErrorDetail(data.detail) || `Delete failed (${res.status})`);
    }

    closePropertyDeleteModal();
    loadCrudProperties(crudCurrentPage);
    const actionLabel = isHard ? 'Permanently Purged' : 'Archived (Soft-Deleted)';
    appendMessageRow('assistant', `🗑️ **Listing ${actionLabel}:** Property ${activeDeletePropertyId} (${activeDeletePropertyName}) was ${isHard ? 'permanently removed from databases' : 'safely archived and hidden from public search'}.`);

  } catch (err) {
    console.error('Delete error:', err);
    if (errEl) {
      errEl.style.display = 'block';
      errEl.innerText = err.message || 'Failed to delete listing';
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<span>${isHard ? 'Permanently Purge Listing' : 'Archive Listing'}</span>`;
    }
  }
}

// ==========================================================================
// 4. Super Admin Control Center Handlers
// ==========================================================================
function openSuperAdminModal() {
  if (!isAdminLoggedIn()) {
    openAdminLoginModal();
    return;
  }
  const user = getAdminUser();
  if (!user || (user.role !== 'super_admin' && user.role !== 'superadmin')) {
    alert('Access denied: Super Admin privileges required.');
    return;
  }

  const modal = document.getElementById('superAdminModal');
  const overlay = document.getElementById('superAdminModalOverlay');
  if (modal) modal.style.display = 'flex';
  if (overlay) overlay.style.display = 'block';

  switchSuperAdminTab('users');
}

function closeSuperAdminModal() {
  const modal = document.getElementById('superAdminModal');
  const overlay = document.getElementById('superAdminModalOverlay');
  if (modal) modal.style.display = 'none';
  if (overlay) overlay.style.display = 'none';
}

function switchSuperAdminTab(tab) {
  const usersBtn = document.getElementById('tabBtnSuperUsers');
  const configBtn = document.getElementById('tabBtnSuperConfig');
  const usersView = document.getElementById('superTabUsersView');
  const configView = document.getElementById('superTabConfigView');

  if (tab === 'config') {
    if (usersBtn) usersBtn.classList.remove('active');
    if (configBtn) configBtn.classList.add('active');
    if (usersView) usersView.style.display = 'none';
    if (configView) configView.style.display = 'block';
    loadSystemConfigStatus();
  } else {
    if (usersBtn) usersBtn.classList.add('active');
    if (configBtn) configBtn.classList.remove('active');
    if (usersView) usersView.style.display = 'block';
    if (configView) configView.style.display = 'none';
    loadSuperAdminUsers();
  }
}

async function loadSuperAdminUsers() {
  const tableBody = document.getElementById('superUsersTableBody');
  const searchInput = document.getElementById('superUserSearch');
  const roleSelect = document.getElementById('superUserRoleFilter');

  if (tableBody) {
    tableBody.innerHTML = `<tr><td colspan="5" style="text-align: center; padding: 24px; color: var(--text-dim);">Loading staff directory...</td></tr>`;
  }

  const params = new URLSearchParams();
  if (searchInput && searchInput.value.trim()) params.set('search', searchInput.value.trim());
  if (roleSelect && roleSelect.value) params.set('role', roleSelect.value);

  try {
    const res = await fetch(`/api/v1/auth/admin/users?${params.toString()}`, {
      headers: adminAuthHeaders()
    });

    if (!res.ok) {
      throw new Error(`Directory query failed (${res.status})`);
    }

    const users = await res.json();
    if (!tableBody) return;

    if (!Array.isArray(users) || users.length === 0) {
      tableBody.innerHTML = `<tr><td colspan="5" style="text-align: center; padding: 30px; color: var(--text-dim);">No staff users found matching criteria.</td></tr>`;
      return;
    }

    tableBody.innerHTML = '';
    users.forEach(u => {
      const tr = document.createElement('tr');
      const isSuper = (u.role === 'super_admin' || u.role === 'superadmin');
      const isAdmin = (u.role === 'admin');
      const badgeCls = isSuper ? 'superadmin' : (isAdmin ? 'admin' : 'staff');
      const isAct = u.is_active !== false && u.status !== 'blocked' && u.status !== 'deactivated';

      tr.innerHTML = `
        <td>
          <div style="font-weight: 600; color: #ffffff;">${escapeHtml(u.name || u.email)}</div>
          <div style="font-size: 11.5px; color: var(--text-muted);">${escapeHtml(u.email)}</div>
        </td>
        <td><span class="role-badge ${badgeCls}">${escapeHtml(u.role || 'staff')}</span></td>
        <td><span style="font-size: 12px; color: var(--text-muted);">${escapeHtml(u.domain || 'All domains')}</span></td>
        <td>
          <span class="status-pill ${isAct ? 'active' : 'archived'}">
            ${isAct ? 'Active ✓' : 'Inactive / Blocked'}
          </span>
        </td>
        <td style="font-size: 11.5px; color: var(--text-dim);">
          ${u.created_at ? new Date(u.created_at).toLocaleDateString() : 'N/A'}
        </td>
      `;
      tableBody.appendChild(tr);
    });

  } catch (err) {
    console.error('Super Admin user directory error:', err);
    if (tableBody) {
      tableBody.innerHTML = `<tr><td colspan="5" style="text-align: center; padding: 24px; color: #f87171;">Failed to load user directory: ${escapeHtml(err.message)}</td></tr>`;
    }
  }
}

async function loadSystemConfigStatus() {
  const grid = document.getElementById('superConfigGrid');
  if (grid) {
    grid.innerHTML = '<div class="batch-loading-placeholder">Fetching real-time configuration status...</div>';
  }

  try {
    const res = await fetch('/api/v1/admin/system/config-status', {
      headers: adminAuthHeaders()
    });

    if (!res.ok) {
      throw new Error(`Config status query failed (${res.status})`);
    }

    const cfg = await res.json();
    if (!grid) return;

    grid.innerHTML = `
      <div class="config-card">
        <span class="config-card-label">Active Tenant Domain</span>
        <span class="config-card-value">${escapeHtml(activeTenantOrigin)}</span>
        <span class="config-card-status" style="color: #34d399;">● Multi-tenant Active</span>
      </div>

      <div class="config-card">
        <span class="config-card-label">PostgreSQL Database</span>
        <span class="config-card-value">${cfg.database_configured ? 'Configured & Online' : 'Local SQLite'}</span>
        <span class="config-card-status" style="color: #34d399;">● Verified Database</span>
      </div>

      <div class="config-card">
        <span class="config-card-label">Audit Logging & Undo</span>
        <span class="config-card-value">${cfg.audit_logging ? 'Enabled (6-Day Retention)' : 'Disabled'}</span>
        <span class="config-card-status" style="color: #34d399;">● Audit Trail Active</span>
      </div>

      <div class="config-card">
        <span class="config-card-label">Luxury Brochure LLM</span>
        <span class="config-card-value">${escapeHtml(cfg.brochure_llm_model || 'claude-sonnet-5-5')}</span>
        <span class="config-card-status" style="color: #fbbf24;">● LLM Engine: ${escapeHtml(cfg.brochure_llm || 'on')}</span>
      </div>

      <div class="config-card">
        <span class="config-card-label">Brochure PDF Engine</span>
        <span class="config-card-value">${escapeHtml(cfg.brochure_engine || 'ReportLab / WeasyPrint')}</span>
        <span class="config-card-status" style="color: #34d399;">● 7 Luxury Visual Themes</span>
      </div>

      <div class="config-card">
        <span class="config-card-label">Report Storage Engine</span>
        <span class="config-card-value">${escapeHtml(cfg.report_storage || 'Ephemeral / S3')}</span>
        <span class="config-card-status" style="color: #34d399;">● Direct Download Stream</span>
      </div>
    `;

  } catch (err) {
    console.error('System config load error:', err);
    if (grid) {
      grid.innerHTML = `<div class="batch-empty-placeholder" style="color: #f87171;">Failed to load system config: ${escapeHtml(err.message)}</div>`;
    }
  }
}
