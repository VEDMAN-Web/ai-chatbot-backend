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
    whatWeFetched: "What We Fetched",
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
    whatWeFetched: "Co Pobrano",
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
    whatWeFetched: "ข้อมูลที่ดึงมา",
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
        <div class="prompt-card" onclick="sendSamplePrompt('${p.replace(/'/g, "\\'")}')">
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
        <div class="prompt-card" onclick="sendSamplePrompt('${p.replace(/'/g, "\\'")}')">
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
  const user = getAdminUser();
  const loggedIn = isAdminLoggedIn();
  if (btn) {
    btn.style.display = currentMode === 'admin' ? 'inline-flex' : 'none';
    if (label) label.innerText = loggedIn ? (user?.email ? `Logout (${user.email.split('@')[0]})` : 'Logout') : 'Staff Login';
    btn.onclick = loggedIn ? () => logoutAdminStaff() : () => openAdminLoginModal();
  }
  if (avatar && user?.name) {
    const initials = String(user.name || user.email || 'AD').trim().slice(0, 2).toUpperCase();
    avatar.innerText = initials;
    avatar.title = loggedIn ? `${user.email || user.name} (${user.role || 'staff'})` : 'Staff Account';
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
      const detail = data.detail || data.message || `Login failed (${res.status})`;
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
  currentMode = mode;

  // Sync upper-side choice buttons
  const topBtnUser = document.getElementById('topBtnUser');
  const topBtnAdmin = document.getElementById('topBtnAdmin');
  if (topBtnUser && topBtnAdmin) {
    if (mode === 'public') {
      topBtnUser.classList.add('active');
      topBtnAdmin.classList.remove('active');
    } else {
      topBtnAdmin.classList.add('active');
      topBtnUser.classList.remove('active');
    }
  }

  // Sync sidebar tabs
  const tabPub = document.getElementById('tabPublic');
  const tabAdm = document.getElementById('tabAdmin');
  const modelLabel = document.getElementById('agentModelLabel');

  if (tabPub && tabAdm) {
    if (mode === 'public') {
      tabPub.classList.add('active');
      tabAdm.classList.remove('active');
    } else {
      tabAdm.classList.add('active');
      tabPub.classList.remove('active');
    }
  }

  if (modelLabel) {
    modelLabel.innerText = mode === 'public' ? 'GPT-5.5' : 'Claude Opus 5.5';
  }

  // Toggle Admin-only controls (Batch operations button & attachment paperclip)
  const btnBatchOps = document.getElementById('btnBatchOps');
  const btnAttachment = document.getElementById('btnAttachment');
  if (btnBatchOps) btnBatchOps.style.display = (mode === 'admin') ? 'inline-flex' : 'none';
  if (btnAttachment) btnAttachment.style.display = (mode === 'admin') ? 'inline-flex' : 'none';

  // Update Hero texts & prompt cards according to current mode & language
  updateHeroAndInputUI();
  updateAdminLoginUI();

  if (mode === 'admin') {
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

// Topbar Quick Actions
function triggerQuickAction(action) {
  if (action === 'batch') {
    openBatchOpsModal('upload');
    return;
  }
  if (currentMode !== 'admin') {
    setAgentMode('admin', false);
  }
  const input = document.getElementById('chatInput');
  if (!input) return;
  if (action === 'report') {
    input.value = "Generate a comprehensive investment and structural dossier PDF report for 15 Sukhumvit Residences";
    handleSendMessage();
  } else if (action === 'listing') {
    openBatchOpsModal('upload');
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

// Quick Actions
function triggerQuickAction(action) {
  const input = document.getElementById('chatInput');
  if (!input) return;

  if (action === 'report') {
    if (currentMode !== 'admin') setAgentMode('admin', false);
    input.value = 'Generate a property report for our featured listings';
  } else if (action === 'listing') {
    if (currentMode !== 'admin') setAgentMode('admin', false);
    input.value = 'Propose adding a new luxury pool villa listing in Phuket';
  }
  handleSendMessage();
}

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
  if (!text) return;

  if (currentMode === 'admin' && !isAdminLoggedIn()) {
    openAdminLoginModal();
    appendMessageRow('assistant', '⚠️ **Authentication required.** Please sign in with your staff email/password to use Admin Team chat.');
    return;
  }

  // Hide hero if visible
  const hero = document.getElementById('welcomeHero');
  if (hero) hero.style.display = 'none';

  // Render user message bubble
  appendMessageRow('user', text);
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
      appendMessageRow('assistant', errData.detail ? `⚠️ **Error (${res.status}):** ${errData.detail}` : fb);
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
  if (currentMode === 'public' && hasProperties && data.preferences && data.preferences.focus_summary && data.preferences.what_we_fetched && data.preferences.what_we_fetched.length > 0) {
    const focusPill = document.createElement('div');
    focusPill.className = 'inline-assistant-focus';
    focusPill.onclick = () => openFocusDrawer();
    focusPill.innerHTML = `
      <span class="inline-focus-icon">🎯</span>
      <span class="inline-focus-label">AI Focus:</span>
      <span class="inline-focus-text">${escapeHtml(data.preferences.focus_summary)}</span>
      <span class="inline-focus-action">What We Fetched 📋</span>
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
      const beds = p.bedrooms ? `${p.bedrooms} Beds` : 'Villa';
      const area = p.area || '';
      const photoCount = (p.images && p.images.length) ? `${p.images.length} Photos` : 'Verified';
      const propId = p.property_id || '';

      card.innerHTML = `
        <div class="property-item-img-wrap" onclick="openLightbox('${escapeHtml(imgUrl)}', '${escapeHtml(name)} - ${escapeHtml(price)}')">
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
            <button class="btn-card-action btn-card-map" title="View on Google Maps" onclick="openPropertyMap('${escapeHtml(propId)}')">📍 Map</button>
            <button class="btn-card-action btn-card-cal" title="Schedule Tour on Google Calendar" onclick="openPropertyCalendar('${escapeHtml(propId)}')">📅 Tour</button>
            <button class="btn-card-action btn-card-email" title="Email Specs via Gmail" onclick="openPropertyEmail('${escapeHtml(propId)}')">✉️ Email</button>
            <button class="btn-card-action btn-card-secondary" title="Download Verified PDF" onclick="generatePdfForProperty('${escapeHtml(propId)}')">📄 PDF</button>
            <button class="btn-card-action btn-card-primary" onclick="inquireProperty('${escapeHtml(propId || name)}')">Inquire</button>
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
        This write action has been safely staged in PostgreSQL and will not execute until confirmed.
      </div>
      <div class="action-card-buttons">
        <button class="btn-confirm" onclick="confirmAction('${pendingActionId}')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"></polyline></svg>
          Confirm & Execute
        </button>
        <button class="btn-reject" onclick="rejectAction('${pendingActionId}')">
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
        card.innerHTML = `<div style="color: var(--accent-red); font-size: 13px;">Execution failed: ${escapeHtml(data.detail || 'Error')}</div>`;
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

function inquireProperty(propId) {
  const input = document.getElementById('chatInput');
  if (input) {
    input.value = `I am interested in ${propId}. Can you provide details and arrange a viewing?`;
    handleSendMessage();
  }
}

function requestReport(propId) {
  if (currentMode !== 'admin') setAgentMode('admin', false);
  const input = document.getElementById('chatInput');
  if (input) {
    input.value = propId ? `What are the private developer specs for ${propId}?` : 'Generate a PDF property report';
    handleSendMessage();
  }
}

function generatePdfForProperty(propId) {
  if (!propId) return;

  // Show loading state on the button
  const btn = event && event.target ? event.target : null;
  const originalText = btn ? btn.innerHTML : '';
  if (btn) {
    btn.innerHTML = '⏳ Loading...';
    btn.disabled = true;
  }

  // Request the official brochure link through the chatbot
  const prop = propertyCache[propId];
  const pName = prop ? (prop.name || prop.property_name || propId) : propId;
  const chatInput = document.getElementById('chatInput');
  if (chatInput) {
    chatInput.value = `Download brochure for ${pName}`;
    handleSendMessage();
  } else {
    const url = `/api/v1/chat/brochure/${encodeURIComponent(propId)}?site_origin=${encodeURIComponent(activeTenantOrigin)}&session_id=${encodeURIComponent(currentSessionId)}`;
    window.open(url, '_blank');
  }

  if (btn) {
    setTimeout(() => {
      btn.innerHTML = originalText;
      btn.disabled = false;
    }, 1500);
  }
}



// Image Lightbox Modal View
function openLightbox(imgUrl, caption) {
  const existing = document.getElementById('appLightbox');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.className = 'image-lightbox';
  modal.id = 'appLightbox';

  modal.innerHTML = `
    <div class="lightbox-content">
      <button class="lightbox-close" onclick="closeLightbox()">&times;</button>
      <img src="${escapeHtml(imgUrl)}" class="lightbox-img" alt="${escapeHtml(caption)}" />
      <div class="lightbox-caption">${escapeHtml(caption)}</div>
    </div>
  `;

  modal.onclick = (e) => {
    if (e.target === modal) closeLightbox();
  };

  document.body.appendChild(modal);
}

function closeLightbox() {
  const modal = document.getElementById('appLightbox');
  if (modal) modal.remove();
}

// ================= Google Maps Modal Handler =================
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
    const landmarks = (p.nearby_landmarks && p.nearby_landmarks.length > 0) ? p.nearby_landmarks : [
      { name: 'Nearest White Sand Beach', distance_km: 1.8, drive_time_mins: 4 },
      { name: 'International Airport', distance_km: 19.5, drive_time_mins: 25 },
      { name: 'Shopping & Dining Marina', distance_km: 8.2, drive_time_mins: 12 },
    ];
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
    closeBatchOpsModal();
    closeFocusDrawer();
    closeLightbox();
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
    const res = await fetch(endpoint);
    if (!res.ok) return;

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
      <button class="history-item-delete" title="Delete conversation" onclick="deleteHistorySession(event, '${escapeHtml(s.id)}')">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
      </button>
    `;

    div.onclick = () => loadHistorySession(s.id, s.mode);
    container.appendChild(div);
  });
}

// UI Helpers
function appendMessageRow(role, content) {
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
  if (content) {
    bubble.innerHTML = parseMarkdown(content);
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
    if (actionType === 'delete') btnText.innerText = 'Delete Instantly';
    else if (actionType === 'create') btnText.innerText = 'Create Instantly';
    else btnText.innerText = 'Update Instantly';
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

async function submitBatchOperation() {
  if (!selectedBatchFile) {
    alert('Please select or drop a property file (.xlsx, .xls, .csv, .pdf, .docx, .json).');
    return;
  }

  const btn = document.getElementById('btnExecuteBatch');
  const resultBox = document.getElementById('batchExecutionResult');
  const notesInput = document.getElementById('batchNotesInput');

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span>⏳</span> <span>Applying to Database...</span>`;
  }
  if (resultBox) {
    resultBox.style.display = 'none';
    resultBox.className = 'batch-result-box';
  }

  const formData = new FormData();
  formData.append('file', selectedBatchFile);
  formData.append('operation_type', selectedBatchActionType);
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
      throw new Error(data.detail || 'Batch operation failed');
    }

    if (resultBox) {
      resultBox.className = 'batch-result-box success';
      resultBox.style.display = 'flex';
      resultBox.innerHTML = `
        <div style="font-weight: 700; font-size: 13.5px;">✅ Operation Applied Instantly!</div>
        <div><strong>Operation ID:</strong> <code>${escapeHtml(data.operation_id)}</code></div>
        <div><strong>Items Affected:</strong> ${data.items_affected || 0} properties modified in Firestore & PostgreSQL.</div>
        <div style="font-size: 11.5px; opacity: 0.9;">
          🛡️ <strong>6-Day Rollback Guaranteed:</strong> Pre-modification snapshot saved until <strong>${new Date(data.expires_at).toLocaleString()}</strong>.
        </div>
        <div style="margin-top: 6px; display: flex; gap: 8px;">
          <button class="btn-refresh-history" onclick="switchBatchTab('history')">View in History & Undo</button>
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
      const isUndone = op.status === 'undone';
      const canUndo = !isExpired && !isUndone;

      let badgeClass = 'active';
      if (isExpired) badgeClass = 'expired';
      else if (op.time_remaining && op.time_remaining.includes('h remaining') && !op.time_remaining.includes('d')) badgeClass = 'expiring';

      item.innerHTML = `
        <div class="history-item-top">
          <div style="display: flex; align-items: center; gap: 8px;">
            <span class="history-op-badge ${escapeHtml(op.operation_type)}">${escapeHtml(op.operation_type)}</span>
            <span class="history-op-id">${escapeHtml(op.operation_id)}</span>
          </div>
          <span class="countdown-badge ${badgeClass}">
            ${isExpired ? '❌ Expired' : '⏳ ' + escapeHtml(op.time_remaining || 'Active')}
          </span>
        </div>

        <div class="history-item-meta">
          <span>📄 <strong>File:</strong> ${escapeHtml(op.file_name || 'batch')}</span>
          <span>🎯 <strong>Affected:</strong> ${op.items_affected || 0} listings</span>
          <span>📅 <strong>Executed:</strong> ${new Date(op.created_at).toLocaleString()}</span>
        </div>

        <div style="display: flex; align-items: center; justify-content: space-between; margin-top: 4px; padding-top: 6px; border-top: 1px solid rgba(255,255,255,0.05);">
          <span style="font-size: 11px; color: var(--text-dim);">
            Expires: ${new Date(op.expires_at).toLocaleDateString()}
          </span>
          ${isUndone 
            ? `<span class="status-undone-tag">Restored ✓</span>` 
            : (canUndo 
                ? `<button class="btn-undo" onclick="executeBatchUndo('${escapeHtml(op.operation_id)}', ${op.items_affected || 0})">
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
      throw new Error(data.detail || 'Rollback failed');
    }

    alert(`✅ Success: Rollback completed!\n${data.restored_count || count} properties have been restored to their pre-change snapshot.`);
    loadBatchHistory(true);

  } catch (err) {
    console.error('Undo error:', err);
    alert(`❌ Rollback error: ${err.message || 'Could not complete rollback'}`);
  }
}


