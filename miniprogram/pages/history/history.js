// pages/history/history.js

const app = getApp();
const config = require('../../config');

Page({
  data: {
    records: [],
    isLoading: false,
    isRefreshing: false,
    todayCount: 0,
    showDetail: false,
    currentRecord: null,
  },

  onLoad() {
    this.loadRecords();
  },

  onShow() {
    this.loadRecords();
  },

  onRefresh() {
    this.setData({ isRefreshing: true });
    this.loadRecords(() => {
      setTimeout(() => this.setData({ isRefreshing: false }), 500);
    });
  },

  loadRecords(cb) {
    const base = config.normalizeServerBase(wx.getStorageSync('serverBase') || app.globalData.serverBase);
    this.setData({ isLoading: true });

    wx.request({
      url: `${base}/records?limit=50`,
      timeout: 10000,
      success: (res) => {
        if (res.statusCode === 200) {
          const today = new Date().toISOString().slice(0, 10);
          const records = (res.data.records || []).map(r => ({
            ...r,
            sizeText: this._formatSize(r.size),
            mtime: this._formatTime(r.mtime),
          }));
          const todayCount = records.filter(r => r.mtime.startsWith(today)).length;
          this.setData({ records, todayCount });
        }
      },
      fail: () => {
        wx.showToast({ title: '加载失败，检查服务', icon: 'none' });
      },
      complete: () => {
        this.setData({ isLoading: false });
        cb && cb();
      }
    });
  },

  onViewDetail(e) {
    const idx = e.currentTarget.dataset.index;
    const record = this.data.records[idx];
    this.setData({ showDetail: true, currentRecord: record });
  },

  onCloseDetail() {
    this.setData({ showDetail: false, currentRecord: null });
  },

  onCopyDetail() {
    const text = this.data.currentRecord?.preview || '';
    wx.setClipboardData({
      data: text,
      success: () => wx.showToast({ title: '已复制', icon: 'success' })
    });
  },

  _formatSize(bytes) {
    if (!bytes) return '0B';
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  },

  _formatTime(iso) {
    if (!iso) return '';
    return iso.replace('T', ' ').slice(0, 19);
  },
});
