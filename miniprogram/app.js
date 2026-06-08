// app.js — 全局配置与初始化
App({
  globalData: {
    // ⚠️ 修改为你的电脑局域网 IP（手机和电脑需在同一 WiFi）
    // 开发模式可使用 http，上线需配置 HTTPS 域名
    serverBase: 'https://www.hsfh.com.cn',

    // 连接状态
    serverConnected: false,
    ollamaModels: [],
  },

  onLaunch() {
    console.log('[App] onLaunch');
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
    wx.request({
      url: `${base}/health`,
      method: 'GET',
      timeout: 5000,
      success: (res) => {
        if (res.statusCode === 200) {
          this.globalData.serverConnected = true;
          this.globalData.ollamaModels = res.data.ollama_models || [];
          console.log('[App] 服务连接正常', res.data);
        }
      },
      fail: (err) => {
        this.globalData.serverConnected = false;
        console.warn('[App] 服务连接失败', err);
      }
    });
  }
});
