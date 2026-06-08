// pages/index/index.js — 录音主页逻辑

const app = getApp();

// 录音最长时间（秒）
const MAX_RECORD_SECONDS = 60;

Page({
  data: {
    serverBase: '',
    serverPresets: [],
    presetIndex: 0,
    serverConnected: false,
    asrProvider: '',
    asrModel: '',
    zhipuConfigured: false,
    activeEngine: '',
    device: '',
    gpuAvailable: false,

    // 录音状态
    isRecording: false,
    recordDuration: 0,
    recordDurationText: '00:00',

    // 处理状态
    isProcessing: false,
    processingStep: '上传音频...',
    uploadProgress: 0,

    // 结果
    lastResult: null,
    engineBadge: 'info',

    // 错误
    errorMsg: '',
    showHelp: false,

    // 服务器地址弹窗
    showServerModal: false,
    serverInput: '',

    // 临时隐藏文件上传入口，避免遮挡地址修改弹窗
    enableFileUpload: false,
  },

  // ─────── 生命周期 ───────
  onLoad() {
    const presets = app.globalData.serverPresets || [];
    const stored = wx.getStorageSync('serverBase');
    const base = stored || app.globalData.serverBase;
    this.setData({
      serverBase: base,
      serverInput: base,
      serverPresets: presets,
      presetIndex: this._findPresetIndexByUrl(base, presets),
    });
    app.globalData.serverBase = base;
  },

  onShow() {
    this.setData({
      serverConnected: app.globalData.serverConnected,
      asrProvider: app.globalData.asrProvider,
      asrModel: app.globalData.asrModel,
      zhipuConfigured: app.globalData.zhipuConfigured,
      activeEngine: app.globalData.activeEngine || '',
      device: app.globalData.device || '',
      gpuAvailable: !!app.globalData.gpuAvailable,
    });
    this.checkConnection();
  },

  // ─────── 连接检查 ───────
  checkConnection() {
    const base = this.data.serverBase;
    wx.request({
      url: `${base}/health`,
      timeout: 5000,
      success: (res) => {
        if (res.statusCode === 200) {
          this.setData({
            serverConnected: true,
            asrProvider: res.data.asr_provider || '',
            asrModel: res.data.asr_model || '',
            zhipuConfigured: !!res.data.zhipu_configured,
            activeEngine: res.data.active_engine || '',
            device: res.data.device || '',
            gpuAvailable: !!res.data.gpu_available,
          });
          app.globalData.serverConnected = true;
          app.globalData.asrProvider = res.data.asr_provider || '';
          app.globalData.asrModel = res.data.asr_model || '';
          app.globalData.zhipuConfigured = !!res.data.zhipu_configured;
          app.globalData.activeEngine = res.data.active_engine || '';
          app.globalData.device = res.data.device || '';
          app.globalData.gpuAvailable = !!res.data.gpu_available;
        }
      },
      fail: () => {
        this.setData({
          serverConnected: false,
          asrProvider: '',
          asrModel: '',
          zhipuConfigured: false,
          activeEngine: '',
          device: '',
          gpuAvailable: false,
        });
      }
    });
  },

  // ─────── 服务器地址管理 ───────
  onEditServer() {
    this.setData({ showServerModal: true, serverInput: this.data.serverBase });
  },
  onCloseModal() {
    this.setData({ showServerModal: false });
  },
  onServerInput(e) {
    this.setData({ serverInput: e.detail.value });
  },
  onPresetChange(e) {
    const idx = Number(e.detail.value || 0);
    const presets = this.data.serverPresets || [];
    const selected = presets[idx];
    if (!selected || !selected.url) return;
    const url = selected.url.replace(/\/$/, '');
    this.setData({ presetIndex: idx, serverInput: url });
  },
  onSaveServer() {
    let url = this.data.serverInput.trim();
    if (!url.startsWith('http')) {
      wx.showToast({ title: '地址须以 http:// 开头', icon: 'none' });
      return;
    }
    url = url.replace(/\/$/, ''); // 去掉末尾斜杠
    this._persistServerBase(url);
    this.setData({
      serverBase: url,
      serverInput: url,
      showServerModal: false,
      presetIndex: this._findPresetIndexByUrl(url, this.data.serverPresets),
    });
    this.checkConnection();
  },

  // ─────── 录音 ───────
  onRecordStart() {
    if (!this.data.serverConnected) {
      wx.showToast({ title: '服务未连接，请检查设置', icon: 'none', duration: 2000 });
      return;
    }
    this._startRecording();
  },

  onRecordEnd() {
    if (this.data.isRecording) {
      this._stopRecording();
    }
  },

  onRecordCancel() {
    // 手指移出按钮区域时取消
    if (this.data.isRecording) {
      this._cancelRecording();
    }
  },

  _startRecording() {
    const recManager = wx.getRecorderManager();
    this.recManager = recManager;

    recManager.onStart(() => {
      console.log('[Rec] 开始录音');
      this.setData({ isRecording: true, recordDuration: 0, errorMsg: '' });
      this._startTimer();
    });

    recManager.onStop((res) => {
      console.log('[Rec] 停止录音', res);
      this._stopTimer();
      this.setData({ isRecording: false });
      if (res.duration < 500) {
        wx.showToast({ title: '录音太短，请重试', icon: 'none' });
        return;
      }
      this._uploadAudio(res.tempFilePath, res.duration);
    });

    recManager.onError((err) => {
      console.error('[Rec] 录音错误', err);
      this._stopTimer();
      this.setData({ isRecording: false });
      this._showError(`录音失败：${err.errMsg}`, false);
    });

    recManager.start({
      duration: MAX_RECORD_SECONDS * 1000,
      sampleRate: 16000,
      numberOfChannels: 1,
      encodeBitRate: 96000,
      format: 'aac',           // iOS/Android 均支持
    });
  },

  _stopRecording() {
    if (this.recManager) {
      this.recManager.stop();
    }
  },

  _cancelRecording() {
    if (this.recManager) {
      this.recManager.stop();
    }
    this._stopTimer();
    this.setData({ isRecording: false });
    wx.showToast({ title: '已取消录音', icon: 'none' });
  },

  _startTimer() {
    this._timerTick = setInterval(() => {
      const d = this.data.recordDuration + 1;
      const mm = String(Math.floor(d / 60)).padStart(2, '0');
      const ss = String(d % 60).padStart(2, '0');
      this.setData({ recordDuration: d, recordDurationText: `${mm}:${ss}` });
      if (d >= MAX_RECORD_SECONDS) {
        this._stopRecording();
      }
    }, 1000);
  },

  _stopTimer() {
    if (this._timerTick) {
      clearInterval(this._timerTick);
      this._timerTick = null;
    }
  },

  // ─────── 选择文件 ───────
  onChooseFile() {
    if (!this.data.enableFileUpload) {
      wx.showToast({ title: '文件上传功能暂时隐藏', icon: 'none' });
      return;
    }
    wx.chooseMessageFile({
      count: 1,
      type: 'file',
      extension: ['mp3', 'aac', 'wav', 'm4a', 'ogg', 'flac', 'webm'],
      success: (res) => {
        const file = res.tempFiles[0];
        this._uploadAudio(file.path, 0, file.name);
      },
      fail: (err) => {
        if (!err.errMsg.includes('cancel')) {
          wx.showToast({ title: '选择文件失败', icon: 'none' });
        }
      }
    });
  },

  // ─────── 上传与转写 ───────
  _uploadAudio(filePath, duration, originalName) {
    const base = this.data.serverBase;
    const fileName = originalName || `record_${Date.now()}.aac`;

    this.setData({
      isProcessing: true,
      uploadProgress: 0,
      processingStep: '上传音频...',
      lastResult: null,
      errorMsg: '',
    });

    const uploadTask = wx.uploadFile({
      url: `${base}/transcribe`,
      filePath: filePath,
      name: 'audio',
      fileName: fileName,
      formData: { duration: String(duration) },
      timeout: 120000,

      success: (res) => {
        this.setData({ uploadProgress: 100, processingStep: '转写完成' });
        try {
          const data = JSON.parse(res.data);
          if (data.success) {
            // 计算徽章颜色
            let badge = 'info';
            if (data.engine === 'glm-asr-2512') badge = 'success';
            else if (data.engine === 'whisper-cpp') badge = 'success';
            else if (data.engine === 'phi3-fallback') badge = 'warning';
            else if (data.engine === 'none') badge = 'error';

            this.setData({
              lastResult: data,
              engineBadge: badge,
              isProcessing: false,
            });

            wx.showToast({ title: '转写成功！', icon: 'success' });

            // 震动反馈
            wx.vibrateShort({ type: 'light' });
          } else {
            throw new Error(data.detail || '转写失败');
          }
        } catch (e) {
          this.setData({ isProcessing: false });
          this._showError(`解析响应失败：${e.message}`, true);
        }
      },

      fail: (err) => {
        this.setData({ isProcessing: false });
        this._showError(
          `上传失败：${err.errMsg}\n请检查网络和服务是否正常运行`,
          true
        );
      }
    });

    // 监听上传进度
    uploadTask.onProgressUpdate((prog) => {
      const p = prog.progress || 0;
      let step = '上传音频...';
      if (p >= 100) step = 'AI 转写中，请稍候...';
      this.setData({ uploadProgress: p, processingStep: step });
    });
  },

  // ─────── 复制与分享 ───────
  onCopyText() {
    const text = this.data.lastResult?.text;
    if (!text) return;
    wx.setClipboardData({
      data: text,
      success: () => wx.showToast({ title: '已复制', icon: 'success' })
    });
  },

  onShareResult() {
    const r = this.data.lastResult;
    if (!r) return;
    wx.showShareMenu({ withShareTicket: false });
  },

  onShareAppMessage() {
    const r = this.data.lastResult;
    return {
      title: r ? `ASR转写：${r.text.slice(0, 30)}...` : '语音转文字',
      path: '/pages/index/index',
    };
  },

  // ─────── 错误处理 ───────
  _showError(msg, showHelp) {
    this.setData({ errorMsg: msg, showHelp: !!showHelp });
  },

  _persistServerBase(url) {
    app.globalData.serverBase = url;
    wx.setStorageSync('serverBase', url);
  },

  _findPresetIndexByUrl(url, presets) {
    const list = presets || [];
    for (let i = 0; i < list.length; i += 1) {
      if ((list[i].url || '').replace(/\/$/, '') === (url || '').replace(/\/$/, '')) {
        return i;
      }
    }
    return 0;
  },

  onUnload() {
    this._stopTimer();
  }
});
