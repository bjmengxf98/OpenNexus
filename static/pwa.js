(function () {
  'use strict';

  const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  const isMobileDevice = window.matchMedia('(max-width: 820px)').matches &&
    (navigator.maxTouchPoints > 0 || /Android|iPhone|iPad|iPod|HarmonyOS|Mobile/i.test(navigator.userAgent));
  const INSTALL_DISMISS_KEY = 'pwa-install-dismissed-at';
  const INSTALL_DISMISS_MS = 30 * 24 * 60 * 60 * 1000;
  let installPrompt = null;
  let refreshing = false;
  let activeRegistration = null;

  function addStyles() {
    if (document.getElementById('pwa-runtime-style')) return;
    const style = document.createElement('style');
    style.id = 'pwa-runtime-style';
    style.textContent = `
      .pwa-action{position:fixed;z-index:2147483000;right:max(14px,env(safe-area-inset-right));bottom:max(14px,env(safe-area-inset-bottom));display:flex;align-items:center;gap:10px;max-width:calc(100vw - 28px);padding:10px 12px;border:1px solid rgba(99,102,241,.32);border-radius:12px;background:#fff;color:#273047;box-shadow:0 12px 34px rgba(15,23,42,.2);font:13px/1.45 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}
      .pwa-action button{border:0;border-radius:8px;padding:7px 11px;background:#4f46e5;color:#fff;font:inherit;cursor:pointer;white-space:nowrap}
      .pwa-action .pwa-close{padding:4px 7px;background:transparent;color:#7c8495;font-size:18px}
      .pwa-action.pwa-above-composer{bottom:calc(88px + env(safe-area-inset-bottom))}
      .pwa-net{position:fixed;z-index:2147483000;left:50%;top:max(12px,env(safe-area-inset-top));transform:translateX(-50%);padding:8px 13px;border-radius:999px;background:#253047;color:#fff;box-shadow:0 8px 24px rgba(0,0,0,.25);font:12px/1.4 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}
      html[data-theme="dark"] .pwa-action,.pwa-action.pwa-dark{background:#202328;color:#e8eaed;border-color:#454b55}
      @media(max-width:720px){.pwa-action{left:12px;right:12px;bottom:max(12px,env(safe-area-inset-bottom));justify-content:center}.pwa-action span{flex:1}}
    `;
    document.head.appendChild(style);
  }

  function actionBanner(id, text, actionText, onAction) {
    document.getElementById(id)?.remove();
    const box = document.createElement('div');
    box.id = id;
    box.className = 'pwa-action';
    if (document.querySelector('.composer')) box.classList.add('pwa-above-composer');
    box.setAttribute('role', 'status');
    const label = document.createElement('span');
    label.textContent = text;
    box.appendChild(label);
    if (actionText) {
      const action = document.createElement('button');
      action.type = 'button';
      action.textContent = actionText;
      action.addEventListener('click', onAction);
      box.appendChild(action);
    }
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'pwa-close';
    close.setAttribute('aria-label', '关闭');
    close.textContent = '×';
    close.addEventListener('click', () => box.remove());
    box.appendChild(close);
    document.body.appendChild(box);
    return box;
  }

  function networkNotice(text) {
    document.getElementById('pwa-network-notice')?.remove();
    const notice = document.createElement('div');
    notice.id = 'pwa-network-notice';
    notice.className = 'pwa-net';
    notice.setAttribute('role', 'status');
    notice.textContent = text;
    document.body.appendChild(notice);
    setTimeout(() => notice.remove(), 3200);
  }

  function announceUpdate(registration) {
    if (!registration.waiting) return;
    actionBanner('pwa-update-banner', 'OpenNexus 已有新版本', '立即更新', () => {
      registration.waiting.postMessage({type: 'SKIP_WAITING'});
    });
  }

  async function checkForUpdate() {
    if (!('serviceWorker' in navigator)) {
      networkNotice('当前浏览器不支持应用更新检查');
      return false;
    }
    try {
      const registration = activeRegistration || await navigator.serviceWorker.getRegistration('/');
      if (!registration) {
        networkNotice('更新服务尚未就绪，请稍后再试');
        return false;
      }
      networkNotice('正在检查新版本…');
      await registration.update();
      if (registration.waiting) {
        announceUpdate(registration);
        return true;
      }
      const worker = registration.installing;
      if (worker) {
        await new Promise(resolve => {
          const timer = setTimeout(resolve, 5000);
          worker.addEventListener('statechange', () => {
            if (['installed', 'activated', 'redundant'].includes(worker.state)) {
              clearTimeout(timer);
              resolve();
            }
          });
        });
      }
      if (registration.waiting) {
        announceUpdate(registration);
        return true;
      }
      networkNotice('当前已经是最新版本');
      return false;
    } catch (error) {
      console.warn('[PWA] 检查更新失败:', error);
      networkNotice('检查更新失败，请确认网络后重试');
      return false;
    }
  }

  window.OpenNexusPWA = {checkForUpdate};

  function installPromptRecentlyDismissed() {
    const dismissedAt = Number(localStorage.getItem(INSTALL_DISMISS_KEY) || 0);
    return dismissedAt > 0 && Date.now() - dismissedAt < INSTALL_DISMISS_MS;
  }

  function rememberInstallPromptDismissal() {
    localStorage.setItem(INSTALL_DISMISS_KEY, String(Date.now()));
  }

  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    installPrompt = event;
    if (isStandalone || !isMobileDevice || installPromptRecentlyDismissed()) return;
    const banner = actionBanner('pwa-install-banner', '安装到手机桌面，使用更方便', '安装', async () => {
      if (!installPrompt) return;
      await installPrompt.prompt();
      const result = await installPrompt.userChoice;
      installPrompt = null;
      banner.remove();
      if (result.outcome !== 'accepted') rememberInstallPromptDismissal();
    });
    banner.querySelector('.pwa-close').addEventListener('click', rememberInstallPromptDismissal);
  });

  window.addEventListener('appinstalled', () => {
    installPrompt = null;
    document.getElementById('pwa-install-banner')?.remove();
  });
  window.addEventListener('offline', () => networkNotice('网络已断开，部分功能暂不可用'));
  window.addEventListener('online', () => networkNotice('网络已恢复'));

  addStyles();
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (refreshing) return;
    refreshing = true;
    window.location.reload();
  });
  window.addEventListener('load', async () => {
    try {
      const registration = await navigator.serviceWorker.register('/sw.js', {scope: '/'});
      activeRegistration = registration;
      announceUpdate(registration);
      registration.addEventListener('updatefound', () => {
        const worker = registration.installing;
        worker?.addEventListener('statechange', () => {
          if (worker.state === 'installed' && navigator.serviceWorker.controller) announceUpdate(registration);
        });
      });
    } catch (error) {
      console.warn('[PWA] Service Worker 注册失败:', error);
    }
  });
})();
