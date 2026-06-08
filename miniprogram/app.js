// app.js — 全局配置与初始化
const DEFAULT_SERVER_BASE = 'http://192.168.137.1:8765';

App({
  globalData: {
    // 默认地址优先用于安卓 USB 网络共享调试，真机请按实际网段调整
    serverBase: DEFAULT_SERVER_BASE,
    serverPresets: [
      {
        key: 'usb',
        label: 'USB 共享网络 IP',
        url: DEFAULT_SERVER_BASE,
      },
      {
        key: 'lan',
        label: '局域网 IP',
        url: 'http://172.18.1.79:8765',
      },
      {
        key: 'tunnel',
        label: '临时隧道地址',
        url: 'https://example.ngrok-free.app',
      }
    ],

    // 连接状态
    serverConnected: false,
    asrProvider: '',
    asrModel: '',
    zhipuConfigured: false,
  },

  onLaunch() {
    console.log('[App] onLaunch');
    const stored = wx.getStorageSync('serverBase') || '';
    const isOldLocalValue = stored.includes('127.0.0.1') || stored.includes('localhost');
    if (stored && !isOldLocalValue) {
      this.globalData.serverBase = stored;
    } else {
      this.globalData.serverBase = DEFAULT_SERVER_BASE;
      wx.setStorageSync('serverBase', DEFAULT_SERVER_BASE);
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
    const base = this.globalData.serverBase;
    if (!base || !base.startsWith('http')) {
      return;
    }
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
          console.log('[App] 服务连接正常', res.data);
        }
      },
      fail: (err) => {
        this.globalData.serverConnected = false;
        this.globalData.asrProvider = '';
        this.globalData.asrModel = '';
        this.globalData.zhipuConfigured = false;
        console.warn('[App] 服务连接失败', err);
      }
    });
  }
});
