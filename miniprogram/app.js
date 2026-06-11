// app.js — 全局配置与初始化
const config = require('./config');
const AUTO_UPGRADE_TO_HTTPS = true;

function isLocalLoopback(base) {
  return base.includes('127.0.0.1') || base.includes('localhost') || base.includes('0.0.0.0');
}

function isPrivateIpv4Host(host) {
  const h = String(host || '').trim();
  const m = h.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
  if (!m) {
    return false;
  }
  const a = Number(m[1]);
  const b = Number(m[2]);
  if (a === 10 || a === 127) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  if (a === 169 && b === 254) return true;
  return false;
}

function shouldKeepHttp(base) {
  if (isLocalLoopback(base)) {
    return true;
  }
  const m = String(base || '').trim().match(/^https?:\/\/([^\/:?#]+)/i);
  if (!m) {
    return false;
  }
  return isPrivateIpv4Host(m[1]);
}

function buildRequestBase(serverBase) {
  const base = String(serverBase || '').trim().replace(/\/$/, '');
  if (!base || !base.startsWith('http')) {
    return base;
  }
  if (base.startsWith('https://')) {
    return base;
  }
  if (!AUTO_UPGRADE_TO_HTTPS || shouldKeepHttp(base)) {
    return base;
  }
  return `https://${base.slice('http://'.length)}`;
}

App({
  globalData: {
    // 默认地址优先用于安卓 USB 网络共享调试，真机请按实际网段调整
    serverBase: config.normalizeServerBase(config.serverBase),
    requestBase: buildRequestBase(config.normalizeServerBase(config.serverBase)),
    serverPresets: config.serverPresets,

    // 连接状态
    serverConnected: false,
    asrProvider: '',
    asrModel: '',
    zhipuConfigured: false,
  },

  onLaunch() {
    console.log('[App] onLaunch');
    const stored = config.normalizeServerBase(wx.getStorageSync('serverBase') || '');
    const isOldLocalValue = stored.includes('127.0.0.1') || stored.includes('localhost');
    const isLegacyUsbPreset = stored.includes('192.168.137.1:8765');
    const configuredBase = config.normalizeServerBase(config.serverBase);
    this.globalData.serverBase = configuredBase;
    wx.setStorageSync('serverBase', configuredBase);
    this.globalData.requestBase = buildRequestBase(this.globalData.serverBase);
    if (isLegacyUsbPreset) {
      wx.setStorageSync('serverBase', configuredBase);
    }
    if (stored && isOldLocalValue) {
      wx.setStorageSync('serverBase', configuredBase);
    }
    // 检查服务连接状态
    this.checkServerHealth();
  },

  onShow() {
    this.checkServerHealth();
  },

  /**
   * 检查后端服务健康状态
   */
  checkServerHealth() {
    const requestBase = this.globalData.requestBase || buildRequestBase(this.globalData.serverBase);
    const rawBase = config.normalizeServerBase(this.globalData.serverBase);
    const bases = [];
    if (requestBase && requestBase.startsWith('http')) bases.push(requestBase);
    if (rawBase && rawBase.startsWith('http') && !bases.includes(rawBase)) bases.push(rawBase);
    if (!bases.length) {
      return;
    }
    const tryHealth = (idx) => {
      if (idx >= bases.length) {
        this.globalData.serverConnected = false;
        this.globalData.asrProvider = '';
        this.globalData.asrModel = '';
        this.globalData.zhipuConfigured = false;
        return;
      }
      const base = bases[idx];
      wx.request({
        url: `${base}/health`,
        method: 'GET',
        timeout: 5000,
        success: (res) => {
          if (res.statusCode === 200) {
            this.globalData.serverConnected = true;
            this.globalData.asrProvider = res.data.asr_provider || '';
            this.globalData.asrModel = res.data.asr_model || '';
            this.globalData.zhipuConfigured = !!res.data.zhipu_configured;
            this.globalData.requestBase = base;
            console.log('[App] 服务连接正常', res.data);
            return;
          }
          tryHealth(idx + 1);
        },
        fail: () => {
          tryHealth(idx + 1);
        }
      });
    };
    tryHealth(0);
  }
});
